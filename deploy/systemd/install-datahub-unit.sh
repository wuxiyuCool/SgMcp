#!/usr/bin/env bash
# Go 数据机：安装 datahub-server systemd 守护（需 root）
#
#   bash deploy/systemd/install-datahub-unit.sh [INSTALL_DIR] [RUN_USER]
#   默认 INSTALL_DIR=/opt/datahub  RUN_USER=root
#
# 前置：INSTALL_DIR 下放好两样东西——
#   $INSTALL_DIR/datahub-server          （离线包内 bin-linux/datahub-server，chmod +x）
#   $INSTALL_DIR/config/datahub.env      （复制 datahub.env.example 填 token/DSN，可选但强烈建议）
#
# 安装后管理：
#   systemctl status|restart|stop datahub-server
#   journalctl -u datahub-server -f
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${1:-/opt/datahub}"
RUN_USER="${2:-root}"

[ "$(id -u)" -eq 0 ] || { echo "需要 root（写 /etc/systemd/system）"; exit 1; }
[ -x "$DIR/datahub-server" ] || { echo "未找到可执行文件 $DIR/datahub-server（先放置并 chmod +x）"; exit 1; }
if [ ! -f "$DIR/config/datahub.env" ]; then
  echo "警告：$DIR/config/datahub.env 不存在——将以默认值启动（仅监听 127.0.0.1:9300、无鉴权、无 DSN）"
fi

# 覆盖旧进程（此前手工 nohup 起的）
pkill -x datahub-server 2>/dev/null || true
sleep 1

sed -e "s|@APP_DIR@|$DIR|g" -e "s|@MCP_USER@|$RUN_USER|g" \
    "$HERE/datahub-server.service" > /etc/systemd/system/datahub-server.service
systemctl daemon-reload
systemctl enable --now datahub-server
sleep 1
systemctl --no-pager status datahub-server | grep -E "●|Active:|Main PID:"
echo "完成。日志：journalctl -u datahub-server -f"
