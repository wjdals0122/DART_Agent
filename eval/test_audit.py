"""최종 답변 검문 검증. **LLM 을 부르지 않는다** (clova.chat 을 가짜로 갈아끼운다).

  python eval/test_audit.py

검문 1 — 답변에 비율·배수가 있는데 calculate 를 안 불렀다
검문 2 — 아무것도 조회하지 않고 "확인되지 않는다" 고 했다

여기서 확인하는 것
  - 판정 함수가 맞는 것만 잡는가(공시에서 베낀 비율은 안 잡는다)
  - 루프가 **딱 한 턴만** 더 도는가. 두 번 이상 돌면 안 된다
  - 추가 턴에서도 같은 상태면 그대로 내보내는가
  - 남은 시간이 8초 미만이면 검문을 건너뛰는가
  - 검문 함수가 어떤 입력에도 예외를 던지지 않는가
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from src.agent import clova, loop                      # noqa: E402
from src.agent import usage as usage_mod               # noqa: E402

usage_mod.set_enabled(False)                           # 비용 로그를 더럽히지 않는다
from src.agent.loop import (all_tools_failed, answer,  # noqa: E402
                            needs_calc, refused_without_search)

FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def doc(text):
    return [{"chunk_id": "c1", "text": text}]


def tc(name):
    return [{"n": 1, "name": name, "status": "ok"}]


print("=" * 74)
print("1 — 검문 1 판정: 비율·배수인데 calculate 를 안 불렀나")
print("=" * 74)

check("만들어 낸 백분율을 잡는다",
      needs_calc("삼성전자가 약 234.73% 더 많다.", tc("search_disclosure"), doc("매출액 300,870,903")))
check("만들어 낸 배수를 잡는다",
      needs_calc("약 4.55배다.", tc("search_disclosure"), doc("매출액 300,870,903")))
check("증가·감소 + 숫자도 잡는다",
      needs_calc("전년 대비 12,345 증가했다.", tc("search_disclosure"), doc("매출 1,000")))

check("문서에서 베낀 비율은 안 잡는다",
      not needs_calc("매출액대비 3.06%다.", tc("search_disclosure"),
                     doc("매출액대비(%) : 3.06")))
check("calculate 를 불렀으면 안 잡는다",
      not needs_calc("약 4.55배다.", tc("calculate"), doc("")))
check("비율 표현이 없으면 안 잡는다",
      not needs_calc("매출액은 300,870,903백만원이다.", tc("search_disclosure"),
                     doc("300,870,903")))
check("'배당' 같은 낱말에 걸리지 않는다",
      not needs_calc("주당 배당금은 1,500원이며 배출량은 별도다.",
                     tc("search_disclosure"), doc("주당 배당금 1,500 배출량")))

print()
print("=" * 74)
print("2 — 검문 2 판정: 조회 없이 거절했나")
print("=" * 74)

check("도구 0회 + 확인되지 않음 → 잡는다",
      refused_without_search("공시에서 확인되지 않습니다.", []))
check("도구 0회 + 확인할 수 없음 → 잡는다",
      refused_without_search("확인할 수 없습니다.", []))
check("도구를 불렀으면 안 잡는다",
      not refused_without_search("확인되지 않습니다.", tc("search_disclosure")))
check("거절이 아니면 안 잡는다",
      not refused_without_search("매출액은 300,870,903백만원이다.", []))

print()
print("=" * 74)
print("2-B — 검문 4 판정: 부른 도구가 전부 빗나갔는데 거절하나")
print("=" * 74)


def bad(name, status="not_found"):
    return [{"n": 1, "name": name, "status": status}]


check("전부 not_found + 거절 → 잡는다",
      refused := all_tools_failed("공시에서 찾을 수 없습니다.", bad("search_disclosure")))
check("전부 error + 거절 → 잡는다",
      all_tools_failed("확인되지 않습니다.", bad("list_disclosures", "error")))
check("섞여 있어도 ok 가 하나라도 있으면 안 잡는다",
      not all_tools_failed("찾을 수 없습니다.",
                           bad("search_disclosure") + tc("search_disclosure")))
check("도구 0회면 안 잡는다(검문 2 의 몫)",
      not all_tools_failed("확인되지 않습니다.", []))
check("거절이 아니면 안 잡는다",
      not all_tools_failed("매출액은 300,870,903백만원이다.", bad("search_disclosure")))
check("'확인할 수 없' 도 거절로 본다",
      all_tools_failed("확인할 수 없습니다.", bad("search_disclosure")))

# 검문 2 와 4 는 조건이 배타적이다 — 저쪽은 도구 0회, 이쪽은 1회 이상
check("검문 2·4 가 동시에 참이 되지 않는다",
      not (refused_without_search("확인되지 않습니다.", bad("search_disclosure"))
           and all_tools_failed("확인되지 않습니다.", [])))

print()
print("=" * 74)
print("3 — 지저분한 입력에도 예외를 안 던진다")
print("=" * 74)

for args in ((None, None, None), ("", [], []), ("4.5배", [None], [None]),
             ("%", [{"name": None}], [{"text": None}])):
    try:
        needs_calc(*args)
        ok = True
    except Exception as e:                     # noqa: BLE001
        ok = False
        print("   ", type(e).__name__, e)
    check(f"needs_calc{args!r:.44s} 예외 없음", ok)
for args in ((None, None), ("", []), ("확인되지 않", None)):
    try:
        refused_without_search(*args)
        ok = True
    except Exception:                          # noqa: BLE001
        ok = False
    check(f"refused_without_search{args!r:.30s} 예외 없음", ok)

print()
print("=" * 74)
print("4 — 루프가 딱 한 턴만 더 돈다")
print("=" * 74)

real_chat = clova.chat


def run_with(texts, tools_each_turn=None):
    """turn 마다 정해진 답을 돌려주는 가짜 LLM. (결과, 주고받은 messages)"""
    state = {"i": 0, "seen": []}

    def fake(messages, **kw):
        state["seen"].append([m.get("content") for m in messages])
        i = state["i"]
        state["i"] += 1
        t = texts[min(i, len(texts) - 1)]
        return {"ok": True, "text": t, "tool_calls": [], "finish_reason": "stop",
                "usage": {"promptTokens": 1, "completionTokens": 1}}

    clova.chat = fake
    try:
        return answer("질문", deadline=50.0), state
    finally:
        clova.chat = real_chat


# 검문 2 경로: 도구 0회 + 거절. FORCE_TOOL·REVIEW 가 먼저 두 번 되돌리고,
# 그 다음이 검문이다. 그래도 안 고치면 4번째 답을 그대로 낸다.
r, st = run_with(["공시에서 확인되지 않습니다."])
check("거절 경로 LLM 호출 4회(FORCE_TOOL·REVIEW·검문)", st["i"] == 4, f"{st['i']}회")
check("검문이 no_search 로 기록된다", r["waste"]["audit"] == "no_search",
      repr(r["waste"]["audit"]))
last = st["seen"][-1]
check("검문 지시가 실제로 들어갔다", any(loop.AUDIT_NO_SEARCH in (c or "") for c in last))
check("검문 지시는 한 번만", sum(1 for c in last if c == loop.AUDIT_NO_SEARCH) == 1)
check("그래도 답은 나온다", bool(r["answer"].strip()), r["answer"][:40])

# 검문 1 경로: 두 번째 턴에서 비율을 고쳐도, 안 고쳐도 한 번만 돈다
r2, st2 = run_with(["삼성전자가 약 234.73% 더 많다.", "삼성전자가 약 234.73% 더 많다."])
check("검문 1 도 한 번만 (호출 4회 이하)", st2["i"] <= 4, f"{st2['i']}회")
check("검문 기록이 남는다", r2["waste"]["audit"] in ("calc", "no_search"),
      repr(r2["waste"]["audit"]))

# 정상 답변은 검문에 안 걸린다 — 추가 호출이 없어야 한다
r3, st3 = run_with(["매출액은 300,870,903백만원이다. 근거는 위와 같다."])
check("정상 답변은 검문에 안 걸린다", r3["waste"]["audit"] == "",
      repr(r3["waste"]["audit"]))

print()
print("=" * 74)
print("4-B — 검문 4 가 루프에서 딱 한 턴만 더 준다")
print("=" * 74)

# 도구를 한 번 불러 not_found 를 받고, 모델은 계속 거절만 한다.
import src.tools as T                                      # noqa: E402

real_tools = dict(T.TOOLS)


def run_all_bad(texts):
    state = {"i": 0, "seen": [], "tool_calls": 0}

    def fake(messages, **kw):
        state["seen"].append([m.get("content") for m in messages])
        i = state["i"]
        state["i"] += 1
        # 첫 턴에만 도구를 부른다. 그 뒤로는 거절 문장만 낸다.
        if i == 0 and kw.get("extra"):
            return {"ok": True, "text": "", "finish_reason": "tool_calls", "usage": {},
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "search_disclosure",
                                                 "arguments": {"query": "x",
                                                               "rcept_dt": "20240130",
                                                               "fiscal_year": 2024}}}]}
        return {"ok": True, "text": texts[min(i - 1, len(texts) - 1)],
                "tool_calls": [], "finish_reason": "stop", "usage": {}}

    T.TOOLS = dict(real_tools)
    T.TOOLS["search_disclosure"] = lambda **kw: {
        "status": "not_found", "results": [], "sources": [],
        "note": "필터 조건에 맞는 문서가 없다 · rcept_dt 와 fiscal_year 를 함께 걸었다"}
    clova.chat = fake
    try:
        return answer("질문", deadline=50.0), state
    finally:
        clova.chat = real_chat
        T.TOOLS = real_tools


r5, st5 = run_all_bad(["공시에서 찾을 수 없습니다."])
check("검문 4 가 걸렸다", r5["waste"]["audit"] == "all_bad", repr(r5["waste"]["audit"]))
last = st5["seen"][-1]
check("검문 4 지시가 들어갔다", any(loop.AUDIT_ALL_BAD in (c or "") for c in last))
check("검문 4 지시는 한 번만", sum(1 for c in last if c == loop.AUDIT_ALL_BAD) == 1)
check("그래도 답은 나온다", bool(r5["answer"].strip()), r5["answer"][:40])
check("LLM 3회 이하(도구턴 + 답변 + 검문 뒤 답변)", st5["usage"] if False else
      (r5["usage"]["llm_calls"] <= 3), str(r5["usage"]["llm_calls"]))

# 도구가 ok 를 돌려주면 검문 4 는 안 걸린다
def run_ok():
    state = {"i": 0}

    def fake(messages, **kw):
        i = state["i"]
        state["i"] += 1
        if i == 0 and kw.get("extra"):
            return {"ok": True, "text": "", "finish_reason": "tool_calls", "usage": {},
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "search_disclosure",
                                                 "arguments": {"query": "x"}}}]}
        return {"ok": True, "text": "공시에서 찾을 수 없습니다.", "tool_calls": [],
                "finish_reason": "stop", "usage": {}}

    T.TOOLS = dict(real_tools)
    T.TOOLS["search_disclosure"] = lambda **kw: {
        "status": "ok", "results": [], "sources": ["[○○ 사업보고서]"], "note": ""}
    clova.chat = fake
    try:
        return answer("질문", deadline=50.0)
    finally:
        clova.chat = real_chat
        T.TOOLS = real_tools


r6 = run_ok()
check("도구가 ok 면 검문 4 가 안 걸린다", r6["waste"]["audit"] != "all_bad",
      repr(r6["waste"]["audit"]))

# 데드라인이 얼마 안 남으면 건너뛴다
state7 = {"i": 0}


def fake_tight(messages, **kw):
    i = state7["i"]
    state7["i"] += 1
    if i == 0 and kw.get("extra"):
        return {"ok": True, "text": "", "finish_reason": "tool_calls", "usage": {},
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "search_disclosure",
                                             "arguments": {"query": "x"}}}]}
    return {"ok": True, "text": "공시에서 찾을 수 없습니다.", "tool_calls": [],
            "finish_reason": "stop", "usage": {}}


T.TOOLS = dict(real_tools)
T.TOOLS["search_disclosure"] = lambda **kw: {
    "status": "not_found", "results": [], "sources": [], "note": "없다"}
clova.chat = fake_tight
try:
    r7 = answer("질문", deadline=1.0)
finally:
    clova.chat = real_chat
    T.TOOLS = real_tools
check("데드라인 1초면 검문 4 를 건너뛴다", r7["waste"]["audit"] == "",
      repr(r7["waste"]["audit"]))
check("그래도 답은 나온다", bool(r7["answer"].strip()), r7["answer"][:40])

print()
print("=" * 74)
print("5 — 남은 시간이 8초 미만이면 검문을 건너뛴다")
print("=" * 74)

state = {"i": 0}


def fake_slow(messages, **kw):
    state["i"] += 1
    return {"ok": True, "text": "공시에서 확인되지 않습니다.", "tool_calls": [],
            "finish_reason": "stop", "usage": {}}


clova.chat = fake_slow
try:
    # deadline 을 아주 작게 주면 남은 시간이 늘 8초 미만이다
    r4 = answer("질문", deadline=1.0)
finally:
    clova.chat = real_chat
check("검문이 걸리지 않았다", r4["waste"]["audit"] == "", repr(r4["waste"]["audit"]))
check("답은 그대로 나온다", bool(r4["answer"].strip()), r4["answer"][:40])

print()
print("=" * 74)
if FAIL:
    print(f"{len(FAIL)}건 실패: " + ", ".join(FAIL))
    sys.exit(1)
print("전부 통과 · LLM 실제 호출 0회")
