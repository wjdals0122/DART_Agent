"""토큰·비용 회계. 모든 LLM 호출을 한 파일에 누적하고 예산 상한을 지킨다.

  from src.agent import usage
  usage.set_context(script="run_e2e", qid="CJ제일제당__search_closed")
  usage.use_budget(usage.Budget(limit_won=5000))
  ...
  usage.record(model="HCX-005", usage_dict=res.get("usage"))

【단가 — 확인된 값이 아니라 사람이 정한 환산 기준이다】
2,000,000 토큰 = 4,800원 으로 환산한다(사용자 지정, 2026-09-02).
HyperCLOVA X 문서·상품 페이지 어디에도 단가가 없어 실제 요금표는 확인하지 못했다.
프롬프트/완성 토큰 단가를 나누지 않고 **총 토큰**에 한 단가를 적용한다.
바꾸려면 아래 상수 둘만 고치면 된다. 리포트에는 언제나 '추정' 이라고 적는다.

【usage 가 없는 호출은 0 으로 채우지 않는다】
응답에 usage 가 없으면 토큰을 `null` 로 기록하고 `usage_missing=true` 를 남긴다.
0 으로 채우면 "안 썼다" 와 "모른다" 가 구분되지 않고, 예산이 조용히 새어 나간다.
집계에서는 별도 항목으로 센다(CLAUDE.md 규칙 1·9).

【로그 파일】
`eval/token_log.jsonl` 에 한 줄씩 append 한다. 읽는 쪽은 `eval/token_log.py`.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "eval" / "token_log.jsonl"

# 환산 기준 (사용자 지정)
TOKENS_PER_UNIT = 2_000_000
WON_PER_UNIT = 4_800.0
WON_PER_TOKEN = WON_PER_UNIT / TOKENS_PER_UNIT          # 0.0024 원/토큰

# 예상치를 낼 때 쓸 기본값. 실측 로그가 있으면 그쪽을 먼저 쓴다.
DEFAULT_TOKENS_PER_ITEM = 50_000

_LOCK = threading.Lock()
_CTX = {"script": None, "qid": None}
_BUDGET = None
_ENABLED = True


def won(tokens) -> float:
    """토큰 수 → 추정 원. 토큰을 모르면 0 을 돌려주되 부르는 쪽이 그걸 알아야 한다."""
    try:
        return float(tokens or 0) * WON_PER_TOKEN
    except (TypeError, ValueError):
        return 0.0


def fmt_won(v) -> str:
    return f"{v:,.0f}원"


# ---------------------------------------------------------------- 실행 맥락


def set_enabled(on: bool) -> None:
    """로그 append 를 끈다. 가짜 LLM 으로 도는 테스트가 비용 로그를 더럽히지 않게.

    끈 상태에서도 record() 는 같은 dict 를 돌려주므로 호출부 계측은 그대로 돈다.
    """
    global _ENABLED
    _ENABLED = bool(on)


def enabled() -> bool:
    return _ENABLED


def set_context(script=None, qid=None) -> None:
    """이후 record() 가 이 값을 기본으로 쓴다. 스크립트 시작할 때 한 번 부른다."""
    if script is not None:
        _CTX["script"] = str(script)
    _CTX["qid"] = None if qid is None else str(qid)


def context() -> dict:
    return dict(_CTX)


# ---------------------------------------------------------------- 예산


class Budget:
    """누적 추정 비용이 상한을 넘으면 exceeded() 가 True 가 된다.

    상한을 넘겼다고 여기서 예외를 던지지 않는다. 호출부가 확인하고 **자기 자리에서**
    멈춰야 어디까지 했는지 기록할 수 있다.
    """

    def __init__(self, limit_won=None) -> None:
        self.limit_won = None if limit_won is None else float(limit_won)
        self.tokens = 0
        self.calls = 0
        self.unknown_calls = 0          # usage 가 없던 호출

    @property
    def spent_won(self) -> float:
        return won(self.tokens)

    def add(self, tokens) -> None:
        self.calls += 1
        if tokens is None:
            self.unknown_calls += 1
            return
        self.tokens += int(tokens)

    def exceeded(self) -> bool:
        return self.limit_won is not None and self.spent_won >= self.limit_won

    def remaining_won(self):
        return None if self.limit_won is None else self.limit_won - self.spent_won

    def line(self) -> str:
        s = f"{self.tokens:,} 토큰 · {fmt_won(self.spent_won)} 추정 · {self.calls}회 호출"
        if self.limit_won is not None:
            s += f" · 상한 {fmt_won(self.limit_won)}"
        if self.unknown_calls:
            s += f" · 토큰 미상 {self.unknown_calls}회"
        return s


def use_budget(b) -> None:
    global _BUDGET
    _BUDGET = b


def active_budget():
    return _BUDGET


# ---------------------------------------------------------------- 기록


def _num(v):
    """usage 값 하나. 숫자가 아니면 None — 0 으로 채우지 않는다."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_usage(u) -> dict:
    """HCX result.usage → {prompt, completion, total, missing}.

    total 이 없으면 prompt+completion 으로 채운다(둘 다 있을 때만).
    셋 다 없으면 missing=True 다.
    """
    u = u or {}
    p = _num(u.get("promptTokens"))
    c = _num(u.get("completionTokens"))
    t = _num(u.get("totalTokens"))
    if t is None and p is not None and c is not None:
        t = p + c
    return {"prompt": p, "completion": c, "total": t,
            "missing": t is None}


def record(model=None, usage_dict=None, script=None, qid=None, ok=True,
           retries=0, retry_429=0, retry_wait_s=0.0, elapsed=None, note="") -> dict:
    """호출 1건을 예산에 더하고 eval/token_log.jsonl 에 한 줄 남긴다."""
    u = parse_usage(usage_dict)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": script or _CTX.get("script") or "?",
        "qid": qid if qid is not None else _CTX.get("qid"),
        "model": model,
        "prompt_tokens": u["prompt"],
        "completion_tokens": u["completion"],
        "total": u["total"],
        "est_won": round(won(u["total"]), 3) if u["total"] is not None else None,
        "ok": bool(ok),
        "retries": int(retries or 0),
        "retry_429": int(retry_429 or 0),
        "retry_wait_s": round(float(retry_wait_s or 0.0), 1),
        "elapsed": elapsed,
    }
    if u["missing"]:
        row["usage_missing"] = True
    if note:
        row["note"] = note

    if _BUDGET is not None:
        _BUDGET.add(u["total"])

    append(row)
    return row


def append(row: dict) -> None:
    """로그 한 줄. 로그를 못 써도 본 작업은 계속돼야 한다 — 실패는 stderr 에 남긴다."""
    if not _ENABLED:
        return
    try:
        with _LOCK:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[usage] 토큰 로그를 못 남겼다({type(e).__name__}: {e}) — "
              f"{LOG_PATH}", file=sys.stderr)


# ---------------------------------------------------------------- 로그 읽기


def read_log(path=None) -> list:
    """로그 전량. 깨진 줄은 버리지 않고 세어서 알린다."""
    p = Path(path or LOG_PATH)
    rows, broken = [], 0
    if not p.is_file():
        return rows
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                broken += 1
    if broken:
        print(f"[usage] {p} 에서 읽지 못한 줄 {broken}개", file=sys.stderr)
    return rows


def avg_tokens_per_item(script=None, path=None):
    """실측 평균 토큰/문항. 근거가 없으면 None — 기본값을 지어내지 않는다.

    같은 qid 의 여러 호출을 한 문항으로 묶는다(한 문항이 도구 왕복으로 3~6회 부른다).
    qid 가 없는 줄은 문항 단위를 알 수 없으므로 뺀다.
    """
    per: dict = {}
    for r in read_log(path):
        if script and r.get("script") != script:
            continue
        qid, tot = r.get("qid"), r.get("total")
        if not qid or tot is None:
            continue
        per[qid] = per.get(qid, 0) + int(tot)
    if not per:
        return None
    return sum(per.values()) / len(per)
