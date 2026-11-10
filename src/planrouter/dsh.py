"""Locate and launch the DeepSeek Harness (DSH) web console.

DSH is an external Node client (`@deepseek-ai/dsh`), not part of PlanRouter:
its `planrouter-web` profile points the provider at this gateway. The console
only needs to know whether the CLI is installed, which profile it runs, and
where to reach it, so this module reports that state and spawns the profile
when asked.

The CLI writes its tokenised URL to stdout (`dsh web: http://...?token=...`).
Nothing is persisted here: a URL obtained by another shell is not reusable,
so only a process spawned by this module is reported as openable.

The model picker inside DSH is built from the profile's *static* `models`
list, not from the gateway's GET /v1/models, so `sync_models` rewrites that
block before every launch: the router's auto ids stay selectable next to every
backend id the console currently routes to.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

WEB_PROFILE = "planrouter-web"
URL_LINE = re.compile(r"dsh web: (http://\S+)")
BASE_URL_LINE = re.compile(r"^\s*baseURL:\s*(\S+)\s*$", re.MULTILINE)
MODELS_LINE = re.compile(r"^\s*models:\s*$")
LOG_LINES = 80
START_TIMEOUT = 90.0

# Friendly labels for the ids the router itself owns; a bare `auto:<route>`
# or `facet:<route>` falls back to the generic form below.
AUTO_LABELS = {
    "facet-auto": "自动 · 默认链",
    "facet-cheap": "自动 · 省钱链",
    "auto": "自动 · 默认链（auto）",
}

_lock = threading.Lock()
_start_lock = threading.Lock()
# profile name -> {"proc", "url", "log", "error"}
_runs: dict[str, dict] = {}


class DshError(RuntimeError):
    """Launch preconditions are not met (missing CLI/profile)."""


def home() -> Path:
    override = os.environ.get("DSH_HOME")
    return Path(override) if override else Path.home() / ".dsh"


def profile_dir(profile: str = WEB_PROFILE) -> Path:
    return home() / "profiles" / profile


def _package_bin(shim: Path) -> Path | None:
    """Resolve the package entry behind an npm shim, if it is reachable."""
    try:
        resolved = shim.resolve()
    except OSError:
        resolved = shim
    if resolved.name == "bin.js" and resolved.is_file():
        return resolved
    sibling = (shim.parent / "node_modules" / "@deepseek-ai" / "dsh"
               / "lib" / "bin.js")
    if sibling.is_file():
        return sibling
    return None


def find_cli() -> dict:
    """Return {"path", "bin", "version"}; path is the shim, bin the JS entry."""
    candidates: list[Path] = []
    override = os.environ.get("DSH_CLI")
    if override:
        candidates.append(Path(override))
    which = shutil.which("dsh")
    if which:
        candidates.append(Path(which))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "dsh.cmd")
    candidates.extend((Path("/usr/local/bin/dsh"), Path("/usr/bin/dsh")))
    for path in candidates:
        if not path.is_file():
            continue
        bin_path = _package_bin(path)
        version = ""
        if bin_path:
            try:
                manifest = json.loads(
                    (bin_path.parents[1] / "package.json").read_text(encoding="utf-8"))
                version = str(manifest.get("version") or "")
            except (OSError, ValueError):
                version = ""
        return {"path": str(path), "bin": str(bin_path) if bin_path else "",
                "version": version}
    return {"path": "", "bin": "", "version": ""}


def _node() -> str:
    return shutil.which("node") or ""


def _command(cli: dict, profile: str, port: int | None) -> list[str]:
    args = ["--profile", profile, "--no-open"]
    if port:
        args.extend(["--port", str(port)])
    if cli["bin"]:
        node = _node()
        if not node:
            raise DshError("找不到 node 可执行文件，无法启动 dsh")
        return [node, cli["bin"], *args]
    shim = cli["path"]
    if sys.platform == "win32" and shim.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", shim, *args]
    return [shim, *args]


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _pump(entry: dict) -> None:
    proc = entry["proc"]
    stream = proc.stdout
    if stream is None:
        return
    for line in stream:
        line = line.rstrip()
        with _lock:
            entry["log"].append(line)
            if len(entry["log"]) > LOG_LINES:
                del entry["log"][:len(entry["log"]) - LOG_LINES]
            found = URL_LINE.search(line)
            if found and not entry.get("url"):
                entry["url"] = found.group(1)


def _failure_reason(entry: dict, returncode: int | None) -> str:
    """Pick the useful line out of a crashed CLI log.

    Node prints a stack trace and a trailing version banner, so the last line
    says nothing; the leading `Error:`/`code:` line carries the real reason.
    """
    for line in entry["log"]:
        text = line.strip()
        if text.startswith("Error"):
            return text
    for line in reversed(entry["log"]):
        text = line.strip()
        if text and not text.startswith("at ") and "code:" not in text:
            return text
    return f"dsh 启动失败（退出码 {returncode}）"


def _profile_base_url(directory: Path) -> str:
    patch = directory / "cordis.patch.yml"
    try:
        text = patch.read_text(encoding="utf-8")
    except OSError:
        return ""
    matches = BASE_URL_LINE.findall(text)
    return matches[-1] if matches else ""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _models_block(lines: list[str]) -> tuple[int, int] | None:
    """Locate the `models:` list of the planrouter provider.

    Returns the half-open range [first item line, first line after the list),
    or None when the profile carries no such block.
    """
    entry = next((i for i, line in enumerate(lines)
                  if line.strip() == "- id: llm-pi-ai"), None)
    if entry is None:
        return None
    key = next((i for i in range(entry + 1, len(lines))
                if MODELS_LINE.match(lines[i])), None)
    if key is None:
        return None
    key_indent = _indent(lines[key])
    end = key + 1
    while end < len(lines) and (not lines[end].strip()
                                or _indent(lines[end]) > key_indent):
        end += 1
    return key + 1, end


def _label(item: dict) -> str:
    """Picker text: which auto chain it is, or id plus its backend channel."""
    name = item.get("id") or ""
    if item.get("mode") == "auto":
        return AUTO_LABELS.get(name) or f"自动 · {name}"
    owner = item.get("owned_by") or ""
    return f"{name} · {owner}" if owner else name


def _render_models(models: list[dict], indent: int) -> list[str]:
    pad = " " * indent
    out: list[str] = []
    for item in models:
        name = str(item.get("id") or "").strip()
        if not name:
            continue
        out.append(f"{pad}- id: {json.dumps(name, ensure_ascii=False)}")
        out.append(f"{pad}  name: {json.dumps(_label(item), ensure_ascii=False)}")
    return out


def sync_models(models: list[dict], profile: str = WEB_PROFILE) -> bool:
    """Rewrite the profile's provider model list to match the gateway.

    DSH only offers what its patch layer declares, so backend ids stay
    invisible in the picker until they appear here. Called before each launch,
    which keeps the list in step with channels enabled or disabled in the
    console; an unchanged list leaves the file untouched.
    """
    patch = profile_dir(profile) / "cordis.patch.yml"
    try:
        text = patch.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines(keepends=True)
    block = _models_block(lines)
    if block is None:
        return False
    start, end = block
    indent = _indent(lines[start - 1]) + 2
    rendered = [line + "\n" for line in _render_models(models, indent)]
    if not rendered:
        return False
    updated = lines[:start] + rendered + lines[end:]
    updated_text = "".join(updated)
    if updated_text == text:
        return True
    tmp = patch.with_name(patch.name + ".tmp")
    try:
        tmp.write_text(updated_text, encoding="utf-8")
        os.replace(tmp, patch)
    except OSError:
        return False
    return True


def status(profile: str = WEB_PROFILE) -> dict:
    """Describe the CLI, the profile wiring, and our own spawned process."""
    cli = find_cli()
    directory = profile_dir(profile)
    state = {
        "installed": bool(cli["path"]),
        "cli": cli["path"],
        "version": cli["version"],
        "home": str(home()),
        "profile": profile,
        "profile_exists": (directory / "package.json").is_file(),
        "base_url": _profile_base_url(directory),
        "running": False,
        "url": "",
        "pid": None,
        "log": [],
        "note": "",
    }
    entry = None
    with _lock:
        entry = _runs.get(profile)
        if entry is not None:
            state["log"] = list(entry["log"])[-20:]
            if _alive(entry["proc"]):
                state["running"] = True
                state["url"] = entry.get("url") or ""
                state["pid"] = entry["proc"].pid
    if not state["installed"]:
        state["note"] = ("未找到 dsh 命令：管理界面需与 dsh 装在同一台宿主机；"
                         "容器里没有该客户端。")
    elif not state["profile_exists"]:
        state["note"] = (f"缺少 dsh profile {profile}；先在终端运行一次 "
                         f"dsh --profile {profile} 生成。")
    elif entry is not None and not state["running"]:
        state["note"] = entry.get("error") or "上次启动的 dsh 已退出。"
    elif state["running"] and not state["url"]:
        state["note"] = "dsh 已启动，但启动行里还没有解析到访问地址。"
    return state


def start_web(api_key: str | None = None, profile: str = WEB_PROFILE,
              port: int | None = None) -> dict:
    """Spawn the DSH web profile and return the tokenised URL.

    The profile prints the URL after its loader settles, so the caller waits
    for it instead of guessing the port. A previously spawned process is
    reused; nothing is opened in a browser here, the console decides that.
    """
    with _start_lock:
        cli = find_cli()
        if not cli["path"]:
            raise DshError("未找到 dsh 命令：管理界面需与 dsh 装在同一台宿主机")
        directory = profile_dir(profile)
        if not (directory / "package.json").is_file():
            raise DshError(f"缺少 dsh profile {profile}；先运行一次 "
                           f"dsh --profile {profile} 生成")
        with _lock:
            existing = _runs.get(profile)
            already = existing is not None and _alive(existing["proc"])
        if already:
            return {**status(profile), "ok": bool(existing.get("url"))}
        command = _command(cli, profile, port)
        env = dict(os.environ)
        if api_key and not env.get("ROUTER_API_KEY"):
            env["ROUTER_API_KEY"] = api_key
        env["NO_COLOR"] = "1"
        kwargs = {}
        if sys.platform == "win32":
            # Detach from the console so closing the gateway shell does not
            # pop a window and keeps the agent process independent.
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, **kwargs,
        )
        entry = {"proc": proc, "url": "", "log": [], "error": ""}
        with _lock:
            _runs[profile] = entry
        thread = threading.Thread(target=_pump, args=(entry,), daemon=True)
        thread.start()
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if entry.get("url") or proc.poll() is not None:
                break
            time.sleep(0.2)
        if not entry.get("url"):
            if proc.poll() is None:
                entry["error"] = ("dsh 仍在启动中（冷启动较慢），进程已驻留；"
                                  "稍后点上角「刷新」即可看到访问地址")
            else:
                entry["error"] = _failure_reason(entry, proc.returncode)
            result = {**status(profile), "ok": False}
            result["error"] = entry["error"]
            return result
        return {**status(profile), "ok": True}
