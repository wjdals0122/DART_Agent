#!/usr/bin/env bash
# 평가용 API 서버 기동 — nohup 으로 백그라운드.
#
#   bash serve.sh          기동 (PORT 기본 80)
#   PORT=8080 bash serve.sh
#   bash serve.sh stop     내린다
#   bash serve.sh status    상태
#
# 왜 이 파일이 있나
#  - 임베딩 모델을 매번 huggingface.co 에서 확인하러 간다. 캐시가 있으므로
#    HF_HUB_OFFLINE=1 로 네트워크를 아예 안 치게 막는다.
#  - 프록시 환경변수가 잡히면 HF 가 TLS 검증에 실패해 모델 초기화에서 죽는다.
#  - 중복 기동을 막는다. 두 벌이 뜨면 인덱스가 두 번 올라가 RAM 이 두 배 든다.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${PORT:-80}"
PY="${PY:-python}"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/api.log"
PIDFILE="$ROOT/logs/api.pid"

mkdir -p "$LOG_DIR"

running() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

case "${1:-start}" in
  stop)
    if running; then
      pid="$(cat "$PIDFILE")"
      kill "$pid" && echo "[serve] 내렸다 (pid $pid)"
      rm -f "$PIDFILE"
    else
      echo "[serve] 떠 있지 않다"
    fi
    exit 0
    ;;
  status)
    if running; then
      echo "[serve] 떠 있다 (pid $(cat "$PIDFILE")) · 포트 $PORT"
      curl -s -o /dev/null -w "  /health → %{http_code}\n" "http://127.0.0.1:$PORT/health"
    else
      echo "[serve] 떠 있지 않다"
    fi
    exit 0
    ;;
esac

if running; then
  echo "[serve] 이미 떠 있다 (pid $(cat "$PIDFILE")). 중복 기동을 막는다."
  echo "        내리려면: bash serve.sh stop"
  exit 1
fi

# 네트워크를 안 친다 (캐시가 없으면 preflight 가 먼저 잡아낸다)
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
# 프록시를 명시적으로 비운다 — 남이 잡아 둔 값이 상속되지 않게
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY
export NO_PROXY='*'
# cp949 로 로그가 죽은 전례가 있다
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

echo "[serve] 기동 전 점검"
if ! "$PY" -X utf8 scripts/preflight.py; then
  echo "[serve] 점검에서 막혔다. 서버를 띄우지 않는다."
  exit 1
fi

echo "[serve] 기동 · 포트 $PORT · 로그 $LOG"
nohup "$PY" -X utf8 -m src.api.server --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 1
if running; then
  echo "[serve] pid $(cat "$PIDFILE") 로 떴다."
  echo "        준비까지 35초쯤 걸린다. 그때까지 /answer 는 503 을 준다."
  echo "        확인:  curl -s http://127.0.0.1:$PORT/health"
else
  echo "[serve] 기동 실패. 로그를 봐라: tail -50 $LOG"
  exit 1
fi
