# -*- coding: utf-8 -*-
"""질문 재작성 단계 검증. **LLM 을 부르지 않는다** (clova.chat 을 가짜로 갈아끼운다).

  python eval/test_rewrite.py

여기서 확인하는 것 — 전부 실패 시 동작 계약이다:
  1. 재작성 LLM 실패(400 등) → 단계를 건너뛰고 answer() 는 정상 동작
  2. JSON 파싱 실패 → 건너뛰고 정상 동작
  3. 15초 초과 → 결과를 버리고 정상 동작
  4. 성공 → [질문 분석] 블록이 시스템 프롬프트의 planner 앞에 붙는다
  5. **API 응답 question 필드는 언제나 원문이다** (주최측 규격 — 재작성문 금지)
  6. think_trace 에 "질문 분석: …" 또는 "질문 분석 생략(사유)" 가 남는다
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

os.environ["DART_REWRITE"] = "1"           # 이 파일은 재작성을 **켜고** 시험한다

from src.agent import clova, loop  # noqa: E402
from src.agent import rewrite as R  # noqa: E402
from src.agent import usage as usage_mod  # noqa: E402
from src.api import server as S  # noqa: E402

usage_mod.set_enabled(False)               # 비용 로그를 더럽히지 않는다

FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


ORIG_Q = "한화오션의 작년 정정공시 내용 알려줘"
REWRITTEN = "한화오션의 2025 회계연도(2025년) 정정공시 내용은 무엇인가?"
GOOD_JSON = json.dumps({
    "rewritten": REWRITTEN,
    "sub_questions": [REWRITTEN],
    "companies": ["한화오션"],
    "fiscal_years": [2025],
    "doc_groups": [],
    "time_note": "'작년'을 코퍼스 최신 시점(2026.03) 기준 2025년으로 해석",
    "out_of_scope": False,
}, ensure_ascii=False)

FINAL = "2025년 정정공시는 계약금액 변경 1건이다."
real_chat = clova.chat


def make_chat(rewrite_reply):
    """재작성 호출(시스템에 '질문 분석기')과 본 루프 호출을 가른다."""
    state = {"n_rewrite": 0, "n_main": 0, "systems": []}

    def fake(messages, **kw):
        head = (messages[0].get("content") or "") if messages else ""
        if "질문 분석기" in head:
            state["n_rewrite"] += 1
            return rewrite_reply() if callable(rewrite_reply) else dict(rewrite_reply)
        state["n_main"] += 1
        state["systems"].append(head)
        return {"ok": True, "text": FINAL, "tool_calls": [],
                "finish_reason": "stop", "usage": {}}

    return fake, state


def run_answer(rewrite_reply):
    fake, st = make_chat(rewrite_reply)
    clova.chat = fake
    try:
        return loop.answer(ORIG_Q, deadline=50.0, verbose=True), st
    finally:
        clova.chat = real_chat


def spec_ok(r):
    return (isinstance(r, dict) and isinstance(r.get("answer"), str)
            and r["answer"].strip() and isinstance(r.get("sources"), list))


print("=" * 74)
print("1 — 재작성 LLM 실패(400) → 건너뛰고 정상 진행")
print("=" * 74)
r, st = run_answer({"ok": False, "error": "HTTP 400: 40009 Unsupported function",
                    "status": 400, "elapsed": 0.1})
check("answer() 정상", spec_ok(r), r["answer"][:40])
check("재작성 1회 시도됨", st["n_rewrite"] == 1, str(st["n_rewrite"]))
check("rewrite.ok=False + 사유", r["rewrite"]["ok"] is False
      and "LLM 실패" in r["rewrite"]["note"], repr(r["rewrite"]["note"]))
check("시스템 프롬프트에 [질문 분석] 없음",
      all("[질문 분석 —" not in s for s in st["systems"]))

print()
print("=" * 74)
print("2 — JSON 파싱 실패 → 건너뛰고 정상 진행")
print("=" * 74)
r, st = run_answer({"ok": True, "text": "이건 JSON 이 아니라 산문이다.",
                    "tool_calls": [], "usage": {}, "elapsed": 0.1})
check("answer() 정상", spec_ok(r), r["answer"][:40])
check("rewrite.ok=False + 파싱 사유", r["rewrite"]["ok"] is False
      and "파싱" in r["rewrite"]["note"], repr(r["rewrite"]["note"]))
check("시스템 프롬프트에 [질문 분석] 없음",
      all("[질문 분석 —" not in s for s in st["systems"]))

print()
print("=" * 74)
print("3 — 15초 초과 → 결과를 버리고 정상 진행")
print("=" * 74)
real_budget = R.REWRITE_BUDGET_S
R.REWRITE_BUDGET_S = 0.01


def slow_reply():
    time.sleep(0.05)
    return {"ok": True, "text": GOOD_JSON, "tool_calls": [], "usage": {}}


try:
    r, st = run_answer(slow_reply)
finally:
    R.REWRITE_BUDGET_S = real_budget
check("answer() 정상", spec_ok(r), r["answer"][:40])
check("결과를 버렸다(ok=False + 초과 사유)", r["rewrite"]["ok"] is False
      and "초과" in r["rewrite"]["note"], repr(r["rewrite"]["note"]))
check("시스템 프롬프트에 [질문 분석] 없음",
      all("[질문 분석 —" not in s for s in st["systems"]))

print()
print("=" * 74)
print("4 — 성공 → [질문 분석] 블록이 planner 앞에 붙는다")
print("=" * 74)
r, st = run_answer({"ok": True, "text": GOOD_JSON, "tool_calls": [], "usage": {}})
check("answer() 정상", spec_ok(r), r["answer"][:40])
check("rewrite.ok=True", r["rewrite"]["ok"] is True, repr(r["rewrite"]["note"]))
check("분석이 담겨 있다", (r["rewrite"]["analysis"] or {}).get("companies") == ["한화오션"])
sys_prompt = st["systems"][0] if st["systems"] else ""
check("[질문 분석] 블록이 들어갔다", "[질문 분석 —" in sys_prompt)
check("재작성문이 블록에 있다", REWRITTEN in sys_prompt)
check("참고용 문구가 있다", "이 분석은 참고용이다" in sys_prompt)
i_block, i_head = sys_prompt.find("[질문 분석"), sys_prompt.find("당신은 DART")
check("블록이 머리말 뒤·본문 앞", 0 <= i_head < i_block, f"{i_head}/{i_block}")
check("코드펜스로 감싼 JSON 도 파싱된다",
      R._parse("```json\n" + GOOD_JSON + "\n```") is not None)
check("쓰레기 입력은 None", R._parse("완전 산문") is None)
check("빈 분석은 빈 블록", R.analysis_block({}) == "")

print()
print("=" * 74)
print("5 — API question 필드는 언제나 원문 (주최측 규격)")
print("=" * 74)
fake, st = make_chat({"ok": True, "text": GOOD_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    body = S.make_answer("Q-원문", ORIG_Q)
finally:
    clova.chat = real_chat
check("question == 원문 그대로", body["question"] == ORIG_Q, repr(body["question"]))
check("question 에 재작성문이 아니다", body["question"] != REWRITTEN)
check("think_trace 에 '질문 분석:'", "질문 분석:" in body["think_trace"],
      body["think_trace"].splitlines()[0][:80])

fake, st = make_chat({"ok": False, "error": "HTTP 400", "status": 400})
clova.chat = fake
try:
    body = S.make_answer("Q-원문2", ORIG_Q)
finally:
    clova.chat = real_chat
check("실패해도 question == 원문", body["question"] == ORIG_Q, repr(body["question"]))
check("think_trace 에 '질문 분석 생략('", "질문 분석 생략(" in body["think_trace"],
      body["think_trace"].splitlines()[0][:80])

print()
print("=" * 74)
print("5-B — doc_groups: 질문이 명시했을 때만 (추정은 코드가 걷어낸다)")
print("=" * 74)
check("명시 안 했으면 버린다 — 실측 사례",
      R._filter_doc_groups(["periodic"],
                           "2024년 영업이익이 가장 높은 회사는? 한화오션의 정정공시는?") == [])
check("'사업보고서' 명시 → periodic 유지",
      R._filter_doc_groups(["periodic"], "사업보고서에서 매출액을 찾아줘") == ["periodic"])
check("'주요사항보고서' 명시 → major 유지",
      R._filter_doc_groups(["major"], "주요사항보고서에서 자기주식취득 내용은?") == ["major"])
check("'지분공시' 명시 → holding 유지",
      R._filter_doc_groups(["holding"], "지분공시 중 국민연금 보유비율은?") == ["holding"])
check("명시된 것만 남는다(혼합)",
      R._filter_doc_groups(["periodic", "major"], "주요사항보고서 좀 봐줘") == ["major"])
check("모르는 값은 버린다",
      R._filter_doc_groups(["정기보고서", "x"], "정기보고서 봐줘") == [])
check("지저분한 입력에도 예외 없음",
      R._filter_doc_groups(None, None) == [] and R._filter_doc_groups([1, {}], "") == [])

# rewrite_question 경로에서도 걷어내는지 — 모델이 periodic 을 추정해 온 상황
GUESSY = json.dumps(dict(json.loads(GOOD_JSON), doc_groups=["periodic"]),
                    ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": GUESSY, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw = R.rewrite_question(ORIG_Q)
finally:
    clova.chat = real_chat
check("rewrite_question 이 추정 doc_groups 를 걷어낸다",
      rw["ok"] and rw["analysis"]["doc_groups"] == [],
      str(rw["analysis"] and rw["analysis"]["doc_groups"]))

print()
print("=" * 74)
print("5-C — ki(기수) 가드: fiscal_years 의 1900 미만 값은 ki 로 옮긴다 (step39)")
print("=" * 74)
KI_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[50], ki=[]),
                     ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": KI_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw = R.rewrite_question("OCI홀딩스 제50기 매출은?")
finally:
    clova.chat = real_chat
check("fiscal_years=[50] → ki=[50] 으로 이동",
      rw["ok"] and rw["analysis"]["ki"] == [50] and rw["analysis"]["fiscal_years"] == [],
      str(rw["analysis"] and (rw["analysis"]["ki"], rw["analysis"]["fiscal_years"])))
check("분석 블록에 ki 안내가 들어간다",
      "ki=50" in R.analysis_block(rw["analysis"]), R.analysis_block(rw["analysis"])[:120])
MIX_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2024, 76], ki=[]),
                      ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": MIX_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw = R.rewrite_question("질문")
finally:
    clova.chat = real_chat
check("연도와 기수가 섞이면 나눈다",
      rw["analysis"]["fiscal_years"] == [2024] and rw["analysis"]["ki"] == [76],
      str((rw["analysis"]["fiscal_years"], rw["analysis"]["ki"])))

print()
print("=" * 74)
print("5-D — 파싱 실패 시 재시도 1회 (step39 · e2e_v5 3/40 실측)")
print("=" * 74)
real_sleep = time.sleep
time.sleep = lambda *_: None                   # 재시도 2초 대기를 건너뛴다


def flaky_reply():
    """1차는 산문(파싱 불가), 2차는 정상 JSON."""
    flaky_reply.n = getattr(flaky_reply, "n", 0) + 1
    if flaky_reply.n == 1:
        return {"ok": True, "text": "질문을 이해했습니다. 분석해 드리겠습니다.",
                "tool_calls": [], "usage": {}}
    return {"ok": True, "text": GOOD_JSON, "tool_calls": [], "usage": {}}


fake, st = make_chat(flaky_reply)
clova.chat = fake
try:
    rw = R.rewrite_question(ORIG_Q)
finally:
    clova.chat = real_chat
    time.sleep = real_sleep
check("1차 실패 → 재시도로 성공", rw["ok"] is True, repr(rw["note"]))
check("재시도 호출이 res_retry 로 남는다(토큰 회계)", rw["res_retry"] is not None)
check("실패 원본이 logs/rewrite_fail.jsonl 에 남는다",
      R.FAIL_LOG.is_file() and "분석해 드리겠습니다" in
      R.FAIL_LOG.read_text(encoding="utf-8"))

time.sleep = lambda *_: None


def always_prose():
    return {"ok": True, "text": "산문만 낸다.", "tool_calls": [], "usage": {}}


fake, st = make_chat(always_prose)
clova.chat = fake
try:
    rw = R.rewrite_question(ORIG_Q)
finally:
    clova.chat = real_chat
    time.sleep = real_sleep
check("재시도까지 실패하면 생략(ok=False)", rw["ok"] is False
      and "재시도" in rw["note"], repr(rw["note"]))

print()
print("=" * 74)
print("6 — DART_REWRITE=0 이면 재작성을 부르지 않는다")
print("=" * 74)
os.environ["DART_REWRITE"] = "0"
try:
    r, st = run_answer({"ok": True, "text": GOOD_JSON, "tool_calls": [], "usage": {}})
finally:
    os.environ["DART_REWRITE"] = "1"
check("재작성 호출 0회", st["n_rewrite"] == 0, str(st["n_rewrite"]))
check("사유가 남는다", "비활성화" in r["rewrite"]["note"], repr(r["rewrite"]["note"]))
check("answer() 정상", spec_ok(r))

print()
print("=" * 74)
if FAIL:
    print(f"실패 {len(FAIL)}건: {FAIL}")
    sys.exit(1)
print("전부 통과 · LLM 실제 호출 0회")
