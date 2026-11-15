#!/usr/bin/env bash
# 卸载 Facet Linux 服务：停 systemd、删服务文件与 venv。
# 默认保留 $FACET_HOME 下的配置与数据（router.json/keys.json 等），
# 传 --purge 才连数据一起删。
set -euo pipefail

FACET_HOME="${FACET_HOME:-$HOME/.facet}"
UNIT_DST=/etc/systemd/system/facet.service

if [ "$(id -u)" = 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

$SUDO systemctl disable --now facet 2>/dev/null || true
$SUDO rm -f "$UNIT_DST"
$SUDO systemctl daemon-reload
rm -rf "$FACET_HOME/venv" "$FACET_HOME/python"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$FACET_HOME"
  echo "Facet 已完全移除（$FACET_HOME 已删除）"
else
  echo "服务已停止并卸载。配置与数据保留在 $FACET_HOME（传 --purge 一并删除）"
fi
