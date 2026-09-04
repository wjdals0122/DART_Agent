"""최종 답변 검문 검증. **LLM 을 부르지 않는다** (clova.chat 을 가짜로 갈아끼운다).

  python eval/test_audit.py

검문 1 — 답변에 비율·배수가 있는데 calculate 를 안 불렀다
검문 2 — 아무것도 조회하지 않고 "확인되지 않는다" 고 했다

여기서 확인하는 것
  - 판정 함수가 맞는 것만 잡는가(공시에서 베낀 비율은 안 잡는다)
  - 루프가 **딱 한 턴만** 더 도는가. 두 번 이상 돌면 안 된다
  - 추가 턴에서도 같은 상태면 그대로 내보내는가
  - 남은 시간이 AUDIT_MARGIN(20초) 미만이면 검문을 건너뛰는가
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

import os                                              # noqa: E402

# 검문(루프 역학)만 시험한다 — 재작성 단계가 켜지면 가짜 LLM 의 턴 수가 밀린다
os.environ["DART_REWRITE"] = "0"

from src.agent import clova, loop                      # noqa: E402
from src.agent import usage as usage_mod               # noqa: E402

usage_mod.set_enabled(False)                           # 비용 로그를 더럽히지 않는다
from src.agent.loop import (all_tools_failed,  # noqa: E402
                            announced_but_not_called, answer,
                            compares_without_calc, needs_calc,
                            refused_without_search)

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

# --- step33 확장: 거절 표현 4종 추가 ---
check("'답변할 수 없' 도 거절로 본다 — 두산퓨얼셀 실측",
      all_tools_failed("해당 계약에 대한 정보가 없어 답변할 수 없습니다.",
                       bad("search_disclosure")))
check("'제공할 수 없' 도 거절로 본다",
      all_tools_failed("해당 정보를 제공할 수 없습니다.", bad("list_disclosures", "error")))
check("ok 가 있으면 새 표현이 있어도 안 잡는다",
      not all_tools_failed("답변할 수 없습니다.", tc("search_disclosure")))
check("거절이 아니면 여전히 안 잡는다",
      not all_tools_failed("영업이익은 9,716억원이다.", bad("search_disclosure")))

print()
print("=" * 74)
print("2-C — 검문 5 판정: 부르겠다고 해 놓고 안 불렀나")
print("=" * 74)

REAL = ("이를 위해 search_disclosure 도구를 사용하여 해당 문서의 본문을 "
        "검색하겠습니다.")
check("실제 실패 문장을 잡는다 — 회귀 3번",
      announced_but_not_called(REAL, tc("list_disclosures")))
check("'해야 합니다' 도 잡는다",
      announced_but_not_called("search_disclosure 로 본문을 확인해야 합니다.",
                               tc("list_disclosures")))
check("'조회하여' 도 잡는다",
      announced_but_not_called("list_disclosures 로 목록을 조회하여 확인하겠습니다.", []))

# --- 오탐 방어: 정상 답변 3종은 걸리면 안 된다 ---
check("① 실제로 부른 도구를 언급하는 것은 정상",
      not announced_but_not_called(
          "search_disclosure 로 조회하여 확인한 결과 매출액은 300,870,903백만원입니다.",
          tc("search_disclosure")))
check("② 도구 이름 없이 예고 표현만 있으면 안 걸린다",
      not announced_but_not_called("정확한 금액은 추가 확인이 필요합니다.",
                                   tc("search_disclosure")))
check("③ 도구 이름만 있고 예고 표현이 없으면 안 걸린다",
      not announced_but_not_called("search_disclosure 결과 문서는 사업보고서였다.",
                                   tc("list_disclosures")))
check("예고 표현이 다른 문장에 있으면 안 걸린다",
      not announced_but_not_called(
          "search_disclosure 결과는 위와 같다. 추가 자료가 필요합니다.",
          tc("list_disclosures")))
check("도구 0회 + 예고만 있어도 도구 이름이 없으면 안 걸린다",
      not announced_but_not_called("조회하여 확인하겠습니다.", []))

# --- step33 확장: 호출 시늉(예고 표현 없이 JSON/괄호) ---
JSON_CALL = ('[{"name": "search_disclosure", "arguments": '
             '{"query": "연료전지 시스템 공급계약"}}]')
check("호출 JSON 텍스트를 잡는다 — 두산퓨얼셀 실측",
      announced_but_not_called(JSON_CALL, tc("list_disclosures")))
check("괄호 호출 시늉도 잡는다",
      announced_but_not_called("search_disclosure(query='계약금액') 결과를 보면 된다.",
                               tc("list_disclosures")))
check("실제로 부른 도구의 괄호 언급은 안 걸린다",
      not announced_but_not_called(
          "search_disclosure(query='매출액') 로 조회한 결과 300,870,903백만원입니다.",
          tc("search_disclosure")))
check("괄호·중괄호가 근처에 없으면 안 걸린다",
      not announced_but_not_called("search_disclosure 결과 문서는 사업보고서였다.",
                                   tc("list_disclosures")))

print()
print("=" * 74)
print("2-D — 검문 6 판정: 여러 값을 비교했는데 calculate 를 안 불렀나")
print("=" * 74)

# 실측 환각(2026-09-04) 재현 — 억원과 백만원을 눈으로 견줘 작은 쪽을 골랐다
COMPARE = ("조회된 회사 중 2024년 영업이익이 가장 높은 회사는 삼성E&A(9,716억원)"
           "입니다. 기아는 12,667,139백만원입니다.")
check("실제 환각 답변을 잡는다 — 억원·백만원 눈대중 비교",
      compares_without_calc(COMPARE, tc("search_disclosure")))
check("'최대' + 숫자 2개도 잡는다",
      compares_without_calc("영업이익 최대는 A(1,000백만원)다. B는 2,500억원이다.",
                            tc("search_disclosure")))
check("'1위' 도 잡는다",
      compares_without_calc("1위는 A(35,000백만원), 2위는 B(21,000백만원)입니다.",
                            tc("search_disclosure")))
check("calculate 를 불렀으면 안 잡는다",
      not compares_without_calc(COMPARE, tc("calculate")))
check("숫자가 하나뿐이면 안 잡는다",
      not compares_without_calc("가장 높은 회사는 기아(12,667,139백만원)입니다.",
                                tc("search_disclosure")))
check("연도는 숫자로 세지 않는다",
      not compares_without_calc("2024년 가장 높은 회사는 기아(12,667,139백만원)입니다.",
                                tc("search_disclosure")))
check("'최대주주' 는 비교 표현이 아니다",
      not compares_without_calc("최대주주는 30,000,000주, 특수관계인은 5,000,000주를 "
                                "보유합니다.", tc("search_disclosure")))
check("비교 표현이 없으면 안 잡는다",
      not compares_without_calc("매출액은 300,870,903백만원, 영업이익은 9,716억원이다.",
                                tc("search_disclosure")))

print()
print("=" * 74)
print("2-G — 검문 8 판정(unsourced_numbers): 답변에만 있는 수치 (step40)")
print("=" * 74)

D2 = [{"chunk_id": "c1", "text": "Salubris 계약금 USD 4,000,000, Blue Earth 1,500,000. "
                                 "Pfizer 건은 계약상 비밀유지로 기재 생략."}]
check("합성값을 잡는다 — 실측 2번(USD 10,000,000)",
      "10000000" in loop.unsourced_numbers(
          "Pfizer 선급금은 USD 10,000,000 입니다.", D2, "Pfizer 선급금은?", []))
check("문서에 있는 값은 통과",
      loop.unsourced_numbers("Salubris 계약금은 USD 4,000,000 입니다.", D2, "질문", []) == [])
DOC66 = [{"chunk_id": "c1", "text": "매출액 66,192,960 백만원"}]
check("표기 변형: 쉼표 없는 66192960 통과",
      loop.unsourced_numbers("매출액은 66192960백만원이다.", DOC66, "질문", []) == [])
check("표기 변형: '6조 6,192억' 도 같은 값으로 통과",
      loop.unsourced_numbers("매출액은 약 6조 6,192억원이다.", DOC66, "질문", []) == [])
check("질문 원문의 가상 경쟁사 값은 통과",
      loop.unsourced_numbers("경쟁사는 9,800,000백만원이다.", [],
                             "가상 경쟁사가 9,800,000백만원이라는데?", []) == [])
check("calculate 결과값은 통과",
      loop.unsourced_numbers("차액은 347,727백만원이다.", DOC66, "질문",
                             [{"name": "calculate",
                               "arguments": {"expression": "10147727-9800000"},
                               "summary": "value=347727"}]) == [])
check("접수번호 14자리는 제외",
      loop.unsourced_numbers("[접수번호 20250319000665]", [], "질문", []) == [])
check("백분율은 제외",
      loop.unsourced_numbers("비중은 146,663% 다.", [], "질문", []) == [])
check("YYYYMMDD 날짜는 제외",
      loop.unsourced_numbers("접수일 20250319 기준이다.", [], "질문", []) == [])
check("근거 절 출처 문자열의 값도 통과",
      loop.unsourced_numbers("자본은 578,460억이다.", [], "질문", [],
                             sources=["[KB금융 …, 자본 578,460억 …]"]) == [])
check("지저분한 입력 예외 없음",
      loop.unsourced_numbers(None, None, None, None) == [])

print()
print("=" * 74)
print("2-E — 검문 되읊기 방어(audit_degraded)·자리표시자 제거 (step39)")
print("=" * 74)

PREV = ("2024년 계약금액은 644억원이다 [HD현대일렉트릭 사업보고서 (2024.12), "
        "2025-03-20, 접수번호 20250320001234].\n\n근거\n- [HD현대일렉트릭 …]")
check("'앞서 제공한 답변에서…' 되읊기를 잡는다 — 실측 4번",
      loop.audit_degraded("앞서 제공한 답변에서 두 회사의 매출을 비교하는 부분에 "
                          "대해 도구를 사용하여 다시 계산했습니다.", PREV))
check("도구 이름이 본문에 나오면 잡는다 — 실측 19번",
      loop.audit_degraded("계약 금액의 변경 비율을 calculate 도구로 구했습니다.", PREV))
check("자리표시자 누출을 잡는다",
      loop.audit_degraded("ㅇㅇ전자의 매출은 300,870,903백만원입니다.", PREV))
check("'죄송합니다…수정된 답변' 어두를 잡는다 — step40 실측",
      loop.audit_degraded("죄송합니다. 제가 제공한 답변에 오류가 있었습니다. "
                          "다음은 수정된 답변입니다.", PREV))
check("근거가 사라지면 잡는다",
      loop.audit_degraded("계약금액은 644억원이다.", PREV))
check("멀쩡히 고친 답변은 안 잡는다",
      not loop.audit_degraded(
          "2024년 최종 계약금액은 644억원이다 [HD현대일렉트릭 사업보고서 (2024.12), "
          "2025-03-20, 접수번호 20250320001234].", PREV))
check("검문 전 답변에 근거가 없었으면 근거 조건은 안 본다",
      not loop.audit_degraded("영업이익은 9,716억원이다.", "확인 중이다."))
for args in ((None, None), ("", ""), (None, PREV)):
    try:
        loop.audit_degraded(*args)
        ok = True
    except Exception:                          # noqa: BLE001
        ok = False
    check(f"audit_degraded{args!r:.30s} 예외 없음", ok)

check("자리표시자 문단만 지운다",
      loop.strip_placeholders("실제 값은 644억원이다.\n\nㅇㅇ전자의 매출은 "
                              "300,870,903백만원입니다.\n\n끝 문단이다.")
      == "실제 값은 644억원이다.\n\n끝 문단이다.")
check("XXXXXX 접수번호도 걷어낸다",
      "XXXXXX" not in loop.strip_placeholders("근거: 접수번호 20250311XXXXXX 문단.\n\n정상."))
check("전부 자리표시자면 빈 문자열", loop.strip_placeholders("△△전자 어쩌고") == "")
check("자리표시자 없으면 그대로",
      loop.strip_placeholders("정상 문단 하나.") == "정상 문단 하나.")

print()
print("=" * 74)
print("2-F — calculate 옮겨 적기 검증 (step39 · 실측 39번)")
print("=" * 74)

DOCS39 = [{"chunk_id": "c1", "text": "매출액 10,147,727 백만원, 경쟁사는 9,800,000"}]
check("검색 결과에 없는 긴 숫자를 짚는다 — 0 하나 더 붙인 실측",
      "100147727" in loop._calc_transcribe_note(
          {"expression": "100147727-9800000"}, DOCS39, "질문"))
check("문서에 있는 숫자는 안 짚는다(쉼표 무관)",
      loop._calc_transcribe_note(
          {"expression": "10147727-9800000"}, DOCS39, "질문") == "")
check("질문 원문의 가상 경쟁사 값도 인정한다",
      loop._calc_transcribe_note(
          {"expression": "10147727-9800000"},
          [{"chunk_id": "c1", "text": "매출액 10,147,727"}],
          "경쟁사가 9,800,000백만원이라는데 비교해줘") == "")
check("6자리 미만 숫자는 안 본다",
      loop._calc_transcribe_note({"expression": "123+456"}, [], "질문") == "")
check("지저분한 입력 예외 없음",
      loop._calc_transcribe_note(None, None, None) == "")

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
for args in ((None, None), ("", []), ("가장 높다 1,234 5,678", [None]),
             ("최대 100 200", [{"name": None}])):
    try:
        compares_without_calc(*args)
        ok = True
    except Exception as e:                     # noqa: BLE001
        ok = False
        print("   ", type(e).__name__, e)
    check(f"compares_without_calc{args!r:.36s} 예외 없음", ok)

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
check("검문 지시는 한 번만",
      sum(1 for c in last if loop.AUDIT_NO_SEARCH in (c or "")) == 1)
check("공통 꼬리(AUDIT_TAIL)가 붙는다",
      any(loop.AUDIT_TAIL in (c or "") for c in last))
check("그래도 답은 나온다", bool(r["answer"].strip()), r["answer"][:40])

# 검문 1 경로: 두 번째 턴에서 비율을 고쳐도, 안 고쳐도 한 번만 돈다
r2, st2 = run_with(["삼성전자가 약 234.73% 더 많다.", "삼성전자가 약 234.73% 더 많다."])
check("검문 1 도 한 번만 (호출 4회 이하)", st2["i"] <= 4, f"{st2['i']}회")
check("검문 기록이 남는다", r2["waste"]["audit"] in ("calc", "no_search"),
      repr(r2["waste"]["audit"]))

# 정상 답변(검색 근거 없는 수치도 없음)은 검문에 안 걸린다
# ※ step40 부터 '검색 0건인데 큰 숫자' 는 검문 8(unsourced_num)에 걸리는 게 맞다 —
#   그래서 수치 없는 문장으로 바꿨다. 수치 유형은 아래 4-E 가 따로 본다.
r3, st3 = run_with(["매출 자료는 근거 절에 정리했다. 근거는 위와 같다."])
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
check("검문 4 지시는 한 번만",
      sum(1 for c in last if loop.AUDIT_ALL_BAD in (c or "")) == 1)
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
print("4-C — 검문 5 가 루프에서 딱 한 턴만 더 준다 (회귀 3번 재현)")
print("=" * 74)


def run_announced(texts, tool_status="ok"):
    """list_disclosures 만 부르고, 답변에서 search_disclosure 를 부르겠다고 말한다."""
    state = {"i": 0, "seen": []}

    def fake(messages, **kw):
        state["seen"].append([m.get("content") for m in messages])
        i = state["i"]
        state["i"] += 1
        if i == 0 and kw.get("extra"):
            return {"ok": True, "text": "", "finish_reason": "tool_calls", "usage": {},
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "list_disclosures",
                                                 "arguments": {"corp_name": "KB금융"}}}]}
        return {"ok": True, "text": texts[min(i - 1, len(texts) - 1)],
                "tool_calls": [], "finish_reason": "stop", "usage": {}}

    T.TOOLS = dict(real_tools)
    T.TOOLS["list_disclosures"] = lambda **kw: {
        "status": tool_status, "results": [{"rcept_no": "20260619000667"}],
        "sources": [], "note": "이것은 문서 목록이며 내용이 아니다"}
    clova.chat = fake
    try:
        return answer("KB금융 2025년 자기자본은?", deadline=50.0), state
    finally:
        clova.chat = real_chat
        T.TOOLS = real_tools


r8, st8 = run_announced([REAL])
check("검문 5 가 걸렸다", r8["waste"]["audit"] == "announced", repr(r8["waste"]["audit"]))
last8 = st8["seen"][-1]
check("검문 5 지시가 들어갔다", any(loop.AUDIT_ANNOUNCED in (c or "") for c in last8))
check("검문 5 지시는 한 번만",
      sum(1 for c in last8 if loop.AUDIT_ANNOUNCED in (c or "")) == 1)
check("그래도 답은 나온다", bool(r8["answer"].strip()), r8["answer"][:40])

# 배타 — 검문 4(전부 빗나감)와 동시에 걸리지 않는다. elif 사슬이라 하나만 남는다.
r9, _ = run_announced(["공시에서 찾을 수 없습니다. search_disclosure 로 조회하겠습니다."],
                      tool_status="not_found")
check("검문 4·5 가 함께 걸리지 않는다 (하나만)",
      r9["waste"]["audit"] in ("all_bad", "announced"), repr(r9["waste"]["audit"]))
check("  먼저 오는 검문 4 가 잡는다", r9["waste"]["audit"] == "all_bad",
      repr(r9["waste"]["audit"]))

# 정상 답변(부른 도구를 언급)은 안 걸린다
r10, st10 = run_announced(
    ["list_disclosures 로 조회하여 확인한 결과 2025년 자기자본은 57,846십억 원입니다."])
check("부른 도구를 언급하는 정상 답변은 안 걸린다", r10["waste"]["audit"] == "",
      repr(r10["waste"]["audit"]))
# 이 경로에서 턴이 하나 더 도는 것은 검문이 아니라 LIST_ONLY_NUDGE 다 —
# list 만 ok 이고 본문 검색이 없으면 원래 한 번 되돌린다(step6-H). 검문 5 와 무관하다.
check("  추가 턴은 LIST_ONLY_NUDGE 의 몫", r10["waste"]["list_only_nudge"] is True,
      str({k: v for k, v in r10["waste"].items() if v}))

print()
print("=" * 74)
print("4-D — 검문 6 가 루프에서 딱 한 턴만 더 준다 (전수 비교 환각 재현)")
print("=" * 74)


def run_compare(texts):
    """search 가 ok 를 주고, 모델이 크기 비교 답변을 calculate 없이 낸다."""
    state = {"i": 0, "seen": []}

    def fake(messages, **kw):
        state["seen"].append([m.get("content") for m in messages])
        i = state["i"]
        state["i"] += 1
        if i == 0 and kw.get("extra"):
            return {"ok": True, "text": "", "finish_reason": "tool_calls", "usage": {},
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "search_disclosure",
                                                 "arguments": {"query": "영업이익"}}}]}
        return {"ok": True, "text": texts[min(i - 1, len(texts) - 1)],
                "tool_calls": [], "finish_reason": "stop", "usage": {}}

    T.TOOLS = dict(real_tools)
    T.TOOLS["search_disclosure"] = lambda **kw: {
        "status": "ok", "results": [], "sources": ["[○○ 사업보고서]"], "note": ""}
    clova.chat = fake
    try:
        return answer("2024년 영업이익이 가장 높은 회사는?", deadline=50.0), state
    finally:
        clova.chat = real_chat
        T.TOOLS = real_tools


r11, st11 = run_compare([COMPARE])
check("검문 6 가 걸렸다", r11["waste"]["audit"] == "compare", repr(r11["waste"]["audit"]))
last11 = st11["seen"][-1]
check("검문 6 지시가 들어갔다", any(loop.AUDIT_COMPARE in (c or "") for c in last11))
check("검문 6 지시는 한 번만",
      sum(1 for c in last11 if loop.AUDIT_COMPARE in (c or "")) == 1)
check("한 턴만 더 돈다 (LLM 3회)", st11["i"] == 3, f"{st11['i']}회")
check("그래도 답은 나온다", bool(r11["answer"].strip()), r11["answer"][:40])

# 배타 — 검문 1(비율·배수)과 함께 걸릴 조건이면 먼저 오는 검문 1 만 잡는다
r12, _ = run_compare(["가장 높은 회사는 A(1,000백만원)로 B(500백만원)보다 100% 많다."])
check("검문 1·6 이 함께 걸리지 않는다 (검문 1 이 먼저)",
      r12["waste"]["audit"] == "calc", repr(r12["waste"]["audit"]))

print()
print("=" * 74)
print("4-E — 검문 8 이 루프에서 딱 한 턴만 (step40)")
print("=" * 74)
r13, st13 = run_compare(["Pfizer 선급금은 USD 10,000,000 입니다."])
check("검문 8 이 걸렸다", r13["waste"]["audit"] == "unsourced_num",
      repr(r13["waste"]["audit"]))
last13 = st13["seen"][-1]
check("검문 8 지시가 들어갔다", any("답변의 수치" in (c or "") for c in last13))
check("한 턴만 더 돈다 (LLM 3회)", st13["i"] == 3, f"{st13['i']}회")
check("그래도 답은 나온다", bool(r13["answer"].strip()), r13["answer"][:40])
# 근거에 있는 수치만 쓰는 정상 답변은 안 걸린다 — 검색 결과가 빈 fixture 라
# 질문의 연도(2024)만 쓰는 답으로 확인한다
r14, _ = run_compare(["2024년 자료는 공시에서 확인되지 않습니다."])
check("수치 없는 답변은 검문 8 미발동", r14["waste"]["audit"] != "unsourced_num",
      repr(r14["waste"]["audit"]))

print()
print("=" * 74)
print("5 — 남은 시간이 AUDIT_MARGIN(20초) 미만이면 검문을 건너뛴다")
print("=" * 74)

state = {"i": 0}


def fake_slow(messages, **kw):
    state["i"] += 1
    return {"ok": True, "text": "공시에서 확인되지 않습니다.", "tool_calls": [],
            "finish_reason": "stop", "usage": {}}


clova.chat = fake_slow
try:
    # deadline 을 아주 작게 주면 남은 시간이 늘 AUDIT_MARGIN 미만이다
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
