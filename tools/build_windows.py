#!/usr/bin/env python3
"""Build the Windows installer for Facet.

Output
------
    dist/windows/Facet-Setup-<version>.exe   what the user runs
    dist/windows/Facet/                      the same tree, for smoke tests

Bundle layout (what the installer lays down, per user, no admin needed):

    Facet/
      runtime/            embedded CPython 3.11 (stdlib only, no pip needed)
      app/                gateway entry, tray, icon
      data/               router.json / keys.json / logs

Why an embedded Python instead of PyInstaller: the gateway is stdlib-only
(see pyproject `description`), so freezing buys nothing; a real CPython keeps
`python.exe app\\...` behaving exactly like the source checkout.

Build dependencies on the build machine (not on the user's machine):
    Inno Setup 6/7           (found on PATH, in its default folder, or the
                              installer is fetched into .cache/windows/inno)

Usage
    python tools/build_windows.py                 # full build + installer
    python tools/build_windows.py --skip-installer
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "windows"
BUILD = ROOT / "build" / "windows"
APP = BUILD / "Facet"
DIST = ROOT / "dist" / "windows"
RUNTIME = APP / "runtime"
SITE = RUNTIME / "Lib" / "site-packages"

PY_EMBED = "3.11.9"
EMBED_URL = f"https://www.python.org/ftp/python/{PY_EMBED}/python-{PY_EMBED}-embed-amd64.zip"
# 控制台「使用文档」浮窗与新手向导引用的三份手册（ui.py 的 wheel 候选路径
# 是 site-packages/facet/gateway/docs/，拷到这里即命中，产品代码无需改动；
# Docker 走 COPY docs/ 到 /opt/facet，源码 checkout 走 parents[3]）。
GUIDE_DOCS = ("api-key-guide.md", "user-guide.md", "agent-integration.md")
INNO_URL = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe"
INNO_SEARCH = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 7\ISCC.exe"),
]
# DSH CLI bundled into the installer so 启动 DSH works on machines that never
# installed node (the Docker counterpart ships the same two in the image).
# dsh's engines field wants >=24.9.0, and its lib/bin.js entry relies on
# `import.meta.main` (added in node 24.2.0 / 22.18.0) — older node exits
# silently, which used to make the bundled CLI a no-op. Pin a current 24.x.
DSH_VERSION = "0.2.2-rc.1"
NODE_VERSION = "24.22.3"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"


def log(step: str, detail: str = "") -> None:
    print(f"[windows] {step}{(' — ' + detail) if detail else ''}", flush=True)


def download(url: str, dest: Path) -> Path:
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log("download", url)
    request = urllib.request.Request(url, headers={"User-Agent": "facet-build/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=300) as response, \
                    open(dest, "wb") as out:
                shutil.copyfileobj(response, out)
            return dest
        except Exception as exc:  # noqa: BLE001 — retry transient network errors
            log("download failed", f"attempt {attempt + 1}: {exc}")
            time.sleep(3)
    raise SystemExit(f"download failed: {url}")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, text=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd)}\n"
                         f"{(result.stdout or '')[-2000:]}{(result.stderr or '')[-2000:]}")
    return result


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def prepare_runtime() -> None:
    """Embedded CPython + package index on sys.path, no pip needed at runtime."""
    if RUNTIME.is_dir():
        shutil.rmtree(RUNTIME)
    archive = download(EMBED_URL, CACHE / f"python-{PY_EMBED}-embed-amd64.zip")
    RUNTIME.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(RUNTIME)
    pth = next(RUNTIME.glob("python*._pth"), None)
    if pth is None:
        raise SystemExit("embedded python: python*._pth missing")
    lines = [line for line in pth.read_text(encoding="utf-8").splitlines()]
    if "Lib\\site-packages" not in lines:
        lines.append("Lib\\site-packages")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("runtime", f"CPython {PY_EMBED} embedded at {RUNTIME.relative_to(ROOT)}")


def copy_guide_docs() -> None:
    """Bundle the console manuals (+ referenced screenshots) into the wheel tree.

    Must run after every pip install: `pip install --target --upgrade`
    rewrites the facet package directory, so a docs copy from a previous
    build would be wiped. Images are collected from the manuals themselves
    (about 90 files) instead of shipping the whole docs/images tree.
    """
    dest_root = SITE / "facet" / "gateway" / "docs"
    docs = ROOT / "docs"
    bundled = [name for name in GUIDE_DOCS if (docs / name).is_file()]
    for name in bundled:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(docs / name, dest_root / name)
    pattern = re.compile(r'(?:!\[[^\]]*\]\(|src="|src=)\s*(images/[^)\s"]+)')
    images: set[str] = set()
    for name in bundled:
        images.update(pattern.findall((dest_root / name).read_text(encoding="utf-8")))
    missing = []
    for rel in sorted(images):
        src = docs / rel
        if not src.is_file():
            missing.append(rel)
            continue
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    log("guide docs", f"{len(bundled)} manuals, {len(images)} images"
        + (f" ({len(missing)} missing: {', '.join(missing[:3])}...)" if missing else ""))


def install_project() -> None:
    """Just the local wheel: code + static assets, no third-party deps."""
    SITE.mkdir(parents=True, exist_ok=True)
    # --no-deps 保证 venv 里没有第三方依赖（产品只用标准库）。不能加
    # --no-build-isolation：那会让 pip 用宿主 Python 自带的 setuptools，
    # 旧版读不了 PEP 639 的 license = "Apache-2.0" 字符串，元数据生成会失败；
    # 隔离环境才会按 build-system.requires 装 setuptools>=77。
    run([sys.executable, "-m", "pip", "install", "--upgrade",
         "--no-warn-script-location", "--target", str(SITE),
         "--no-deps", str(ROOT)])
    copy_guide_docs()


def install_packages() -> None:
    """The gateway (stdlib only) plus what the tray imports."""
    install_project()
    run([sys.executable, "-m", "pip", "install", "--upgrade",
         "--no-warn-script-location", "--target", str(SITE),
         "pystray", "pillow"])
    check = [str(RUNTIME / "python.exe"), "-c",
             "import facet.gateway, facet, pystray, PIL; "
             "print('facet.gateway', facet.gateway.__file__)"]
    result = run(check, capture_output=True, env={**os.environ, "PYTHONPATH": ""})
    log("packages", result.stdout.strip())


def make_icon() -> Path:
    """Installer, tray and web tab share one icon: the checked-in static asset.

    Regenerating it by hand (draw()) used to drift from the web favicon, so the
    ico now lives once in src/facet/gateway/static/favicon.ico and every build
    copies it — changing the icon means changing that one file.
    """
    icon = BUILD / "facet.ico"
    shutil.copy2(ROOT / "src" / "facet" / "gateway" / "static" / "favicon.ico", icon)
    return icon


def copy_app_files(icon: Path) -> None:
    app = APP / "app"
    app.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "tools" / "windows" / "run_gateway.py", app / "run_gateway.py")
    shutil.copy2(ROOT / "tools" / "windows" / "facet_tray.py", app / "facet_tray.py")
    shutil.copy2(ROOT / "tools" / "windows" / "set_port.py", app / "set_port.py")
    shutil.copy2(ROOT / "tools" / "windows" / "open_console.py", app / "open_console.py")
    shutil.copy2(icon, app / "facet.ico")
    shutil.copy2(ROOT / "tools" / "windows" / "stop.cmd", APP / "stop.cmd")
    data = APP / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / ".keep").write_text(
        "Facet keeps router.json, keys.json and logs here.\n",
        encoding="utf-8")
    log("app files", "gateway + tray + data dir")


def _node_version_ok(node: Path) -> bool:
    """Match the extracted node against NODE_VERSION, so a version bump in
    this script never keeps serving a stale directory (node.exe alone exists)."""
    if not (node / "node.exe").is_file():
        return False
    result = subprocess.run([str(node / "node.exe"), "--version"],
                            capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == f"v{NODE_VERSION}"


def prepare_dsh() -> None:
    """Bundle a portable Node plus the @deepseek-ai/dsh CLI.

    The Dockerfile installs both into the image; without this the installer
    would leave 启动 DSH dead on every machine without a system node. npm with
    -g --prefix writes dsh.cmd at the bundle root and the package under
    node_modules/, exactly the layout dsh.py's _bundle_root looks for.
    """
    node = APP / "node"
    package = APP / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    archive = download(NODE_URL, CACHE / f"node-v{NODE_VERSION}-win-x64.zip")
    if not _node_version_ok(node):
        if node.is_dir():
            shutil.rmtree(node)
        node.mkdir(parents=True)
        prefix = f"node-v{NODE_VERSION}-win-x64/"
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue
                target = node / info.filename[len(prefix):]
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    shutil.copyfileobj(zf.open(info), out)
        log("dsh node", f"portable CPython-free node {NODE_VERSION} at {node.relative_to(ROOT)}")
    run([str(node / "node.exe"),
         str(node / "node_modules" / "npm" / "bin" / "npm-cli.js"),
         "install", "-g", "--prefix", str(APP),
         f"@deepseek-ai/dsh@{DSH_VERSION}"])
    if not package.is_file():
        raise SystemExit(f"dsh bundle incomplete: {package} missing")
    log("dsh", f"@deepseek-ai/dsh {DSH_VERSION} bundled at {APP.relative_to(ROOT)}")


def iscc() -> Path | None:
    for candidate in INNO_SEARCH:
        if candidate.is_file():
            return candidate
    found = shutil.which("iscc")
    if found:
        return Path(found)
    cached = CACHE / "inno" / "ISCC.exe"
    if cached.is_file():
        return cached
    return None


def install_inno() -> Path:
    """Inno Setup is a build-time tool; install it per user when missing."""
    existing = iscc()
    if existing:
        return existing
    setup = download(INNO_URL, CACHE / "innosetup-6.7.4.exe")
    log("inno", "installing Inno Setup per user (no admin)")
    subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                    "/CURRENTUSER", f"/DIR={CACHE / 'inno'}"], check=False)
    found = iscc()
    if found:
        return found
    raise SystemExit("Inno Setup not found: install it from https://jrsoftware.org/isdl.php "
                     "and rerun (it is only needed on the build machine), or pass "
                     "--skip-installer")


def build_installer(version_text: str, icon: Path) -> Path:
    compiler = install_inno()
    DIST.mkdir(parents=True, exist_ok=True)
    iss = ROOT / "tools" / "windows" / "facet.iss"
    log("inno compile", str(compiler))
    run([str(compiler), f"/DAppVersion={version_text}", f"/DSourceDir={APP}",
         f"/DOutputDir={DIST}", f"/DIconFile={icon}", "/Qp", str(iss)])
    output = DIST / f"Facet-Setup-{version_text}.exe"
    if not output.is_file():
        raise SystemExit(f"installer not produced: {output}")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-installer", action="store_true",
                        help="assemble dist/windows/Facet but do not compile Setup.exe")
    parser.add_argument("--clean", action="store_true", help="delete dist/windows/Facet first")
    parser.add_argument("--reuse-runtime", action="store_true",
                        help="keep the existing embedded Python and packages (faster reruns)")
    args = parser.parse_args()

    version_text = version()
    log("version", version_text)
    if args.clean and APP.is_dir():
        shutil.rmtree(APP)
    APP.mkdir(parents=True, exist_ok=True)

    # --reuse-runtime only works if the embedded Python already exists
    reuse = args.reuse_runtime and (RUNTIME / "python.exe").is_file()
    if args.reuse_runtime and not reuse:
        log("runtime", "no existing runtime; doing a full build")
    if not reuse:
        prepare_runtime()
        install_packages()
    elif SITE.is_dir():
        # Reuse skips the runtime, not the code: a console tweak must still
        # land in the bundle.
        install_project()
    icon = make_icon()
    copy_app_files(icon)
    prepare_dsh()
    size = sum(p.stat().st_size for p in APP.rglob("*") if p.is_file())
    log("bundle", f"{size / 1024 ** 2:.0f} MB at {APP}")

    if not args.skip_installer:
        output = build_installer(version_text, icon)
        log("installer", f"{output} ({output.stat().st_size / 1024 ** 2:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
