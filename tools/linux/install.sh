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
#   $FACET_HOME/venv    独立虚拟环境（不污染系统 site-packages）
#   $FACET_HOME/data    router.json / keys.json / usage.json 等状态文件
#   /etc/systemd/system/facet.service   系统服务（注册需要 root）
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
  python3 - "$1" <<'PY'
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

# 0. 特权准备：注册 systemd 服务需要 root
if [ "$(id -u)" = 0 ]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || err "注册 systemd 服务需要 root 权限，且未找到 sudo"
  sudo -v || err "sudo 验证失败，无法注册 systemd 服务"
  SUDO="sudo"
fi

# 1. Python 3.10+
command -v python3 >/dev/null 2>&1 \
  || err "未找到 python3，请先安装 Python 3.10+（Debian/Ubuntu: sudo apt install python3 python3-venv）"
python3 - <<'PY' || err "python3 版本过低，Facet 需要 3.10+"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# 2. venv + 安装（Facet 零第三方依赖，venv 只有标准库 + 本项目）
log "安装 Facet 到 $VENV（来源：$SRC）"
mkdir -p "$FACET_HOME/data"
if [ ! -x "$VENV/bin/pip" ]; then
  rm -rf "$VENV"   # 清掉上次失败留下的半成品目录
  python3 -m venv "$VENV" 2>/dev/null \
    || { rm -rf "$VENV"; err "创建虚拟环境失败，请安装 venv 支持（Debian/Ubuntu: sudo apt install python3-venv）"; }
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --force-reinstall "$SRC"
"$VENV/bin/facet" --help >/dev/null 2>&1 || err "facet 命令不可用，安装失败"

# 3. 端口预检：端口已被别的程序占用时提前报错，避免注册后崩溃循环还误报成功
# （重装场景服务自己在跑，跳过预检）。
if ! systemctl is-active --quiet facet 2>/dev/null \
   && probe "http://$FACET_HOST:$FACET_PORT/health"; then
  err "端口 $FACET_PORT 已被其他程序占用（http://$FACET_HOST:$FACET_PORT 有响应）。请先停掉占用者（ss -ltnp | grep :$FACET_PORT），或换端口重装：FACET_PORT=9000 tools/linux/install.sh"
fi

# 渲染并注册 systemd 服务（重装覆盖旧服务并重启生效）
log "注册 systemd 服务 facet（$FACET_HOST:$FACET_PORT，开机自启 + 崩溃自动重启）"
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

# 4. 健康检查：服务真正 running 且端口有响应才算装好
log "等待服务启动（http://$FACET_HOST:$FACET_PORT）…"
for _ in $(seq 1 30); do
  sub="$(systemctl show -p SubState --value facet 2>/dev/null || true)"
  if [ "$sub" = "running" ] && probe "http://$FACET_HOST:$FACET_PORT/health"; then
    log "Facet 已运行：http://$FACET_HOST:$FACET_PORT/"
    echo "  控制台管理密码默认 admin，首次登录请更换；数据在 $FACET_HOME/data"
    echo "  日志：journalctl -u facet -f"
    echo "  停止：sudo systemctl disable --now facet；卸载：tools/linux/uninstall.sh"
    exit 0
  fi
  sleep 1
done
err "30 秒内服务未就绪（当前状态：${sub:-未知}）。端口被占用时先查 ss -ltnp | grep :$FACET_PORT，否则查看：journalctl -u facet -xe"
