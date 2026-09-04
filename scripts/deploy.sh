#!/usr/bin/env bash
# 코드만 서버로 올리고 API 를 다시 띄운다. 인덱스·모델캐시는 건드리지 않는다.
# 재기동은 systemd(dis044.service)로 한다 — serve.sh 를 직접 부르지 않는다.
#
#   bash scripts/deploy.sh                 # 올리고 systemctl restart + /health 확인
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

# 원격에서 한 번에: 풀기 → systemd 재기동 → 준비될 때까지 is-active + /health 확인
#
# 【serve.sh 를 부르지 않는다 — 2026-09-04】
# 서버는 systemd(dis044.service)가 관리하며 80 포트를 잡고 있다.
# serve.sh 로 또 띄우면 "address already in use" 로 실패한다(실측).
ssh "$HOST" "PORT=$PORT REMOTE=$REMOTE bash -s" <<'REMOTE_SCRIPT'
set -u
cd "$REMOTE"
tar xzf /tmp/dis044_code.tgz && rm -f /tmp/dis044_code.tgz
echo "[deploy] 코드 갱신 완료"

echo "[deploy] systemctl restart dis044"
systemctl restart dis044 || {
  echo "[deploy] restart 실패. journalctl:"
  journalctl -u dis044 -n 40 --no-pager
  exit 1
}

# 준비 판정은 둘 다 본다:
#  - systemctl is-active  — 프로세스가 죽으면 바로 알 수 있다(그때 /health 는 그냥 무응답)
#  - /health 200 + "status": "ok"  — 서버는 준비 전 503 {"status": "starting"} 을 준다.
#    (예전 grep 대상이던 "ready": true 는 /health 응답에 없는 키였다 — 고쳤다)
echo "[deploy] 준비 기다리는 중 (최대 120초)"
for i in $(seq 1 60); do
  if ! systemctl is-active --quiet dis044; then
    echo "[deploy] dis044 가 살아 있지 않다 ($((i*2))초). journalctl:"
    journalctl -u dis044 -n 40 --no-pager
    exit 1
  fi
  code="$(curl -s -o /tmp/h.json -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
  if [ "$code" = "200" ] && grep -q '"status": *"ok"' /tmp/h.json; then
    echo "[deploy] 준비됐다 ($((i*2))초)"; cat /tmp/h.json; echo; exit 0
  fi
  sleep 2
done
echo "[deploy] 120초 안에 준비되지 않았다. journalctl:"
journalctl -u dis044 -n 40 --no-pager
exit 1
REMOTE_SCRIPT

echo "[deploy] 바깥에서 확인"
curl -s -m 10 -o /dev/null -w "  /health → %{http_code}\n" "http://${HOST#*@}:$PORT/health"
