#!/usr/bin/env bash
# 安装并启用 systemd 守护（需 root）：mcp-common / mcp-itops / mcp-gateway
#
#   bash deploy/systemd/install-units.sh [APP_DIR] [RUN_USER]
#   默认 APP_DIR=/opt/mcp/sgmcp-deploy/app  RUN_USER=root
#
# 安装后日常管理：
#   systemctl status|restart|stop mcp-gateway
#   journalctl -u mcp-gateway -f
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${1:-/opt/mcp/sgmcp-deploy/app}"
RUN_USER="${2:-root}"

[ "$(id -u)" -eq 0 ] || { echo "需要 root（写 /etc/systemd/system）"; exit 1; }
[ -x "$APP_DIR/.venv/bin/python" ] || { echo "未找到 $APP_DIR/.venv/bin/python，请先完成安装依赖"; exit 1; }

# 停掉 serversctl 方式拉起的旧进程，避免端口冲突
if [ -d "$APP_DIR/run" ]; then
  (cd "$APP_DIR" && bash scripts/serversctl.sh stop) || true
fi

for u in mcp-common mcp-itops mcp-gateway; do
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@MCP_USER@|$RUN_USER|g" \
      "$HERE/$u.service" > "/etc/systemd/system/$u.service"
  echo "已写入 /etc/systemd/system/$u.service"
done

systemctl daemon-reload
systemctl enable mcp-common mcp-itops mcp-gateway
systemctl restart mcp-common mcp-itops mcp-gateway
sleep 2
systemctl --no-pager --full status mcp-common mcp-itops mcp-gateway 2>/dev/null | grep -E "●|Active:"
echo "完成。开机自启+崩溃自动拉起已生效；日志：journalctl -u mcp-gateway -f"
