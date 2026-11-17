#!/usr/bin/env bash
# 卸载 Facet macOS 服务：停 launchd、删 plist 与 venv。
# 默认保留 $FACET_HOME 下的配置与日志（router.json/keys.json 等），
# 传 --purge 才连数据一起删。
set -euo pipefail

FACET_HOME="${FACET_HOME:-$HOME/.facet}"
LABEL="com.facet.gateway"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_N="$(id -u)"

launchctl bootout "gui/$UID_N" "$PLIST_DST" 2>/dev/null || true
rm -f "$PLIST_DST"
rm -rf "$FACET_HOME/venv" "$FACET_HOME/python"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$FACET_HOME"
  echo "Facet 已完全移除（$FACET_HOME 已删除）"
else
  echo "服务已停止并卸载。配置与日志保留在 ${FACET_HOME}（传 --purge 一并删除）"
fi
