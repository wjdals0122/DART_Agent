"""토큰·비용 안전장치 검증. **LLM 을 한 번도 부르지 않는다.**

  python eval/test_usage.py

가짜 `clova.chat` 으로 루프를 돌려 아래를 확인한다.
  1. 입력·출력 단가(1.25 / 5.00 per 1K)와 예산 계산이 맞는가
  2. usage 가 없는 응답을 0 으로 채우지 않는가 (안 썼다 ≠ 모른다)
  3. 예산을 넘으면 **문항 중간에도** 멈추는가
  4. 낭비 신호(재호출 차단 / error 뒤 재시도 / 6회 한도 / 429)를 세는가
  5. 로그가 한 줄씩 append 되고 다시 읽히는가
  6. run_e2e 의 문항 적재·표본·예상치가 도는가

진짜 `eval/token_log.jsonl` 은 건드리지 않는다 — 임시 파일로 갈아끼운다.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from src.agent import clova, loop  # noqa: E402
from src.agent import usage as U  # noqa: E402

BAR = "=" * 78
_FAILED: list = []


def check(name, cond, detail="") -> None:
    if not cond:
        _FAILED.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def fake_call(i, name="search_disclosure", args=None):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": args or {"query": "매출액"}}}


def reply(tool=False, usage=None, i=1, args=None, **kw):
    """가짜 clova.chat 응답 하나."""
    out = {"ok": True, "text": "" if tool else "답변 본문",
           "tool_calls": [fake_call(i, args=args)] if tool else [],
           "finish_reason": "tool_calls" if tool else "stop",
           "usage": usage, "model": "FAKE-1", "elapsed": 0.1,
           "retries": 0, "retry_429": 0, "retry_wait_s": 0.0}
    out.update(kw)
    return out


USAGE = {"promptTokens": 4000, "completionTokens": 1000, "totalTokens": 5000}


def main() -> int:
    real_chat = clova.chat

    # ---------- 1. 단가·예산
    print(f"{BAR}\n1 — 단가(입력 1.25 / 출력 5.00 per 1K)와 예산\n{BAR}")
    check("입력 1,000토큰 = 1.25원", abs(U.won(1000, 0) - 1.25) < 1e-9,
          f"{U.won(1000, 0)}원")
    check("출력 1,000토큰 = 5.00원", abs(U.won(0, 1000) - 5.0) < 1e-9,
          f"{U.won(0, 1000)}원")
    check("출력이 입력의 4배", abs(U.won(0, 1000) / U.won(1000, 0) - 4.0) < 1e-9)
    check("총 토큰만으로는 값을 내지 않는다", U.won(50_000, None) is None
          and U.won(None, None) is None)
    per_item = U.won(U.DEFAULT_PROMPT_PER_ITEM, U.DEFAULT_COMPLETION_PER_ITEM)
    check("기본 가정 문항 1건 = 81.25원", abs(per_item - 81.25) < 1e-9,
          f"입력 45,000 + 출력 5,000 → {per_item}원")
    check("40문항 = 3,250원", abs(per_item * 40 - 3250.0) < 1e-6,
          U.fmt_won(per_item * 40))

    b = U.Budget(limit_won=5000)
    b.add(1_000_000, 0)                       # 1,250원
    check("상한 미만이면 exceeded=False", not b.exceeded(), b.line())
    b.add(2_000_000, 250_000)                 # +2,500 +1,250 = 5,000원
    check("상한 넘으면 exceeded=True", b.exceeded(), b.line())
    b.add(1000, 0)
    check("remaining 이 음수로 정확히 나온다", b.remaining_won() < 0,
          f"{b.remaining_won():.2f}원")

    # ---------- 2. usage 결측을 0 으로 채우지 않는다
    print(f"\n{BAR}\n2 — usage 가 없는 호출\n{BAR}")
    p = U.parse_usage(None)
    check("total=None", p["total"] is None, str(p))
    check("missing=True", p["missing"])
    check("내역이 반쪽이면 missing=True",
          U.parse_usage({"totalTokens": 5000})["missing"] is True,
          "총 토큰만 알면 금액을 못 낸다(단가가 4배 다르다)")
    b2 = U.Budget(limit_won=100)
    b2.add(None, None)
    check("예산에 0 이 더해지지 않는다", b2.tokens == 0 and b2.unknown_calls == 1,
          b2.line())
    p2 = U.parse_usage({"promptTokens": 7, "completionTokens": 3})
    check("totalTokens 가 없으면 prompt+completion", p2["total"] == 10, str(p2))

    # ---------- 3. 로그 append / 읽기 (임시 파일)
    print(f"\n{BAR}\n3 — 로그 append 와 재읽기\n{BAR}")
    real_log = U.LOG_PATH
    with tempfile.TemporaryDirectory() as td:
        U.LOG_PATH = Path(td) / "t.jsonl"
        U.set_enabled(True)
        U.use_budget(None)
        U.set_context(script="테스트", qid="Q1")
        r1 = U.record(model="FAKE-1", usage_dict=USAGE)
        U.set_context(script="테스트", qid="Q2")
        U.record(model="FAKE-1", usage_dict=None, ok=False, retries=2, retry_429=2,
                 retry_wait_s=12.0, note="429")
        rows = U.read_log()
        check("두 줄이 남았다", len(rows) == 2, str(len(rows)))
        keys = {"ts", "script", "qid", "model", "prompt_tokens",
                "completion_tokens", "total", "est_won"}
        check("요구된 키가 모두 있다", keys <= set(rows[0]),
              str(sorted(keys - set(rows[0]))))
        check("est_won 이 단가와 맞는다", abs(r1["est_won"] - 10.0) < 1e-6,
              f"{r1['est_won']}원 (입력 4,000×1.25/1K + 출력 1,000×5/1K)")
        check("usage 없는 줄은 total=null", rows[1]["total"] is None)
        check("429 재시도가 기록된다", rows[1]["retry_429"] == 2)
        avg = U.avg_split_per_item()
        check("문항 평균은 아는 것만 센다",
              avg == {"prompt": 4000.0, "completion": 1000.0, "n": 1}, str(avg))
    U.LOG_PATH = real_log
    U.set_enabled(False)          # 아래 루프 테스트는 진짜 로그를 건드리지 않는다

    # ---------- 4. 예산 초과 시 루프가 문항 중간에 멈춘다
    print(f"\n{BAR}\n4 — 예산 초과 시 즉시 중단\n{BAR}")
    n_chat = {"n": 0}

    def chat_forever_tools(messages, **kw):
        n_chat["n"] += 1
        return reply(tool=True, usage=USAGE, i=n_chat["n"],
                     args={"query": f"검색어{n_chat['n']}"})

    clova.chat = chat_forever_tools
    U.use_budget(U.Budget(limit_won=12.0))       # 호출당 10원 → 2회째 진입에서 도달
    res = loop.answer("질문", max_tool_calls=6, deadline=60)
    clova.chat = real_chat
    check("stopped_by_budget=True", res["stopped_by_budget"] is True)
    check("LLM 호출이 2회에서 끊겼다", res["usage"]["llm_calls"] == 2,
          f"{res['usage']['llm_calls']}회 (1회 뒤 상한 도달, 다음 턴 진입에서 중단)")
    check("note 에 상한 초과가 남는다", "비용 상한" in (res["note"] or ""),
          (res["note"] or "")[:60])
    check("answer 는 비지 않는다(계약)", bool(res["answer"].strip()))

    # ---------- 5. 낭비 신호
    print(f"\n{BAR}\n5 — 낭비 신호\n{BAR}")
    n2 = {"n": 0}

    def chat_same_args(messages, **kw):
        n2["n"] += 1
        if n2["n"] <= 4:
            return reply(tool=True, usage=USAGE, i=n2["n"], args={"query": "같은 것"})
        return reply(tool=False, usage=USAGE)

    clova.chat = chat_same_args
    U.use_budget(U.Budget(limit_won=None))
    res = loop.answer("질문", max_tool_calls=6, deadline=60)
    clova.chat = real_chat
    w = res["waste"]
    check("재호출 차단을 센다", w["repeat_blocked"] >= 3, str(w["repeat_blocked"]))
    check("토큰이 문항 단위로 합산된다",
          res["usage"]["total"] == 5000 * res["usage"]["llm_calls"],
          f"{res['usage']['total']:,} / {res['usage']['llm_calls']}회")
    check("추정 비용이 입력·출력을 나눠 계산된다",
          abs(res["usage"]["est_won"] - 10.0 * res["usage"]["llm_calls"]) < 0.01,
          f"{res['usage']['est_won']}원 / {res['usage']['llm_calls']}회 × 10원")

    n3 = {"n": 0}

    def chat_always_tool(messages, **kw):
        n3["n"] += 1
        return reply(tool=True, usage=USAGE, i=n3["n"],
                     args={"query": f"다른 검색어{n3['n']}"})

    clova.chat = chat_always_tool
    U.use_budget(U.Budget(limit_won=None))
    res = loop.answer("질문", max_tool_calls=6, deadline=60)
    clova.chat = real_chat
    check("도구 6회 한도 도달을 표시한다", res["waste"]["hit_call_limit"] is True)

    def chat_429_then_ok(messages, **kw):
        return reply(tool=False, usage=USAGE, retries=3, retry_429=2,
                     retry_wait_s=18.0)

    clova.chat = chat_429_then_ok
    U.use_budget(U.Budget(limit_won=None))
    res = loop.answer("질문", deadline=60)
    clova.chat = real_chat
    w, n = res["waste"], res["usage"]["llm_calls"]
    # 도구를 한 번도 안 부르는 답변은 loop 가 REVIEW 로 한 번 더 부른다(실측 2회).
    # 그래서 호출 수에 비례해 쌓이는지를 본다 — 상수로 박으면 그 경로를 못 본다.
    check("도구 없는 답변은 REVIEW 때문에 LLM 을 2회 부른다", n == 2, f"{n}회")
    check("429 재시도 횟수를 센다", w["retry_429"] == 2 * n,
          f"{w['retry_429']} (호출 {n}회 × 2)")
    check("대기 시간을 센다", w["retry_wait_s"] == 18.0 * n,
          f"{w['retry_wait_s']}초")
    check("다시 보낸 프롬프트 토큰을 추정한다",
          w["resent_prompt_tokens_est"] == 3 * 4000 * n,
          f"{w['resent_prompt_tokens_est']:,} (재시도 3 × 프롬프트 4,000 × {n}회)")

    def chat_error_then_retry(messages, **kw):
        n = len([m for m in messages if m.get("role") == "tool"])
        if n == 0:
            return reply(tool=True, usage=USAGE, i=1,
                         args={"query": ""})          # query 없음 → 도구 error
        if n == 1:
            return reply(tool=True, usage=USAGE, i=2, args={"query": "고친 검색어"})
        return reply(tool=False, usage=USAGE)

    clova.chat = chat_error_then_retry
    U.use_budget(U.Budget(limit_won=None))
    res = loop.answer("질문", deadline=60)
    clova.chat = real_chat
    check("error 뒤 재시도를 센다", res["waste"]["retry_after_bad"] >= 1,
          str(res["waste"]["retry_after_bad"]))

    U.use_budget(None)

    # ---------- 6. run_e2e 문항 적재·표본·예상치
    print(f"\n{BAR}\n6 — run_e2e 문항 적재 (LLM 안 부름)\n{BAR}")
    import eval.run_e2e as E

    items, warn = E.load_items()
    for m in warn:
        print(f"   [문항] {m}")
    check("문항을 읽었다", len(items) > 0, f"{len(items)}건")
    types = sorted({x["qtype"] for x in items})
    corps = {x["corp"] for x in items}
    print(f"   회사 {len(corps)} · 유형 {types}")
    check("템플릿 파일은 빠졌다",
          all(x["src_file"] != E.TEMPLATE_FILE for x in items))
    check("qid 가 겹치지 않는다", len({x['qid'] for x in items}) == len(items))
    check("ground_truth 가 있다",
          all(x["ground_truth"] for x in items),
          f"빈 것 {sum(1 for x in items if not x['ground_truth'])}건")

    picked = E.sample(items, 12)
    c = {}
    for x in picked:
        c[x["qtype"]] = c.get(x["qtype"], 0) + 1
    check("표본이 12건", len(picked) == 12, str(len(picked)))
    check("유형이 고루 섞인다", max(c.values()) - min(c.values()) <= 1, str(c))
    check("시드가 같으면 같은 표본",
          [x["qid"] for x in E.sample(items, 12)] == [x["qid"] for x in picked])

    pf = E.preflight(40, 5000.0, None)
    check("예상치: 40문항 × (입력 45,000 + 출력 5,000)", pf["total"] == 2_000_000,
          f"입력 {pf['total_prompt']:,} + 출력 {pf['total_completion']:,}")
    check("예상 비용 3,250원", abs(pf["won"] - 3250.0) < 1e-6, U.fmt_won(pf["won"]))
    check("실측이 없으면 measured=False", pf["measured"] is False)
    pf2 = E.preflight(10, 5000.0, {"prompt": 28_000.0, "completion": 2_000.0, "n": 5})
    check("실측 평균이 있으면 그것을 쓴다",
          pf2["total"] == 300_000 and pf2["measured"] is True
          and abs(pf2["won"] - 450.0) < 1e-6,
          f"{pf2['total']:,}토큰 · {U.fmt_won(pf2['won'])}")

    nh = E.numbers_hit("매출액은 148,181,177천원이다", "148,181,177천원 (약 1,481억)")
    check("숫자 힌트가 센다", nh["hit"] >= 1, str(nh))
    nh2 = E.numbers_hit("서술형 답변입니다", "숫자가 없는 서술형 정답")
    check("숫자가 없으면 ratio=None(못 잰다)", nh2["ratio"] is None, str(nh2))

    print(f"\n{BAR}")
    if _FAILED:
        print(f"실패 {len(_FAILED)}건: {_FAILED}")
        return 1
    print("전부 통과 · LLM 실제 호출 0회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
