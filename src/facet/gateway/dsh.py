"""Locate and launch the DeepSeek Harness (DSH) web console.

DSH is an external Node client (`@deepseek-ai/dsh`), not part of Facet:
its `facet-web` profile points the provider at this gateway. The console
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
from urllib.parse import urlsplit

WEB_PROFILE = "facet-web"
URL_LINE = re.compile(r"dsh web: (https?://\S+)")
BASE_URL_LINE = re.compile(r"^\s*baseURL:\s*(\S+)\s*$", re.MULTILINE)
MODELS_LINE = re.compile(r"^\s*models:\s*$")
PORT_IN_USE = re.compile(r"EADDRINUSE.*:(\d+)")
LOG_LINES = 80
START_TIMEOUT = 90.0
DEFAULT_BASE_URL = "http://127.0.0.1:8787"

# 与 docker/dsh-profile 同款：provider "facet" 指向本机网关，
# apiKeyEnv 复用启动时透传的 ROUTER_API_KEY，无需用户把密钥复制进 profile。
# 官方 web 模板只启用 base + web-app，工具插件随 CLI 装好了却没挂上，
# 界面看着像空架子。追加的都是安装包里自带 patch 的官方 bundle，
# 无需联网安装，解析失败时 dsh 会跳过而不是启动失败。
_PROFILE_BUNDLES = [
    "@deepseek-ai/dsh-base",
    "@deepseek-ai/dsh-web-app",
    "@deepseek-ai/dsh-experimental-terminal-bundle",
    "@deepseek-ai/dsh-experimental-session-search",
    "@deepseek-ai/dsh-experimental-session-titles-bundle",
    "@deepseek-ai/dsh-experimental-plugin-market-bundle",
]
# 旧版本播种的 bundle 组合，用于升级已存在的 profile。
_SEEDED_BUNDLES_V1 = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]

_PROFILE_PACKAGE = {
    "name": "dsh-profile-facet-web",
    "private": True,
    "dependencies": {},
    "dsh": {"profile": {"bundles": _PROFILE_BUNDLES}},
}

_PROFILE_PATCH = (
    "# DSH profile seeded by the Facet gateway on first launch.\n"
    "# provider \"facet\" points at this gateway's loopback address;\n"
    "# apiKeyEnv reuses ROUTER_API_KEY handed over at launch.\n"
    "- id: agent-default-model\n"
    "  config:\n"
    "    provider: facet\n"
    "    model: facet-auto\n"
    "- id: llm-pi-ai\n"
    "  config:\n"
    "    providers:\n"
    "      facet:\n"
    "        apiKeyEnv: ROUTER_API_KEY\n"
    "        api: openai-completions\n"
    "        baseURL: {base_url}/v1\n"
    "        compat:\n"
    "          supportsDeveloperRole: false\n"
    "          maxTokensField: max_tokens\n"
    "        models:\n"
    "          - id: \"facet-auto\"\n"
    "            name: \"自动 · 默认链\"\n"
    "          - id: \"facet-cheap\"\n"
    "            name: \"自动 · 省钱链\"\n"
    "          - id: \"facet-free\"\n"
    "            name: \"自动 · 免费链\"\n"
    "- id: ui-settings-general\n"
    "  name: \"@deepseek-ai/dsh-client-ui-settings-general\"\n"
    "  config:\n"
    "    welcomeNoticeVersion: 2026-09-28.1\n"
)


def ensure_profile(profile: str = WEB_PROFILE,
                   base_url: str = DEFAULT_BASE_URL) -> None:
    """Create the facet-web profile on first launch, wired to this gateway.

    Mirrors docker/dsh-profile (seeded by the container entrypoint) so a
    Windows install works without a manual ``dsh --profile facet-web`` step.
    An existing profile is never overwritten; only the bundle list this
    module seeded itself is upgraded in place.
    """
    directory = profile_dir(profile)
    patch = directory / "cordis.patch.yml"
    manifest = directory / "package.json"
    if patch.is_file():
        # The operator (or dsh itself) already created this profile; never
        # overwrite a hand-edited patch layer with the generated wiring.
        _upgrade_bundles(manifest)
        # One thing does follow this gateway: the facet provider's baseURL.
        # A profile seeded while the gateway ran on another port (installer
        # port page, later wizard change) would otherwise keep sending DSH
        # to the dead address forever.
        _sync_base_url(base_url, profile)
        return
    directory.mkdir(parents=True, exist_ok=True)
    patch.write_text(_PROFILE_PATCH.format(base_url=base_url.rstrip("/")),
                     encoding="utf-8")
    if not manifest.is_file():
        manifest.write_text(json.dumps(_PROFILE_PACKAGE, indent=2) + "\n",
                            encoding="utf-8")


def _upgrade_bundles(manifest: Path) -> None:
    """Replace the exact bundle tuple an earlier release seeded.

    Profiles from older installs still list only base + web-app, so the
    console would stay bare after upgrading. Only a manifest that still
    carries the old tuple under our profile name is rewritten; any other
    bundle list is left alone.
    """
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("name") != _PROFILE_PACKAGE["name"]:
        return
    pkg = data.get("dsh")
    profile = pkg.get("profile") if isinstance(pkg, dict) else None
    if not isinstance(profile, dict) or profile.get("bundles") != _SEEDED_BUNDLES_V1:
        return
    profile["bundles"] = list(_PROFILE_BUNDLES)
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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


def _bundle_root() -> Path | None:
    """Windows installer bundle: portable node + @deepseek-ai/dsh inside.

    The installer lays out <install>/node/node.exe and installs the CLI into
    <install> itself (dsh.cmd + node_modules/@deepseek-ai/dsh), so no system
    npm install is needed on the target machine. This module sits in
    <install>/runtime/Lib/site-packages/facet/gateway/, so one walk up finds
    the bundle root. Containers keep /usr/local/bin/dsh; this lookup never
    matches there (no node/node.exe under /usr/local).
    """
    for parent in Path(__file__).resolve().parents:
        if ((parent / "node" / "node.exe").is_file()
                and (parent / "node_modules" / "@deepseek-ai" / "dsh"
                     / "lib" / "bin.js").is_file()):
            return parent
    return None


def find_cli() -> dict:
    """Return {"path", "bin", "version"}; path is the shim, bin the JS entry."""
    candidates: list[Path] = []
    override = os.environ.get("DSH_CLI")
    if override:
        candidates.append(Path(override))
    bundle = _bundle_root()
    if bundle:
        candidates.append(bundle / ("dsh.cmd" if sys.platform == "win32" else "dsh"))
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
    bundle = _bundle_root()
    if bundle:
        bundled = bundle / "node" / "node.exe"
        if bundled.is_file():
            return str(bundled)
    return shutil.which("node") or ""


def _command(cli: dict, profile: str, port: int | None) -> list[str]:
    args = ["--profile", profile, "--no-open"]
    if port:
        args.extend(["--port", str(port)])
    # Container deployments: dsh rejects wildcard binds and its printed URL
    # would point at the container's own loopback instead of the published
    # host address, so the entrypoint passes both through the environment.
    host = os.environ.get("DSH_WEB_HOST", "").strip()
    if host:
        args.extend(["--host", host])
    public = os.environ.get("DSH_WEB_PUBLIC_URL", "").strip()
    if public:
        args.extend(["--public-url", public])
        authority = urlsplit(public).netloc
        if authority:
            args.extend(["--trusted-host", authority])
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


def _kill_with_parent(proc: subprocess.Popen) -> object | None:
    """Tie a spawned dsh to the gateway's lifetime (Windows job object).

    The tray stops the gateway with TerminateProcess, so no Python cleanup
    runs; without a kill-on-close job the node child survives, keeps the web
    port, and the next launch dies with EADDRINUSE. Returns the job handle,
    which must stay referenced for as long as the child runs.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        # Popen._handle is an int; anything else (tests hand in Mocks) must
        # never reach ctypes, whose HANDLE conversion recurses on it.
        handle = getattr(proc, "_handle", None)
        if not isinstance(handle, int):
            return None
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(handle)):
            return None
        return job
    except Exception:
        return None


def _port_owner_pid(port: int) -> int | None:
    """PID currently listening on *port* (netstat keeps this dependency-free)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP":
            continue
        if fields[-2].upper() != "LISTENING":
            continue
        if fields[1].rsplit(":", 1)[-1] == str(port):
            return int(fields[-1]) if fields[-1].isdigit() else None
    return None


def _is_dsh_process(pid: int) -> bool:
    """True when *pid* is a node process running the dsh CLI."""
    script = ("(Get-CimInstance Win32_Process -Filter \"ProcessId={}\")"
              ".CommandLine").format(pid)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    command = result.stdout.strip().lower()
    return "dsh" in command and "node" in command


def _release_port(port: int) -> bool:
    """Kill a stale dsh left listening on *port* by a previous Facet run.

    Only a node process whose command line runs dsh is touched, so an
    unrelated app that happens to use the port is left alone.
    """
    pid = _port_owner_pid(port)
    if pid is None or not _is_dsh_process(pid):
        return False
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    # Give Windows a moment to close the listening socket before retrying.
    time.sleep(0.5)
    return True


def _launch(cli: dict, profile: str, port: int | None,
            api_key: str | None) -> dict:
    """Spawn the profile and wait for its tokenised URL."""
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
    entry = {"proc": proc, "url": "", "log": [], "error": "",
             "job": _kill_with_parent(proc)}
    with _lock:
        _runs[profile] = entry
    thread = threading.Thread(target=_pump, args=(entry,), daemon=True)
    thread.start()
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if entry.get("url") or proc.poll() is not None:
            break
        time.sleep(0.2)
    return entry


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
            return _explain_failure(text)
    for line in reversed(entry["log"]):
        text = line.strip()
        if text and not text.startswith("at ") and "code:" not in text:
            return text
    return f"dsh 启动失败（退出码 {returncode}）"


def _explain_failure(text: str) -> str:
    """Translate the EADDRINUSE crash into what the operator should do.

    The raw English line is kept so start_web can still find the blocked
    port and clear the stale dsh before retrying.
    """
    busy = PORT_IN_USE.search(text)
    if busy:
        return (f"端口 {busy.group(1)} 被占用：上次退出时遗留的 dsh 还在运行，"
                f"点「启动 DSH」会自动结束它并重试（{text}）")
    return text


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
    """Locate the `models:` list of the facet provider.

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


def _sync_base_url(base_url: str, profile: str = WEB_PROFILE) -> bool:
    """Point the facet provider at this gateway when its address moved.

    The patch layer is seeded once and hand edits are preserved, so a
    profile created while the gateway listened on another port keeps
    sending DSH to that dead address long after the port changed. The
    facet provider's baseURL is functional wiring rather than a hand-edit
    surface: sync_models already rewrites the models block before every
    launch, and this keeps the address on the same footing. Only the
    baseURL inside the `llm-pi-ai` block is touched; anything else the
    operator edited stays as-is.
    """
    patch = profile_dir(profile) / "cordis.patch.yml"
    try:
        text = patch.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines(keepends=True)
    entry = next((i for i, line in enumerate(lines)
                  if line.strip() == "- id: llm-pi-ai"), None)
    if entry is None:
        return False
    # Top-level entries start at column 0; the provider's list items are
    # indented, so `- id:` without leading spaces ends the block.
    end = next((i for i in range(entry + 1, len(lines))
                if lines[i].startswith("- id:")), len(lines))
    target = next((i for i in range(entry, end)
                   if BASE_URL_LINE.match(lines[i])), None)
    if target is None:
        return False
    # Keep the original line ending: the seeded patch carries CRLF on
    # Windows, and rewriting it as LF would flag an unchanged address as a
    # modification on every launch.
    eol = "\r\n" if lines[target].endswith("\r\n") else "\n"
    lines[target] = (f"{' ' * _indent(lines[target])}"
                     f"baseURL: {base_url.rstrip('/')}/v1{eol}")
    updated_text = "".join(lines)
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
        state["note"] = ("未找到 dsh 命令：DSH 客户端缺失或损坏，重新安装 Facet 即可恢复"
                         "（也可手动 npm install -g @deepseek-ai/dsh）；容器部署没有该客户端。")
    elif not state["profile_exists"]:
        state["note"] = (f"DSH 还没有配置档案 {profile}：点「启动 DSH」会自动创建"
                         f"并指向本网关，无需手动设置。")
    elif entry is not None and not state["running"]:
        state["note"] = entry.get("error") or "上次启动的 dsh 已退出。"
    elif state["running"] and not state["url"]:
        state["note"] = "DSH 正在启动，还没拿到访问地址；等几秒刷新本页即可。"
    return state


def start_web(api_key: str | None = None, profile: str = WEB_PROFILE,
              port: int | None = None, base_url: str | None = None) -> dict:
    """Spawn the DSH web profile and return the tokenised URL.

    The profile prints the URL after its loader settles, so the caller waits
    for it instead of guessing the port. A previously spawned process is
    reused; nothing is opened in a browser here, the console decides that.
    A missing profile is seeded on the spot (ensure_profile), so a fresh
    Windows install can launch DSH without a manual profile step.
    """
    with _start_lock:
        cli = find_cli()
        if not cli["path"]:
            raise DshError("未找到 dsh 命令：DSH 需与网关装在同一台电脑"
                           "（npm install -g @deepseek-ai/dsh）；容器部署没有该客户端")
        try:
            ensure_profile(profile, base_url or DEFAULT_BASE_URL)
        except OSError as exc:
            raise DshError(f"无法创建 dsh profile {profile}：{exc}") from exc
        with _lock:
            existing = _runs.get(profile)
            already = existing is not None and _alive(existing["proc"])
        if already:
            return {**status(profile), "ok": bool(existing.get("url"))}
        entry = _launch(cli, profile, port, api_key)
        if not entry.get("url") and entry["proc"].poll() is not None:
            # A dsh left behind by a previous exit still holds the web port;
            # kill it and retry once so the console's 启动 button self-heals.
            busy = PORT_IN_USE.search(_failure_reason(entry,
                                                      entry["proc"].returncode))
            if busy and _release_port(int(busy.group(1))):
                entry = _launch(cli, profile, port, api_key)
        if not entry.get("url"):
            if entry["proc"].poll() is None:
                entry["error"] = ("dsh 仍在启动中（冷启动较慢），进程已驻留；"
                                  "稍后点上角「刷新」即可看到访问地址")
            else:
                entry["error"] = _failure_reason(entry, entry["proc"].returncode)
            result = {**status(profile), "ok": False}
            result["error"] = entry["error"]
            return result
        return {**status(profile), "ok": True}
