#!/usr/bin/env python3
"""Computer-use agent: a vision LLM drives a real browser, through PlanRouter.

What this is
------------
A closed loop:  observe (screenshot + interactive element list)  ->  model
picks one browser action  ->  Playwright performs it  ->  observe again.
The browser side is Playwright (Chromium) with a persistent profile, so a
site you logged into once stays logged in; the brain side is any chat model
reachable over the gateway, addressed as `facet-agent`, which the router
fills in with a tool-calling model and, because every request carries a
screenshot, with the vision-capable channel.

Actions are index-based, not pixel-based. Each observation stamps the
interactive elements with `data-cu-idx` and lists them for the model as
`[3] button "登录"`, so the model says *click 3* instead of guessing
coordinates — generic VL chat models are poor at pixel grounding but good at
picking from a labelled list. `click_point` remains as a fallback for canvas
/ non-DOM targets.

Usage
-----
    # store an account for a host (password is prompted, never echoed)
    python tools/computer_use_agent.py --add-credential www.modelscope.cn
    python tools/computer_use_agent.py --credentials      # what is stored

    # deterministic sign-in from that credential (no LLM call)
    python tools/computer_use_agent.py --autologin https://www.modelscope.cn

    # or sign in by hand once; the persistent profile keeps the session
    python tools/computer_use_agent.py --login https://www.modelscope.cn

    # let the agent do something (it can call login() itself when a stored
    # credential is needed; the password never reaches the model)
    python tools/computer_use_agent.py --task "打开 https://www.modelscope.cn/mcp 列出页面上前三个 MCP 服务的名字"

    # no LLM at all: dump what the model would see
    python tools/computer_use_agent.py --observe https://www.modelscope.cn

    # watch it work in a real window
    python tools/computer_use_agent.py --headed --task "..."

Config: gateway URL / key / model come from env (FACET_GATEWAY, FACET_KEY or
ROUTER_KEY, FACET_MODEL); the key falls back to the first entry of
data/keys.json next to this checkout. Nothing is hardcoded.

Requires: pip install playwright && python -m playwright install chromium

Safety: the agent is told never to type credentials or solve captchas (login
is expected to already be in the profile), and it refuses to act on hosts
outside --allow-domains when that flag is set.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _resolve_data_dir() -> Path:
    env = os.environ.get("FACET_DATA_DIR")
    if env:
        return Path(env)
    # Repo checkout: data beside it (<repo>\data). The Windows bundle runs
    # from <install>\app\tools\ (facet_tray.py is a sibling), so its data
    # lives one level up at <install>\data.
    if (REPO / "facet_tray.py").is_file():
        return REPO.parent / "data"
    return REPO / "data"


# The Windows bundle keeps its data beside the install (…\Facet\data), not
# beside the script, so the installer points us there with FACET_DATA_DIR.
DATA_DIR = _resolve_data_dir()


def _gateway_from_config() -> str:
    """Follow the port the installer wrote into router.json (default 8787)."""
    try:
        cfg = json.loads((DATA_DIR / "router.json").read_text(encoding="utf-8"))
        port = int((cfg.get("listen") or {}).get("port") or 8787)
    except (OSError, ValueError, TypeError):
        port = 8787
    return f"http://127.0.0.1:{port}/v1"


DEFAULT_GATEWAY = _gateway_from_config()
DEFAULT_MODEL = "facet-agent"
PROFILE_DIR = DATA_DIR / "cu-browser-profile"
RUNS_DIR = DATA_DIR / "cu-runs"
CRED_FILE = DATA_DIR / "cu-credentials.json"

VIEWPORT = {"width": 1280, "height": 800}
KEEP_IMAGES = 3          # screenshots kept verbatim in the history
MAX_ELEMENTS = 80
TEXT_LIMIT = 3000

SYSTEM_PROMPT = """\
You are a computer-use agent operating a real Chromium browser through tools.
Every turn you receive: the current URL, page title, scroll position, a
numbered list of the interactive elements on screen, and a screenshot.

Rules:
- Perform ONE action per turn, then wait for the new observation.
- Prefer click(index) / type(index, text) over coordinates; the element list
  is far more reliable than pixel guessing.
- If the element you want is not listed, use scroll(direction) and look again,
  or use text() to read the page when you only need information.
- Navigation may take a moment: after a click, judge from the next observation
  rather than assuming success.
- To sign in, call login(url) — it fills the locally stored credential for
  that host itself, so you never see or type a password. Never type
  passwords or verification codes by hand, and never solve captchas; if a
  site needs a step you cannot do (captcha, SMS code, 2FA), stop and say so.
- When the task is finished (or impossible), call done(summary) and state the
  result plainly; the summary is what the user receives.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "navigate", "description": "Open a URL in the current tab.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "login",
        "description": "Sign in with the credential stored locally for this "
                       "host. The password is filled in by the tool itself and "
                       "is never shown to you.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "login page URL (defaults to current page)"}}}}},
    {"type": "function", "function": {
        "name": "click", "description": "Click the element with this index from the list.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"}}, "required": ["index"]}}},
    {"type": "function", "function": {
        "name": "click_point", "description": "Click raw viewport pixels (only for non-DOM targets).",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"]}}},
    {"type": "function", "function": {
        "name": "type", "description": "Type text into the element with this index (replaces its content).",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer"}, "text": {"type": "string"},
            "submit": {"type": "boolean", "description": "press Enter afterwards"}},
            "required": ["index", "text"]}}},
    {"type": "function", "function": {
        "name": "key", "description": "Press a key: Enter, Escape, Tab, Backspace, ArrowDown...",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "scroll", "description": "Scroll the page.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
            "amount": {"type": "integer", "description": "pixels, default one viewport"}},
            "required": ["direction"]}}},
    {"type": "function", "function": {
        "name": "text", "description": "Read the page's visible text (truncated).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "wait", "description": "Pause for up to 15 seconds (loading, animation).",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "number"}}, "required": ["seconds"]}}},
    {"type": "function", "function": {
        "name": "done", "description": "Finish: report the outcome to the user.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]

# ---------------------------------------------------------------- observation

_OBSERVE_JS = """
() => {
  const sel = 'a[href],button,input,textarea,select,summary,label,' +
              '[role=button],[role=link],[role=tab],[role=checkbox],[role=menuitem],' +
              '[onclick],[contenteditable=true],[tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-cu-idx]').forEach(e => e.removeAttribute('data-cu-idx'));
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' ||
        st.pointerEvents === 'none' || parseFloat(st.opacity) === 0) continue;
    const text = ((el.innerText || '') || (el.type === 'password' ? '\\u2022\\u2022\\u2022\\u2022' : el.value) ||
      el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
      el.getAttribute('title') || el.getAttribute('alt') || '').trim()
      .replace(/\\s+/g, ' ').slice(0, 70);
    if (el.type === 'hidden') continue;
    el.setAttribute('data-cu-idx', String(i));
    out.push('[' + i + '] ' + el.tagName.toLowerCase() +
      (el.type ? ':' + el.type : '') + (el.tagName === 'A' ? ' link' : '') +
      ' "' + text + '"' +
      ((el.tagName === 'A' && el.getAttribute('href') || '').length ?
        ' -> ' + (el.getAttribute('href') || '').slice(0, 60) : ''));
    i++;
    if (i >= %MAX%) break;
  }
  return {
    url: location.href,
    title: document.title,
    scroll: Math.round(scrollY),
    height: Math.round(document.body ? document.body.scrollHeight : 0),
    elements: out
  };
}
""".replace("%MAX%", str(MAX_ELEMENTS))


def observe(page) -> tuple[str, str]:
    """Return (text observation, jpeg data URL) for the current page."""
    info = page.evaluate(_OBSERVE_JS)
    shot = base64.b64encode(page.screenshot(type="jpeg", quality=60)).decode()
    lines = [f"URL: {info['url']}", f"TITLE: {info['title']}",
             f"SCROLL: {info['scroll']}px of {info['height']}px"]
    lines.append("ELEMENTS:")
    lines.extend(info["elements"] or ["  (none visible)"])
    return "\n".join(lines), "data:image/jpeg;base64," + shot


# ------------------------------------------------------------------ gateway

def gateway_key() -> str:
    for name in ("FACET_KEY", "ROUTER_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    path = DATA_DIR / "keys.json"
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return ""
        for item in raw.get("keys") or []:
            if isinstance(item, dict) and item.get("key"):
                return str(item["key"])
    return ""


def chat(url: str, key: str, model: str, messages: list) -> tuple[dict, dict]:
    body = json.dumps({"model": model, "messages": messages, "tools": TOOLS,
                       "tool_choice": "auto", "temperature": 0.2}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read())
            routed = {k: v for k, v in resp.headers.items()
                      if k.lower().startswith("x-routed") or k.lower() == "x-route"}
    except urllib.error.HTTPError as exc:
        detail = str(exc.read()[:400], "utf-8", "replace")
        raise RuntimeError(f"gateway HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"gateway unreachable at {url}: {exc}") from exc
    return payload, routed


def prune_images(messages: list) -> None:
    """Keep only the newest few screenshots; older turns keep their text."""
    shots = [m for m in messages
             if m.get("role") == "user" and isinstance(m.get("content"), list)
             and any(p.get("type") == "image_url" for p in m["content"])]
    for old in shots[:-KEEP_IMAGES]:
        old["content"] = [p for p in old["content"] if p.get("type") != "image_url"] \
            + [{"type": "text", "text": "[screenshot omitted]"}]


# ---------------------------------------------------------------- credentials
#
# Stored in data/cu-credentials.json (data/ is gitignored), one entry per
# host, and the password is only ever read here — it is never sent to the
# model, never echoed by the login tool, and password inputs are masked in
# the observation. Point at another file with CU_CREDENTIALS.

def cred_file() -> Path:
    return Path(os.environ.get("CU_CREDENTIALS") or CRED_FILE)


def load_creds() -> dict:
    path = cred_file()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_cred(domain: str, username: str = "", password: str = "",
              cookie: str = "") -> Path:
    """Merge a credential into the store. Empty fields keep the stored value."""
    path = cred_file()
    data = load_creds()
    entry = dict(data.get(domain) or {})
    if username:
        entry["username"] = username
    if password:
        entry["password"] = password
    if cookie:
        entry["cookie"] = cookie
    data[domain] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return (m.group(1) if m else url).split(":")[0].lower()


def cred_for(url: str) -> tuple[str, dict] | None:
    """Longest stored host that matches this URL (www.x.cn beats .x.cn)."""
    host = host_of(url)
    data = load_creds()
    best = ""
    for domain in data:
        d = str(domain).lower()
        if (host == d or host.endswith("." + d.lstrip("."))) and len(d) > len(best):
            best = d
    return (best, data[best]) if best else None


ACCOUNT_SEL = ("input[name*=account i], input[name*=user i], "
               "input[name*=email i], input[name*=phone i], "
               "input[placeholder*='账号'], input[placeholder*='邮箱'], "
               "input[placeholder*='手机'], input[type=email], "
               "input[type=text]:not([type=password])")


def login_frame(page, entry: dict):
    """Frame that holds the password field.

    Large sites serve their login form from a passport iframe (ModelScope uses
    https://passport.modelscope.cn/mini_login.htm), so the main document has
    no inputs at all; walk the frames instead of assuming one.
    """
    sel = entry.get("password_selector") or "input[type=password]"
    frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    for frame in frames:
        try:
            if frame.locator(sel).count():
                return frame
        except Exception:  # noqa: BLE001 - a frame can detach mid-walk
            continue
    return None


def accept_agreement(frame) -> bool:
    """Tick the "I agree to the terms" box password forms usually require."""
    box = frame.locator("input[type=checkbox]").first
    if not box.count():
        return False
    try:
        if box.is_checked():
            return True
        box.check(force=True, timeout=3000)
        return True
    except Exception:  # noqa: BLE001 - custom-styled box hides the input
        try:
            box_id = box.get_attribute("id")
            if box_id:
                label = frame.locator(f"label[for='{box_id}']").first
                if label.count():
                    label.click(timeout=3000)
                    return True
            box.click(force=True, timeout=3000)
            return True
        except Exception:  # noqa: BLE001 - not fatal, the site may not need it
            return False


def autologin(page, target: str, entry: dict, manual_seconds: int = 0) -> str:
    """Fill the stored username/password locally and submit.

    Selectors can be pinned per host in the credential file
    (username_selector / password_selector / submit_selector); otherwise the
    first visible password input decides, wherever it lives — main document
    or passport iframe.
    """
    if host_of(page.url) != host_of(target) or page.url == "about:blank":
        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        settle(page)

    frame = login_frame(page, entry)
    if frame is None:
        link = page.locator("text=密码登录").first
        if link.count():
            link.click()
            settle(page)
            frame = login_frame(page, entry)
    if frame is None:
        return "no login form found on this page"

    account = frame.locator(entry.get("username_selector") or ACCOUNT_SEL).first
    password = frame.locator(entry.get("password_selector") or
                             "input[type=password]").first
    if not account.count() or not password.count():
        return "no login form found on this page"
    account.fill(str(entry.get("username", "")))
    password.fill(str(entry.get("password", "")))
    accept_agreement(frame)

    submit = frame.locator(entry.get("submit_selector") or
                           "button[type=submit]").first
    if not submit.count():
        for hint in ("登录", "Sign in", "Log in"):
            submit = frame.locator(f"button:has-text('{hint}')").first
            if submit.count():
                break
    if submit.count():
        submit.click()
    else:
        password.press("Enter")
    settle(page)

    if login_frame(page, entry) is not None and manual_seconds:
        print(f"[login] still on a password form — {manual_seconds}s left to "
              "finish by hand (captcha / SMS code)", flush=True)
        page.wait_for_timeout(manual_seconds * 1000)
    left = login_frame(page, entry) is not None
    return ("still showing a login form (needs captcha / manual step)"
            if left else f"signed in as {entry.get('username')} on {host_of(page.url)}")


# ------------------------------------------------------------------- actions

def run_action(page, name: str, args: dict, manual_seconds: int = 0) -> str:
    if name == "navigate":
        page.goto(args["url"], wait_until="domcontentloaded", timeout=60_000)
        settle(page)
        return f"navigated to {page.url}"
    if name == "login":
        target = args.get("url") or page.url
        found = cred_for(target)
        if not found:
            return (f"no stored credential for {host_of(target)}; ask the user "
                    "to run --add-credential, or use --login for manual sign-in")
        return autologin(page, target, found[1], manual_seconds)
    if name == "click":
        sel = f'[data-cu-idx="{int(args["index"])}"]'
        page.click(sel, timeout=15_000)
        settle(page)
        return f"clicked element {args['index']}"
    if name == "click_point":
        page.mouse.click(int(args["x"]), int(args["y"]))
        settle(page)
        return f"clicked ({args['x']},{args['y']})"
    if name == "type":
        sel = f'[data-cu-idx="{int(args["index"])}"]'
        page.fill(sel, str(args.get("text", "")), timeout=15_000)
        if args.get("submit"):
            page.press(sel, "Enter")
            settle(page)
        return f"typed into element {args['index']}"
    if name == "key":
        page.keyboard.press(str(args["name"]))
        settle(page)
        return f"pressed {args['name']}"
    if name == "scroll":
        direction = args.get("direction", "down")
        amount = int(args.get("amount") or VIEWPORT["height"] - 80)
        delta = {"down": amount, "up": -amount,
                 "top": -1_000_000, "bottom": 1_000_000}[direction]
        page.evaluate(f"window.scrollBy(0, {delta})")
        return f"scrolled {direction}"
    if name == "text":
        body = page.evaluate("document.body ? document.body.innerText : ''") or ""
        return re.sub(r"\n{3,}", "\n\n", body.strip())[:TEXT_LIMIT]
    if name == "wait":
        time.sleep(min(float(args.get("seconds", 1)), 15.0))
        return "waited"
    if name == "done":
        return str(args.get("summary", ""))
    return f"unknown tool {name}"


def settle(page) -> None:
    """Give a click / navigation a moment, but never hang on a busy page."""
    try:
        page.wait_for_load_state("networkidle", timeout=6_000)
    except Exception:  # noqa: BLE001 - idle never arrives on chatty sites
        pass
    try:
        page.wait_for_timeout(600)
    except Exception:  # noqa: BLE001 - tab may already be gone
        pass


# ----------------------------------------------------------------- the loop

def agent_loop(page, url: str, key: str, model: str, task: str,
               max_steps: int, trace_dir: Path, allow: list[str],
               manual_seconds: int = 0) -> tuple[int, str]:
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Task: {task}"},
    ]
    transcript: list[dict] = []
    outcome, reason = "", ""
    for step in range(1, max_steps + 1):
        obs_text, shot = observe(page)
        if allow and not any(host in page.url for host in allow):
            outcome = "blocked: current page is outside --allow-domains"
            reason = "offsite"
            break
        messages.append({"role": "user", "content": [
            {"type": "text", "text": f"Step {step} observation:\n{obs_text}"},
            {"type": "image_url", "image_url": {"url": shot}}]})
        prune_images(messages)
        (trace_dir / f"step-{step:02d}.jpg").write_bytes(base64.b64decode(shot.split(",", 1)[1]))

        payload, routed = chat(url, key, model, messages)
        msg = (payload.get("choices") or [{}])[0].get("message", {})
        messages.append({"role": "assistant", "content": msg.get("content"),
                         "tool_calls": msg.get("tool_calls")})
        calls = msg.get("tool_calls") or []
        transcript.append({"step": step, "route": routed,
                           "content": (msg.get("content") or "")[:400],
                           "calls": [{"name": c["function"]["name"],
                                      "args": c["function"]["arguments"]} for c in calls]})
        thought = (msg.get("content") or "").strip().replace("\n", " ")
        print(f"[{step}] {routed.get('X-Routed-Via', '?')} | "
              f"{(calls[0]['function']['name'] if calls else 'say')}: "
              f"{(calls[0]['function']['arguments'] if calls else thought)[:120]}",
              flush=True)
        if not calls:
            if not thought:
                outcome, reason = "", "no-tool"
                break
            # a plain answer with no action is the model reporting the result
            outcome, reason = thought, "done"
            break

        finished = ""
        for call in calls:
            fname = call["function"]["name"]
            try:
                fargs = json.loads(call["function"]["arguments"] or "{}")
            except ValueError:
                fargs = {}
            try:
                result = run_action(page, fname, fargs, manual_seconds)
            except Exception as exc:  # noqa: BLE001 - tell the model, keep going
                result = f"ERROR: {type(exc).__name__}: {str(exc)[:200]}"
            messages.append({"role": "tool", "tool_call_id": call.get("id", fname),
                             "content": result[:TEXT_LIMIT]})
            if fname == "done":
                finished = result
        if finished:
            outcome, reason = finished, "done"
            break
    else:
        outcome = "step budget exhausted before the task was finished"
        reason = "max-steps"

    transcript.append({"outcome": outcome, "reason": reason})
    (trace_dir / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    return reason, outcome


# -------------------------------------------------------------------- browser

def launch(headed: bool, profile: Path, channel: str = "auto"):
    """Start a persistent context; `channel` picks the browser build.

    auto prefers the bundled Chromium and falls back to the installed Chrome,
    so the agent works before `playwright install chromium` has finished
    downloading on a machine that already has a browser.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed: pip install playwright "
                 "&& python -m playwright install chromium")
    playwright = sync_playwright().start()
    options = {"user_data_dir": str(profile), "headless": not headed,
               "viewport": VIEWPORT,
               "args": ["--disable-blink-features=AutomationControlled"]}
    order = {"auto": ["", "chrome"], "bundled": [""]}.get(channel, [channel])
    error = None
    for pick in order:
        attempt = dict(options)
        if pick:
            attempt["channel"] = pick
            print(f"[browser] falling back to the installed {pick} "
                  "(run `python -m playwright install chromium` for the "
                  "isolated build)", flush=True)
        try:
            return playwright, playwright.chromium.launch_persistent_context(**attempt)
        except Exception as exc:  # noqa: BLE001 - keep the message, try the next
            error = exc
            if pick == "" and len(order) > 1:
                continue
    playwright.stop()
    sys.exit(f"[browser] cannot start a browser: {str(error)[:300]}")


def mode_login(url: str, headed: bool, profile: Path, channel: str = "auto") -> int:
    playwright, context = launch(True, profile, channel)
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    print(f"[login] browser open at {url} with profile {profile}", flush=True)
    print("[login] sign in by hand (scan the QR / enter the password), "
          "then CLOSE that browser window when done — the session is saved.",
          flush=True)
    try:
        page.wait_for_event("close", timeout=0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            context.close()
        finally:
            playwright.stop()
    print(f"[login] profile kept at {profile}", flush=True)
    return 0


def mode_observe(url: str, headed: bool, profile: Path, channel: str = "auto") -> int:
    playwright, context = launch(headed, profile, channel)
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    settle(page)
    obs_text, shot = observe(page)
    out = RUNS_DIR / f"observe-{datetime.now():%Y%m%d-%H%M%S}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(shot.split(",", 1)[1]))
    print(obs_text)
    print(f"\n[screenshot] {out}")
    context.close()
    playwright.stop()
    return 0


def parse_cookie(text: str) -> tuple[str, str]:
    name, sep, value = str(text).partition("=")
    if not sep or not name.strip():
        return "", ""
    return name.strip(), value.strip()


def add_session_cookie(context, url: str, cookie: str, days: int = 30) -> bool:
    """Store a pasted session cookie for `url`'s host in this context.

    A cookie without `expires` is a session cookie: Chromium keeps it in
    memory and drops it when the context closes, so the next run would be
    logged out again. The 30-day lifetime makes it persist in the profile.
    """
    name, value = parse_cookie(cookie)
    if not name:
        return False
    context.add_cookies([{"name": name, "value": value,
                          "domain": host_of(url), "path": "/",
                          "expires": time.time() + days * 86400}])
    return True


def path_of(url: str) -> str:
    m = re.match(r"https?://[^/]+([^?#]*)", url)
    return (m.group(1) if m else "").rstrip("/")


def session_alive(page, url: str) -> bool:
    """True when the profile's session reaches `url` without a login detour.

    Two signals: the site did not bounce us to another path (logged-out users
    get redirected from /my/... to the home page), and no password form is
    presented anywhere — including a passport iframe.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    settle(page)
    if path_of(page.url) != path_of(url):
        return False
    return login_frame(page, {}) is None


def mode_seed_cookie(url: str, cookie: str, headed: bool, profile: Path,
                     channel: str = "auto") -> int:
    """Write one session cookie into the persistent profile, then show the page.

    Headless-friendly replacement for --login when there is no display (e.g.
    inside a container): paste a cookie you already have (for ModelScope the
    `ms-` token works as `m_session_id`) and it is stored in the profile.
    """
    name, value = parse_cookie(cookie)
    if not name:
        print("[seed] cookie must look like NAME=VALUE", file=sys.stderr)
        return 1
    if url == "about:blank":
        print("[seed] pass --url with the site the cookie belongs to",
              file=sys.stderr)
        return 1
    host = host_of(url)
    playwright, context = launch(headed, profile, channel)
    try:
        add_session_cookie(context, url, cookie)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        settle(page)
        obs_text, shot = observe(page)
        out = RUNS_DIR / f"seed-{datetime.now():%Y%m%d-%H%M%S}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(shot.split(",", 1)[1]))
        print(obs_text)
        print(f"\n[seed] {name.strip()} written for {host} in {profile}")
        print(f"[screenshot] {out}")
    finally:
        context.close()
        playwright.stop()
    return 0


def mode_autologin(url: str, headed: bool, profile: Path, channel: str,
                   manual_seconds: int) -> int:
    """Deterministic sign-in from the stored credential (no LLM involved)."""
    found = cred_for(url)
    if not found:
        print(f"[autologin] no credential stored for {host_of(url)} — run "
              f"--add-credential {host_of(url)} first", file=sys.stderr)
        return 1
    playwright, context = launch(headed, profile, channel)
    page = context.new_page()
    try:
        result = autologin(page, url, found[1], manual_seconds)
    finally:
        context.close()
        playwright.stop()
    print(f"[autologin] {result}", flush=True)
    return 0 if result.startswith("signed in") else 2


def mode_ensure_login(url: str, headed: bool, profile: Path, channel: str,
                      manual_seconds: int) -> int:
    """Keep the profile signed in, cheaply, for scheduled runs.

    Order: reuse the profile session -> paste the stored cookie -> fill the
    stored username/password. Exit 0 when signed in, 3 when a human must step
    in (expired cookie, or a captcha on the password form).
    """
    found = cred_for(url)
    playwright, context = launch(headed, profile, channel)
    page = context.new_page()
    try:
        if session_alive(page, url):
            print(f"[session] already signed in ({page.url})", flush=True)
            return 0
        print(f"[session] not signed in to {host_of(url)}; trying the stored "
              "credential", flush=True)

        if found and str(found[1].get("cookie") or "").strip():
            add_session_cookie(context, url, found[1]["cookie"])
            if session_alive(page, url):
                print(f"[session] stored cookie for {found[0]} accepted "
                      f"({page.url})", flush=True)
                return 0
            print("[session] stored cookie rejected (expired?)", flush=True)

        if found and str(found[1].get("password") or "").strip():
            print(f"[session] password login: "
                  f"{autologin(page, url, found[1], manual_seconds)}", flush=True)
            if session_alive(page, url):
                print(f"[session] signed in ({page.url})", flush=True)
                return 0

        print("[session] a human step is needed: refresh the cookie or the "
              "password in the console, or run --login on a desktop",
              file=sys.stderr)
        return 3
    finally:
        context.close()
        playwright.stop()


def mode_add_credential(host: str, username: str, password: str) -> int:
    if not username or not password:
        import getpass
        username = username or input("username: ").strip()
        password = password or getpass.getpass("password (not echoed): ")
    path = save_cred(host.lower().strip(), username, password)
    print(f"[credential] saved {username}@{host} -> {path}", flush=True)
    print("[credential] the file holds a plaintext password; keep it out of "
          "backups and never commit it (data/ is already gitignored).", flush=True)
    return 0


def mode_list_credentials() -> int:
    data = load_creds()
    if not data:
        print(f"(no credentials yet — file would be {cred_file()})")
        return 0
    for host, entry in sorted(data.items()):
        extra = [k for k in ("username_selector", "password_selector",
                             "submit_selector") if entry.get(k)]
        print(f"  {host:<32} {str(entry.get('username', '')):<20} "
              f"password: ****  {' '.join(extra)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", help="what the agent should do")
    ap.add_argument("--url", default="about:blank", help="starting page")
    ap.add_argument("--login", metavar="URL", help="open a window for manual login, then exit")
    ap.add_argument("--autologin", metavar="URL",
                    help="sign in from the stored credential, no LLM call")
    ap.add_argument("--add-credential", metavar="HOST",
                    help="store the username/password for this host")
    ap.add_argument("--credentials", action="store_true",
                    help="list stored hosts and usernames (passwords are never shown)")
    ap.add_argument("--username", default=os.environ.get("CU_USERNAME", ""),
                    help="with --add-credential; prefer the prompt over a CLI value")
    ap.add_argument("--password", default=os.environ.get("CU_PASSWORD", ""),
                    help="with --add-credential; prefer the prompt over a CLI value")
    ap.add_argument("--observe", metavar="URL", help="print the observation, no LLM call")
    ap.add_argument("--seed-cookie", metavar="NAME=VALUE",
                    help="store one session cookie in the profile (use with "
                         "--url; headless alternative to --login)")
    ap.add_argument("--ensure-login", metavar="URL",
                    help="check the saved session and, if stale, sign in from "
                         "the stored cookie or credential (exit 3 = a human "
                         "step is needed; use this for scheduled runs)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--model", default=os.environ.get("FACET_MODEL", DEFAULT_MODEL))
    ap.add_argument("--gateway", default=os.environ.get("FACET_GATEWAY", DEFAULT_GATEWAY))
    ap.add_argument("--profile", default=os.environ.get("CU_PROFILE", str(PROFILE_DIR)))
    ap.add_argument("--channel", default=os.environ.get("CU_CHANNEL", "auto"),
                    help="auto | bundled | chrome | msedge (browser build)")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--manual-seconds", type=int, default=120,
                    help="time to finish a captcha/SMS step by hand during autologin")
    ap.add_argument("--allow-domains", default="",
                   help="comma-separated host allowlist for the whole run")
    args = ap.parse_args()

    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)
    if args.credentials:
        return mode_list_credentials()
    if args.add_credential:
        return mode_add_credential(args.add_credential, args.username, args.password)
    if args.login:
        return mode_login(args.login, args.headed, profile, args.channel)
    if args.autologin:
        return mode_autologin(args.autologin, args.headed, profile, args.channel,
                              args.manual_seconds)
    if args.observe:
        return mode_observe(args.observe, args.headed, profile, args.channel)
    if args.seed_cookie:
        return mode_seed_cookie(args.url, args.seed_cookie, args.headed,
                                profile, args.channel)
    if args.ensure_login:
        return mode_ensure_login(args.ensure_login, args.headed, profile,
                                 args.channel, args.manual_seconds)
    if not args.task:
        ap.error("--task is required (or use --login / --autologin / "
                 "--ensure-login / --add-credential / --credentials / --observe)")

    key = gateway_key()
    if not key:
        print("no gateway key: set FACET_KEY, or start PlanRouter so that "
              "data/keys.json exists", file=sys.stderr)
        return 1

    run_dir = RUNS_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    allow = [h.strip() for h in args.allow_domains.split(",") if h.strip()]

    playwright, context = launch(args.headed, profile, args.channel)
    page = context.new_page()
    try:
        if args.url != "about:blank":
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        settle(page)
        reason, outcome = agent_loop(page, args.gateway, key, args.model,
                                     args.task, args.max_steps, run_dir, allow,
                                     args.manual_seconds)
    finally:
        context.close()
        playwright.stop()
    print(f"\n[outcome:{reason}] {outcome}", flush=True)
    print(f"[trace] {run_dir}")
    return 0 if reason == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
