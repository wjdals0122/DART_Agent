"""토큰·비용 회계. 모든 LLM 호출을 한 파일에 누적하고 예산 상한을 지킨다.

  from src.agent import usage
  usage.set_context(script="run_e2e", qid="CJ제일제당__search_closed")
  usage.use_budget(usage.Budget(limit_won=5000))
  ...
  usage.record(model="HCX-005", usage_dict=res.get("usage"))

【단가 — HCX-005 (사용자 확인, 2026-09-02)】
  입력(프롬프트)  1,000토큰당 1.25원   = 0.00125 원/토큰
  출력(완성)      1,000토큰당 5.00원   = 0.005   원/토큰

**출력이 입력보다 4배 비싸다.** 그래서 총 토큰 하나로 환산하면 안 된다.
프롬프트와 완성을 반드시 나눠서 곱한다. 다른 모델을 쓰게 되면 아래 상수 둘만 고친다.

【둘 중 하나라도 모르면 값을 내지 않는다】
`won()` 은 프롬프트·완성이 **둘 다** 있어야 금액을 돌려준다. 하나만 알거나
`usage` 가 아예 없으면 `None` 이다. 4배 차이 나는 단가를 두고 비율을 가정하면
그 추정은 최대 4배 틀린다. 모르는 것은 모른다고 세고(`unknown_calls`),
합계 옆에 "이 값은 하한이다" 를 붙인다(CLAUDE.md 규칙 1·9).

【로그 파일】
`eval/token_log.jsonl` 에 한 줄씩 append 한다. 읽는 쪽은 `eval/token_log.py`.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "eval" / "token_log.jsonl"

# HCX-005 단가 (1,000토큰당 원)
PRICE_PROMPT_PER_1K = 1.25
PRICE_COMPLETION_PER_1K = 5.00
WON_PER_PROMPT_TOKEN = PRICE_PROMPT_PER_1K / 1000.0        # 0.00125
WON_PER_COMPLETION_TOKEN = PRICE_COMPLETION_PER_1K / 1000.0  # 0.005

# 실행 전 예상치에 쓸 기본값. **실측이 아니라 가정이다** — 한 번이라도 돌리면
# token_log.jsonl 의 실측 평균으로 대체된다. 프롬프트가 압도적으로 크다:
# 시스템 프롬프트 4,000자에 도구 결과가 턴마다 12,000자씩 쌓이고,
# 완성은 loop.MAX_TOKENS=2048 로 묶여 있다.
DEFAULT_PROMPT_PER_ITEM = 45_000
DEFAULT_COMPLETION_PER_ITEM = 5_000

_LOCK = threading.Lock()
_CTX = {"script": None, "qid": None}
_BUDGET = None
_ENABLED = True


def won(prompt=None, completion=None):
    """(프롬프트, 완성) → 추정 원. 하나라도 모르면 None.

    총 토큰만으로는 계산하지 않는다. 출력 단가가 입력의 4배라 비율을 가정하면
    그 값이 최대 4배 틀린다.
    """
    if prompt is None or completion is None:
        return None
    try:
        return (float(prompt) * WON_PER_PROMPT_TOKEN
                + float(completion) * WON_PER_COMPLETION_TOKEN)
    except (TypeError, ValueError):
        return None


def fmt_won(v) -> str:
    return "?원" if v is None else f"{v:,.0f}원"


def price_line() -> str:
    """리포트마다 붙이는 단가 한 줄."""
    return (f"HCX-005 단가: 입력 1,000토큰 {PRICE_PROMPT_PER_1K}원 · "
            f"출력 1,000토큰 {PRICE_COMPLETION_PER_1K}원 (출력이 4배 비싸다)")


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
        self.prompt = 0
        self.completion = 0
        self.calls = 0
        self.unknown_calls = 0          # 프롬프트·완성을 모르던 호출

    @property
    def tokens(self) -> int:
        return self.prompt + self.completion

    @property
    def spent_won(self) -> float:
        return won(self.prompt, self.completion) or 0.0

    def add(self, prompt=None, completion=None) -> None:
        self.calls += 1
        if prompt is None or completion is None:
            self.unknown_calls += 1
            return
        self.prompt += int(prompt)
        self.completion += int(completion)

    def exceeded(self) -> bool:
        return self.limit_won is not None and self.spent_won >= self.limit_won

    def remaining_won(self):
        return None if self.limit_won is None else self.limit_won - self.spent_won

    def line(self) -> str:
        s = (f"입력 {self.prompt:,} + 출력 {self.completion:,} = {self.tokens:,} 토큰 · "
             f"{fmt_won(self.spent_won)} 추정 · {self.calls}회 호출")
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
    **반대는 하지 않는다** — total 만 있고 내역을 모르면 금액을 낼 수 없다.
    """
    u = u or {}
    p = _num(u.get("promptTokens"))
    c = _num(u.get("completionTokens"))
    t = _num(u.get("totalTokens"))
    if t is None and p is not None and c is not None:
        t = p + c
    return {"prompt": p, "completion": c, "total": t,
            "missing": p is None or c is None}


def record(model=None, usage_dict=None, script=None, qid=None, ok=True,
           retries=0, retry_429=0, retry_wait_s=0.0, elapsed=None, note="") -> dict:
    """호출 1건을 예산에 더하고 eval/token_log.jsonl 에 한 줄 남긴다."""
    u = parse_usage(usage_dict)
    est = won(u["prompt"], u["completion"])
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": script or _CTX.get("script") or "?",
        "qid": qid if qid is not None else _CTX.get("qid"),
        "model": model,
        "prompt_tokens": u["prompt"],
        "completion_tokens": u["completion"],
        "total": u["total"],
        "est_won": None if est is None else round(est, 3),
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
        _BUDGET.add(u["prompt"], u["completion"])

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


def avg_split_per_item(script=None, path=None):
    """문항 1건당 실측 평균 {prompt, completion, n}. 근거가 없으면 None.

    같은 qid 의 여러 호출을 한 문항으로 묶는다(한 문항이 도구 왕복으로 3~6회 부른다).
    qid 가 없거나 내역을 모르는 줄은 뺀다 — 기본값을 지어내지 않는다.
    """
    per: dict = {}
    for r in read_log(path):
        if script and r.get("script") != script:
            continue
        qid = r.get("qid")
        p, c = r.get("prompt_tokens"), r.get("completion_tokens")
        if not qid or p is None or c is None:
            continue
        a = per.setdefault(qid, [0, 0])
        a[0] += int(p)
        a[1] += int(c)
    if not per:
        return None
    n = len(per)
    return {"prompt": sum(v[0] for v in per.values()) / n,
            "completion": sum(v[1] for v in per.values()) / n, "n": n}
