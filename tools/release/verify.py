#!/usr/bin/env python3
"""Verify a Facet release candidate and report the results to the center.

受控发版流程的第二步（dev-plan T4.8）：打包完成之后、管理后台点「发版」
之前，在构建机运行本脚本。它跑全量测试、核对三种产物与版本号一致性，
并把结果上报到中心后台（POST /admin/release/verify）。全部通过后，
后台「发版」按钮才会放行，点击才触发签名发布 CI。

Checks
------
    版本一致性   pyproject.toml == facet.__version__ == docs/release-<v>.md 存在
    全量测试     python -m pytest -q（完整回归）
    pip wheel    dist/facet_models-<v>-py3-none-any.whl 存在
    Docker镜像   docker image inspect facet-router:<v>（--skip-docker 跳过）
    Windows安装包 dist/windows/Facet-Setup-<v>.exe（--skip-installer 跳过）

Usage
    python tools/release/verify.py                      # 本机中心 127.0.0.1:8788
    python tools/release/verify.py --center https://facet.skillgo.cn \
        --token $CENTER_ADMIN_TOKEN --skip-docker       # 服务器中心
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def log(step: str, detail: str = "") -> None:
    print(f"[verify] {step}{(' — ' + detail) if detail else ''}", flush=True)


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("pyproject.toml 里没有 version")
    return match.group(1)


def check_version_consistency(ver: str) -> bool:
    import facet
    ok = True
    if facet.__version__ != ver:
        log("版本不一致", f"pyproject={ver} facet.__version__={facet.__version__}")
        ok = False
    if not (ROOT / "docs" / f"release-{ver}.md").is_file():
        log("缺少 Release 说明", f"docs/release-{ver}.md 不存在")
        ok = False
    return ok


def check_tests() -> bool:
    log("pytest", "运行全量测试…")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(ROOT),
                            capture_output=True, text=True, timeout=3600)
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    log("pytest", tail[0])
    return result.returncode == 0


def check_wheel(ver: str) -> str | None:
    wheel = ROOT / "dist" / f"facet_models-{ver}-py3-none-any.whl"
    if wheel.is_file():
        return wheel.name
    log("wheel 缺失", f"{wheel.relative_to(ROOT)} 不存在，先 python -m pip wheel . -w dist")
    return None


def check_installer(ver: str) -> str | None:
    exe = ROOT / "dist" / "windows" / f"Facet-Setup-{ver}.exe"
    if exe.is_file():
        return exe.name
    log("安装包缺失", f"{exe.relative_to(ROOT)} 不存在，先 python tools/build_windows.py")
    return None


def check_docker(ver: str) -> str | None:
    result = subprocess.run(["docker", "image", "inspect", f"facet-router:{ver}"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        return f"facet-router:{ver}"
    log("docker 镜像缺失", f"facet-router:{ver} 不存在，先 docker build -t facet-router:{ver} .")
    return None


def report(center: str, token: str, payload: dict) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        center.rstrip("/") + "/admin/release/verify", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Admin-Token": token,
                 "User-Agent": "facet-release-verify/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            log("上报完成", f"中心状态: {data.get('status')}")
            return True
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"")[:300].decode("utf-8", "replace")
        log("上报失败", f"HTTP {e.code}: {detail}")
        return False
    except OSError as e:
        log("上报失败", str(e))
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--center", default="http://127.0.0.1:8788",
                        help="中心服务地址（默认 http://127.0.0.1:8788）")
    parser.add_argument("--token", default=None,
                        help="管理 token（默认取环境变量 CENTER_ADMIN_TOKEN）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest（快速重报）")
    parser.add_argument("--skip-docker", action="store_true", help="跳过 Docker 镜像检查")
    parser.add_argument("--skip-installer", action="store_true", help="跳过 Windows 安装包检查")
    parser.add_argument("--no-report", action="store_true", help="只本地检查，不上报中心")
    args = parser.parse_args()

    ver = version()
    log("版本", ver)
    artifacts: list[str] = []
    results: dict[str, bool] = {}

    results["版本一致性"] = check_version_consistency(ver)
    if args.skip_tests:
        log("pytest", "--skip-tests，跳过")
        results["全量测试"] = False
    else:
        results["全量测试"] = check_tests()

    wheel = check_wheel(ver)
    results["pip wheel"] = wheel is not None
    if wheel:
        artifacts.append(wheel)
    if args.skip_installer:
        log("安装包", "--skip-installer，跳过")
    else:
        exe = check_installer(ver)
        results["Windows安装包"] = exe is not None
        if exe:
            artifacts.append(exe)
    if args.skip_docker:
        log("docker", "--skip-docker，跳过")
    else:
        image = check_docker(ver)
        results["Docker镜像"] = image is not None
        if image:
            artifacts.append(image)

    passed = all(results.values())
    for name, ok in results.items():
        log(("PASS " if ok else "FAIL ") + name)
    log("结论", "全部通过" if passed else "存在失败项，不能发版")

    if args.no_report:
        return 0 if passed else 1
    token = args.token
    if token is None:
        import os
        token = os.environ.get("CENTER_ADMIN_TOKEN", "")
    if not token:
        log("未配置 token", "用 --token 或环境变量 CENTER_ADMIN_TOKEN 提供中心管理 token")
        return 2
    if not artifacts:
        log("没有产物", "至少要有一个构建产物（wheel/安装包/docker 镜像）才能上报")
        return 2
    report(args.center, token, {
        "version": ver, "artifacts": artifacts, "results": results,
        "notes": "tools/release/verify.py @ 构建机",
    })
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
