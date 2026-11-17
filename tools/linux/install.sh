#!/usr/bin/env bash
# Facet Linux installer: venv 安装 + systemd 系统服务（开机自启 + 崩溃自动重启）。
# 对齐 Windows 安装包 / macOS install.sh 的体验：装完即后台常驻，健康检查通过才算装好。
#
# 用法：
#   tools/linux/install.sh                  # 安装本仓库
#   tools/linux/install.sh <路径|git URL>   # 从其他来源安装
#   FACET_HOME=/opt/facet FACET_PORT=9000 tools/linux/install.sh
#
# 布局：
#   $FACET_HOME/python  独立 CPython（系统 Python 过旧时自动铺设）
#   $FACET_HOME/venv    独立虚拟环境（不污染系统 site-packages）
#   $FACET_HOME/data    router.json / keys.json / usage.json 等状态文件
#   /etc/systemd/system/facet.service   系统服务（注册需要 root）
#
# 无需自备 Python：缺失或版本过旧时自动安装——先用系统包管理器，
# 系统仓库太旧（如 Ubuntu 20.04 / CentOS 7）再自动铺独立 CPython。
#
# 注：普通用户运行即可，脚本只在注册 systemd 服务时请求 sudo。
set -euo pipefail

FACET_HOME="${FACET_HOME:-$HOME/.facet}"
FACET_PORT="${FACET_PORT:-8787}"
FACET_HOST="${FACET_HOST:-127.0.0.1}"
SRC="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
VENV="$FACET_HOME/venv"
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/facet.service"
UNIT_DST=/etc/systemd/system/facet.service
FACET_USER="$(id -un)"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
probe() {   # 端口上有 HTTP 响应（含 401）即返回 0，仅连接失败返回 1
  "$PYTHON" - "$1" <<'PY'
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen(sys.argv[1], timeout=3)
    sys.exit(0)
except urllib.error.HTTPError:
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
}
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

# 0. 特权准备：注册 systemd 服务需要 root
if [ "$(id -u)" = 0 ]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || err "注册 systemd 服务需要 root 权限，且未找到 sudo"
  sudo -v || err "sudo 验证失败，无法注册 systemd 服务"
  SUDO="sudo"
fi

# 1. Python 3.10+：已有就用；没有就自动装，不给用户添思考
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

try_pkg_install() {   # 系统包管理器装 Python（含 venv 支持）
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q python3
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y -q python3
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -Sy --noconfirm --quiet python
  elif command -v zypper >/dev/null 2>&1; then
    $SUDO zypper --non-interactive install python3
  elif command -v apk >/dev/null 2>&1; then
    $SUDO apk add -q python3 py3-pip
  else
    return 1
  fi
}

# 独立 CPython（python-build-standalone）：系统仓库太旧时的兜底，铺到
# $FACET_HOME/python，不碰系统环境；双下载源自动切换（国内优先镜像）。
PY_TAG=20261110
PY_VER=3.12.16
install_standalone_python() {
  local arch file url tarball
  case "$(uname -m)" in
    x86_64)        arch="x86_64" ;;
    aarch64|arm64) arch="aarch64" ;;
    *) err "暂不支持的 CPU 架构 $(uname -m)，请手动安装 Python 3.10+ 后重跑本脚本" ;;
  esac
  file="cpython-$PY_VER+$PY_TAG-$arch-unknown-linux-gnu-install_only.tar.gz"
  tarball="$(mktemp)"
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
  err "独立 CPython 下载失败（网络不通？）。可手动安装 Python 3.10+ 后重跑本脚本"
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  log "未检测到 Python 3.10+，正在自动安装…"
  try_pkg_install || true
  PYTHON="$(find_python || true)"
fi
if [ -z "$PYTHON" ]; then
  log "系统仓库的 Python 版本过旧，改用独立 CPython（不影响系统环境）"
  install_standalone_python
  PYTHON="$FACET_HOME/python/bin/python3"
fi
# 变量名一律用 ${VAR} 包起来：bash 3.2（macOS 自带）会把紧跟的全角字符
# 并进变量名（$PYTHON（ → 变量 "PYTHON（" 不存在），set -u 下直接报错。
log "使用 Python：${PYTHON}（$("$PYTHON" --version 2>&1)）"

# 2. venv + 安装（Facet 零第三方依赖，venv 只有标准库 + 本项目）
log "安装 Facet 到 ${VENV}（来源：${SRC}）"
mkdir -p "$FACET_HOME/data"
if [ ! -x "$VENV/bin/pip" ]; then
  rm -rf "$VENV"   # 清掉上次失败留下的半成品目录
  if ! "$PYTHON" -m venv "$VENV" 2>/dev/null; then
    rm -rf "$VENV"
    # Debian/Ubuntu 把 venv 拆成独立包，缺了就补装后重试一次
    if command -v apt-get >/dev/null 2>&1; then
      $SUDO apt-get install -y -qq python3-venv 2>/dev/null || true
    fi
    "$PYTHON" -m venv "$VENV" 2>/dev/null \
      || { rm -rf "$VENV"; err "创建虚拟环境失败，请安装 venv 支持（Debian/Ubuntu: sudo apt install python3-venv）"; }
  fi
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --force-reinstall "$SRC"
"$VENV/bin/facet" --help >/dev/null 2>&1 || err "facet 命令不可用，安装失败"

# 3. 端口预检：端口已被别的程序占用时提前报错，避免注册后崩溃循环还误报成功
# （重装场景服务自己在跑，跳过预检）。
if ! systemctl is-active --quiet facet 2>/dev/null \
   && probe "http://$FACET_HOST:$FACET_PORT/health"; then
  err "端口 $FACET_PORT 已被其他程序占用（http://$FACET_HOST:${FACET_PORT} 有响应）。请先停掉占用者（ss -ltnp | grep :${FACET_PORT}），或换端口重装：FACET_PORT=9000 tools/linux/install.sh"
fi

# 渲染并注册 systemd 服务（重装覆盖旧服务并重启生效）
log "注册 systemd 服务 facet（$FACET_HOST:${FACET_PORT}，开机自启 + 崩溃自动重启）"
tmp_unit="$(mktemp)"
sed -e "s|__FACET_USER__|$FACET_USER|g" \
    -e "s|__FACET_HOME__|$FACET_HOME|g" \
    -e "s|__FACET_VENV__|$VENV|g" \
    -e "s|__FACET_HOST__|$FACET_HOST|g" \
    -e "s|__FACET_PORT__|$FACET_PORT|g" \
    "$UNIT_SRC" > "$tmp_unit"
$SUDO install -m 644 "$tmp_unit" "$UNIT_DST"
rm -f "$tmp_unit"
$SUDO systemctl daemon-reload
$SUDO systemctl enable facet >/dev/null 2>&1
$SUDO systemctl restart facet

# 4. 健康检查：服务真正 running 且端口有响应才算装好。零密钥首启没有任何
# 启用的渠道，/health 按设计返回 503 all_channels_cooling；probe 不带 -f，
# 任何 HTTP 响应（含 503）都算服务在跑，不能改成 curl -f。
log "等待服务启动（http://$FACET_HOST:${FACET_PORT}）…"
for _ in $(seq 1 30); do
  sub="$(systemctl show -p SubState --value facet 2>/dev/null || true)"
  if [ "$sub" = "running" ] && probe "http://$FACET_HOST:$FACET_PORT/health"; then
    log "Facet 已运行：http://$FACET_HOST:$FACET_PORT/"
    echo "  首启管理密钥只打印一次（journalctl -u facet 搜「管理控制台密钥」），可在控制台「管理密钥」中修改；数据在 $FACET_HOME/data"
    echo "  日志：journalctl -u facet -f"
    echo "  停止：sudo systemctl disable --now facet；卸载：tools/linux/uninstall.sh"
    exit 0
  fi
  sleep 1
done
err "30 秒内服务未就绪（当前状态：${sub:-未知}）。端口被占用时先查 ss -ltnp | grep :${FACET_PORT}，否则查看：journalctl -u facet -xe"
