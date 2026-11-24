#!/usr/bin/env bash
# Facet macOS installer: venv 安装 + launchd 开机自启服务。
# 对齐 Windows 安装包的体验：装完即后台常驻，崩溃自动拉起。
#
# 用法：
#   tools/macos/install.sh                  # 安装本仓库
#   tools/macos/install.sh <路径|git URL>   # 从其他来源安装
#   FACET_HOME=~/facet FACET_PORT=9000 tools/macos/install.sh
#
# 布局：
#   $FACET_HOME/python  独立 CPython（系统 Python 过旧时自动铺设）
#   $FACET_HOME/venv    独立虚拟环境（不污染用户 site-packages）
#   $FACET_HOME/data    router.json / keys.json 等状态文件
#   $FACET_HOME/logs    launchd 收集的网关日志
#   ~/Library/LaunchAgents/com.facet.gateway.plist
#
# 无需自备 Python：缺失或版本过旧时自动安装——有 Homebrew 就用 brew，
# 没有（或 brew 装不上）再自动铺独立 CPython 到 $FACET_HOME/python，不碰系统环境。
set -euo pipefail

FACET_HOME="${FACET_HOME:-$HOME/.facet}"
FACET_PORT="${FACET_PORT:-8787}"
FACET_HOST="${FACET_HOST:-127.0.0.1}"
LABEL="com.facet.gateway"
SRC="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
VENV="$FACET_HOME/venv"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.facet.gateway.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_N="$(id -u)"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
download() {   # download <url> <dst>：curl / wget / python3 三层兜底
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 10 -m 600 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 10 -O "$2" "$1"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$1" "$2" <<'PY'
import sys, urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
  else
    return 1
  fi
}

# 1. Python 3.10+：已有就用；没有就自动装，不给用户添思考
# （macOS 自带的 /usr/bin/python3 是 3.9，版本够不上，会自动走安装分支）
find_python() {
  local c
  for c in python3 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

try_brew_install() {   # Homebrew 装 Python，成功则直接输出解释器路径
  command -v brew >/dev/null 2>&1 || return 1
  brew install python@3.12 >/dev/null 2>&1 || true
  local p
  p="$(brew --prefix python@3.12 2>/dev/null)/bin/python3.12"
  [ -x "$p" ] || return 1
  printf '%s' "$p"
}

# 独立 CPython（python-build-standalone）：没有 Homebrew 时的兜底，铺到
# $FACET_HOME/python，不碰系统环境；双下载源自动切换（国内优先镜像）。
PY_TAG=20261110
PY_VER=3.12.16
install_standalone_python() {
  local arch file url tarball
  case "$(uname -m)" in
    x86_64)        arch="x86_64" ;;
    arm64|aarch64) arch="aarch64" ;;
    *) err "暂不支持的 CPU 架构 $(uname -m)，请手动安装 Python 3.10+ 后重跑本脚本" ;;
  esac
  file="cpython-$PY_VER+$PY_TAG-$arch-apple-darwin-install_only.tar.gz"
  tarball="$(mktemp "${TMPDIR:-/tmp}/facet-python.XXXXXX")"
  for url in \
    "https://registry.npmmirror.com/-/binary/python-build-standalone/$PY_TAG/$file" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/$file"; do
    log "下载独立 CPython ${PY_VER}：$url"
    if download "$url" "$tarball" 2>/dev/null; then
      rm -rf "$FACET_HOME/python"
      mkdir -p "$FACET_HOME"
      tar -xzf "$tarball" -C "$FACET_HOME"   # 展开为 $FACET_HOME/python
      rm -f "$tarball"
      [ -x "$FACET_HOME/python/bin/python3" ] || err "独立 CPython 解压异常"
      return 0
    fi
  done
  rm -f "$tarball"
  err "独立 CPython 下载失败（网络不通？）。可手动安装 Python 3.10+（brew install python@3.12）后重跑本脚本"
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  log "未检测到 Python 3.10+，正在自动安装…"
  PYTHON="$(try_brew_install || true)"
fi
if [ -z "$PYTHON" ]; then
  log "没有可用的 Homebrew Python，改用独立 CPython（不影响系统环境）"
  install_standalone_python
  PYTHON="$FACET_HOME/python/bin/python3"
fi
# 变量名一律用 ${VAR} 包起来：macOS 自带 bash 3.2 会把紧跟的全角字符
# 并进变量名（$PYTHON（ → 变量 "PYTHON（" 不存在），set -u 下直接报错。
log "使用 Python：${PYTHON}（$("$PYTHON" --version 2>&1)）"

# 随包手册：控制台「使用文档」浮窗与新手向导读 venv 里 site-packages 的
# docs/（ui.py 的 wheel 候选路径），pip 安装不带仓库的 docs/，需补拷三份
# 手册与其引用的截图（按引用收集，不整搬 docs/images）。SRC 是 git URL
# 时本地没有 docs/，跳过并保持浮窗降级提示（与旧行为一致）。
# pip 每次重装都会重写 facet 包目录，所以必须在每次 pip install 之后重拷。
install_guide_docs() {
  local dest
  dest="$("$VENV/bin/python" -c 'import facet.gateway as g, pathlib; print(pathlib.Path(g.__file__).parent / "docs")')"
  "$VENV/bin/python" - "${SRC}/docs" "$dest" <<'PY'
import re, shutil, sys
from pathlib import Path
docs, dest = Path(sys.argv[1]), Path(sys.argv[2])
names = ("api-key-guide.md", "user-guide.md", "agent-integration.md")
if not (docs / names[0]).is_file():
    print("  手册跳过：安装来源不含 docs/（git URL 安装），控制台手册走降级提示")
    sys.exit(0)
pattern = re.compile(r'(?:!\[[^\]]*\]\(|src="|src=)\s*(images/[^)\s"]+)')
images, copied = set(), 0
for name in names:
    src = docs / name
    if src.is_file():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / name)
        copied += 1
        images.update(pattern.findall((dest / name).read_text(encoding="utf-8")))
count = 0
for rel in sorted(images):
    src = docs / rel
    if src.is_file():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
        count += 1
print(f"  手册随包：{copied} 份手册，{count} 张截图")
PY
}

# 2. venv + 安装（Facet 零第三方依赖，venv 只有标准库+本项目）
log "安装 Facet 到 ${VENV}（来源：${SRC}）"
mkdir -p "$FACET_HOME/data" "$FACET_HOME/logs"
if [ ! -x "$VENV/bin/pip" ]; then
  rm -rf "$VENV"   # 清掉上次失败留下的半成品目录
  "$PYTHON" -m venv "$VENV" \
    || { rm -rf "$VENV"; err "创建虚拟环境失败（$("$PYTHON" --version 2>&1)），请确认 $PYTHON -m venv 可用"; }
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --force-reinstall "$SRC"
"$VENV/bin/facet" --help >/dev/null 2>&1 || err "facet 命令不可用，安装失败"
install_guide_docs

# 3. 渲染并装载 launchd（重装先卸旧）
log "注册 launchd 服务 ${LABEL}（端口 ${FACET_PORT}，开机自启 + 崩溃重启）"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__FACET_HOME__|$FACET_HOME|g" \
    -e "s|__FACET_VENV__|$VENV|g" \
    -e "s|__FACET_HOST__|$FACET_HOST|g" \
    -e "s|__FACET_PORT__|$FACET_PORT|g" \
    "$PLIST_SRC" > "$PLIST_DST"
launchctl bootout "gui/$UID_N" "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$PLIST_DST" || launchctl load -w "$PLIST_DST"

# 4. 健康检查：零密钥首启没有任何启用的渠道，/health 按设计返回 503
# all_channels_cooling；这里只要端口有 HTTP 响应（退出码 0）就算服务在跑，
# 不能加 -f，否则 503 会被当成失败。
log "等待服务启动（http://$FACET_HOST:${FACET_PORT}）…"
for _ in $(seq 1 30); do
  if curl -sS "http://$FACET_HOST:$FACET_PORT/health" >/dev/null 2>&1; then
    log "Facet 已运行：http://$FACET_HOST:$FACET_PORT/"
    echo "  首启管理密钥只打印一次（见 $FACET_HOME/logs/gateway.log），可在控制台「管理密钥」中修改；数据在 $FACET_HOME/data"
    echo "  日志：tail -f $FACET_HOME/logs/gateway.err.log"
    echo "  停止/卸载：tools/macos/uninstall.sh"
    exit 0
  fi
  sleep 1
done
err "30 秒内服务未响应，请查看 $FACET_HOME/logs/gateway.err.log"
