#!/usr/bin/env bash
# mdview 启停脚本（不开终端也能后台常驻）
#
#   ./mdview.sh start     后台启动（默认 8899）
#   ./mdview.sh stop      停止
#   ./mdview.sh restart   重启
#   ./mdview.sh status    是否在跑 / 端口 / 访问地址
#   ./mdview.sh log       跟踪日志（含口令）
#
# 环境变量：MDVIEW_PORT  MDVIEW_ROOT  MDVIEW_ARGS（附加参数，如 --no-auth）

set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/.mdview.pid"
LOG="$DIR/mdview.log"
PORT="${MDVIEW_PORT:-8899}"
ROOT="${MDVIEW_ROOT:-$DIR/docs}"
PY="$(command -v python3)"

running() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
}

start() {
  if running; then
    echo "已经在运行（PID $(cat "$PIDFILE")）"
    return 0
  fi
  mkdir -p "$ROOT"
  echo "启动 mdview: 端口 $PORT，文档目录 $ROOT"
  nohup "$PY" "$DIR/serve.py" --port "$PORT" --root "$ROOT" ${MDVIEW_ARGS:-} >>"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  sleep 0.8
  if running; then
    echo "已启动（PID $(cat "$PIDFILE")），日志：$LOG"
    tail -n 16 "$LOG" || true
  else
    echo "启动失败，看日志：$LOG"
    tail -20 "$LOG" || true
    exit 1
  fi
}

stop() {
  if ! running; then
    echo "没有在运行"
    rm -f "$PIDFILE"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "已停止"
}

status() {
  if running; then
    echo "运行中（PID $(cat "$PIDFILE")），端口 $PORT"
  else
    echo "未运行"
  fi
  ss -lnt 2>/dev/null | grep ":$PORT " || true
  command -v hostname >/dev/null && echo "主机名：$(hostname).local"
  local defif
  defif="$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)"
  echo "可用地址（已过滤 docker/vpn 等虚拟网卡）："
  ip -4 -o addr show scope global 2>/dev/null |
    awk -v p="$PORT" -v d="$defif" '{
      if ($2 ~ /^(docker|br-|veth|virbr|utun|tun|wg|zt)/) next;
      split($4, a, "/");
      mark = ($2 == d) ? "   ← 默认路由，平板大概率用这个" : "";
      printf "  %-10s http://%s:%s/%s\n", $2, a[1], p, mark
    }'
  echo "  提示：平板 Wi-Fi 详情里的 IP 要和上面同一网段；不同网段请用热点兜底"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  log) tail -f "$LOG" ;;
  *) echo "用法：$0 {start|stop|restart|status|log}"; exit 1 ;;
esac
