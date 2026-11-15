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
#   $FACET_HOME/venv    独立虚拟环境（不污染用户 site-packages）
#   $FACET_HOME/data    router.json / keys.json 等状态文件
#   $FACET_HOME/logs    launchd 收集的网关日志
#   ~/Library/LaunchAgents/com.facet.gateway.plist
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

# 1. Python 3.10+
command -v python3 >/dev/null 2>&1 || err "未找到 python3，请先安装 Python 3.10+（brew install python）"
python3 - <<'PY' || err "python3 版本过低，Facet 需要 3.10+"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# 2. venv + 安装（Facet 零第三方依赖，venv 只有标准库+本项目）
log "安装 Facet 到 $VENV（来源：$SRC）"
mkdir -p "$FACET_HOME/data" "$FACET_HOME/logs"
if [ ! -x "$VENV/bin/pip" ]; then
  rm -rf "$VENV"   # 清掉上次失败留下的半成品目录
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --force-reinstall "$SRC"
"$VENV/bin/facet" --help >/dev/null 2>&1 || err "facet 命令不可用，安装失败"

# 3. 渲染并装载 launchd（重装先卸旧）
log "注册 launchd 服务 $LABEL（端口 $FACET_PORT，开机自启 + 崩溃重启）"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__FACET_HOME__|$FACET_HOME|g" \
    -e "s|__FACET_VENV__|$VENV|g" \
    -e "s|__FACET_HOST__|$FACET_HOST|g" \
    -e "s|__FACET_PORT__|$FACET_PORT|g" \
    "$PLIST_SRC" > "$PLIST_DST"
launchctl bootout "gui/$UID_N" "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap "gui/$UID_N" "$PLIST_DST" || launchctl load -w "$PLIST_DST"

# 4. 健康检查
log "等待服务启动（http://$FACET_HOST:$FACET_PORT）…"
for _ in $(seq 1 30); do
  if curl -fsS "http://$FACET_HOST:$FACET_PORT/health" >/dev/null 2>&1; then
    log "Facet 已运行：http://$FACET_HOST:$FACET_PORT/"
    echo "  控制台管理密码默认 admin，首次登录请更换；数据在 $FACET_HOME/data"
    echo "  日志：tail -f $FACET_HOME/logs/gateway.err.log"
    echo "  停止/卸载：tools/macos/uninstall.sh"
    exit 0
  fi
  sleep 1
done
err "30 秒内服务未响应，请查看 $FACET_HOME/logs/gateway.err.log"
