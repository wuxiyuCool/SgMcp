#!/usr/bin/env bash
# Python 三层 server 启停控制（Linux 部署用）
#
#   bash scripts/serversctl.sh start     # 后台拉起 common/itops/gateway
#   bash scripts/serversctl.sh stop      # 按 PID 逐个停止
#   bash scripts/serversctl.sh status    # 查看存活与端口
#   bash scripts/serversctl.sh restart   # 先停后起
#
# 可配置环境变量：
#   HOST=0.0.0.0        监听地址（默认 127.0.0.1；对外服务设 0.0.0.0）
#   PYTHON=<路径>       解释器（默认 .venv/bin/python）
#   GATEWAY_PORT 等     各层端口覆盖（GATEWAY_PORT/ITOPS_PORT/COMMON_PORT）
# 日志在 logs/<name>.log，PID 在 run/<name>.pid（均已 gitignore）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
HOST="${HOST:-127.0.0.1}"
LOG_DIR="$ROOT/logs"
RUN_DIR="$ROOT/run"

SERVICES=(
  "common:mcp_common_server.main:${COMMON_PORT:-9100}"
  "itops:mcp_itops.main:${ITOPS_PORT:-9200}"
  "gateway:mcp_gateway.main:${GATEWAY_PORT:-9000}"
)

mkdir -p "$LOG_DIR" "$RUN_DIR"
export NO_PROXY='*'

pid_of() {
  local pidfile="$RUN_DIR/$1.pid"
  [ -f "$pidfile" ] && cat "$pidfile" || true
}

is_up() {
  local pid
  pid="$(pid_of "$1")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start() {
  for svc in "${SERVICES[@]}"; do
    IFS=: read -r name entry port <<<"$svc"
    if is_up "$name"; then
      echo "  [skip] $name 已在运行 (pid $(pid_of $name), :$port)"
      continue
    fi
    if ! [ -x "$PY" ]; then
      echo "  [fail] 未找到解释器 $PY，请先执行 install-offline.sh / install-online.sh"
      exit 1
    fi
    nohup "$PY" -m "$entry" --transport http --host "$HOST" --port "$port" \
      >"$LOG_DIR/$name.log" 2>&1 &
    echo $! >"$RUN_DIR/$name.pid"
    sleep 1
    if is_up "$name"; then
      echo "  [ ok ] $name -> $HOST:$port (pid $(pid_of $name), 日志 logs/$name.log)"
    else
      echo "  [fail] $name 启动失败，查看 logs/$name.log"
      tail -5 "$LOG_DIR/$name.log" | sed 's/^/         /'
    fi
  done
}

stop() {
  # 逆序停：先网关，再业务/通用层
  for i in 2 1 0; do
    IFS=: read -r name entry port <<<"${SERVICES[$i]}"
    pid="$(pid_of "$name")"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
      echo "  [skip] $name 未在运行"
      rm -f "$RUN_DIR/$name.pid"
      continue
    fi
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    rm -f "$RUN_DIR/$name.pid"
    echo "  [ ok ] $name 已停止 (原 pid $pid)"
  done
}

status() {
  for svc in "${SERVICES[@]}"; do
    IFS=: read -r name entry port <<<"$svc"
    if is_up "$name"; then
      echo "  [ UP   ] $name  pid=$(pid_of $name)  port=$port"
    else
      echo "  [ DOWN ] $name"
    fi
  done
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  *)       echo "用法: bash scripts/serversctl.sh {start|stop|restart|status}"; exit 2 ;;
esac
