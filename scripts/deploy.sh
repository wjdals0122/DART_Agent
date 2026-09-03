#!/usr/bin/env bash
# 코드만 서버로 올리고 API 를 다시 띄운다. 인덱스·모델캐시는 건드리지 않는다.
#
#   bash scripts/deploy.sh                 # 올리고 재기동 + /health 확인
#   bash scripts/deploy.sh --no-restart    # 올리기만
#   HOST=root@1.2.3.4 PORT=8080 bash scripts/deploy.sh
#
# 비밀번호는 두 번 묻는다(scp 한 번, ssh 한 번). ssh 키를 넣어 두면 안 묻는다:
#   ssh-keygen -t ed25519 -f ~/.ssh/dis044 -N ""
#   ssh-copy-id -i ~/.ssh/dis044.pub root@49.50.141.125
#
# 왜 tar 로 묶어 보내나 — 파일마다 scp 를 부르면 비밀번호를 그 횟수만큼 묻는다.

set -eu

HOST="${HOST:-root@49.50.141.125}"
REMOTE="${REMOTE:-/root/dis-044}"
PORT="${PORT:-80}"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 보내는 것: 코드·설정·프롬프트뿐. data/ corpus/ logs/ .env 는 서버 것을 그대로 둔다.
PACK="$(mktemp -t dis044_code_XXXXXX).tgz"
tar czf "$PACK" \
  --exclude='__pycache__' --exclude='*.pyc' \
  src config prompts eval scripts serve.sh requirements.txt

SIZE="$(du -h "$PACK" | cut -f1)"
echo "[deploy] 묶었다 $SIZE → $HOST:$REMOTE"

scp "$PACK" "$HOST:/tmp/dis044_code.tgz"
rm -f "$PACK"

if [ "$RESTART" = "0" ]; then
  ssh "$HOST" "cd $REMOTE && tar xzf /tmp/dis044_code.tgz && rm -f /tmp/dis044_code.tgz && echo '[deploy] 풀었다 (재기동 안 함)'"
  exit 0
fi

# 원격에서 한 번에: 풀기 → 내리기 → 점검 → 기동 → 준비될 때까지 /health 확인
ssh "$HOST" "PORT=$PORT REMOTE=$REMOTE bash -s" <<'REMOTE_SCRIPT'
set -u
cd "$REMOTE"
tar xzf /tmp/dis044_code.tgz && rm -f /tmp/dis044_code.tgz
echo "[deploy] 코드 갱신 완료"

# venv 가 있으면 쓴다
[ -f venv/bin/activate ] && . venv/bin/activate

bash serve.sh stop || true
PORT="$PORT" bash serve.sh || { echo "[deploy] 기동 실패"; tail -40 logs/api.log; exit 1; }

echo "[deploy] 준비 기다리는 중 (최대 120초)"
for i in $(seq 1 60); do
  code="$(curl -s -o /tmp/h.json -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
  if [ "$code" = "200" ] && grep -q '"ready": *true' /tmp/h.json; then
    echo "[deploy] 준비됐다 ($((i*2))초)"; cat /tmp/h.json; echo; exit 0
  fi
  sleep 2
done
echo "[deploy] 120초 안에 준비되지 않았다. 로그:"
tail -40 logs/api.log
exit 1
REMOTE_SCRIPT

echo "[deploy] 바깥에서 확인"
curl -s -m 10 -o /dev/null -w "  /health → %{http_code}\n" "http://${HOST#*@}:$PORT/health"
