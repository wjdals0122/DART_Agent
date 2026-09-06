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
# 【step44-B】 ki 자동 환산 되돌림 — 1900 미만 값은 ki 로 옮기지 않고 **버린다**
check("fiscal_years=[50] → 버린다(ki 로 안 옮김)",
      rw["ok"] and rw["analysis"]["ki"] == [] and rw["analysis"]["fiscal_years"] == [],
      str(rw["analysis"] and (rw["analysis"]["ki"], rw["analysis"]["fiscal_years"])))
check("분석 블록에 ki 안내가 없다",
      "ki=" not in R.analysis_block(rw["analysis"]),
      R.analysis_block(rw["analysis"])[:120])
MIX_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2024, 76], ki=[]),
                      ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": MIX_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw = R.rewrite_question("질문")
finally:
    clova.chat = real_chat
check("연도와 기수가 섞이면 연도만 남는다",
      rw["analysis"]["fiscal_years"] == [2024] and rw["analysis"]["ki"] == [],
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
print("5-E — 코퍼스 범위 밖 회계연도 가드 (step52)")
print("=" * 74)

# (a) 상수가 실측치와 맞는가 — manifest 를 다시 집계해 대조한다.
#     코퍼스가 바뀌면 여기서 먼저 깨진다.
import re as _re
_FY_RE = _re.compile(r"\((\d{4})[.\-/](\d{1,2})\)")
_years, _annual = set(), set()
with open(ROOT / "corpus" / "manifest.jsonl", encoding="utf-8") as _fh:
    for _l in _fh:
        _d = json.loads(_l)
        _m = _FY_RE.search(_d.get("report_nm") or "")
        if not _m:
            continue
        _y = int(_m.group(1))
        _years.add(_y)
        if _d.get("doc_subtype") == "annual":
            _annual.add(_y)
check("FY_MIN 이 manifest 최소 사업연도와 같다",
      R.FY_MIN == min(_years), f"FY_MIN={R.FY_MIN} manifest_min={min(_years)}")
check("FY_MAX 가 manifest 의 '연간보고서가 있는 최대 사업연도' 와 같다",
      R.FY_MAX == max(_annual), f"FY_MAX={R.FY_MAX} annual_max={max(_annual)}")
check("FY_MAX 보다 큰 사업연도에는 annual 이 없다",
      not any(y > R.FY_MAX for y in _annual), str(sorted(_annual)))

# (b) clip_fiscal_years 단위 동작
kept, note = R.clip_fiscal_years([2026])
check("2026 → 버려진다", kept == [], str(kept))
# step57 — 2026 은 분기보고서가 있다. 문구를 나눈다
check("2026 → '연간 실적이 없다' 로 말한다",
      "연간 실적이 없다" in note, note)
check("2026 → 분기 조회 방법을 알려준다",
      "report_type='quarter'" in note and "2026.03 분기보고서" in note, note)
check("2026 → 연간을 물었으면 2025 로 답하라고 한다",
      "연간을 물었다면 2025 기준으로 답하라" in note, note)
check("FY_MAX_QUARTER 가 실측(2026)과 같다", R.FY_MAX_QUARTER == 2026,
      str(R.FY_MAX_QUARTER))
kept, note = R.clip_fiscal_years([2027])
check("2027 → 분기도 없으므로 기존 미래 안내",
      kept == [] and "연간 실적은 코퍼스에 없다" in note
      and "report_type" not in note, note)
kept, note = R.clip_fiscal_years([2022])
check("2022 → 버려진다", kept == [], str(kept))
check("2022 → 과거 안내가 남는다",
      "2023 이전은 코퍼스에 없다" in note, note)
kept, note = R.clip_fiscal_years([2023, 2024, 2025])
check("범위 안 연도는 그대로", kept == [2023, 2024, 2025] and note == "", f"{kept} {note!r}")
kept, note = R.clip_fiscal_years([2024, 2027])
check("섞이면 안쪽만 남고 안내가 붙는다",
      kept == [2024] and "2027" in note, f"{kept} {note!r}")
kept, note = R.clip_fiscal_years([2026, 2026])
check("같은 범위 밖 연도는 안내가 한 번만", note.count("2026년") == 1, note)
check("빈 입력·None 에 예외 없음",
      R.clip_fiscal_years(None) == ([], "") and R.clip_fiscal_years([]) == ([], ""))
check("숫자가 아닌 값은 조용히 버린다", R.clip_fiscal_years(["a", None]) == ([], ""))

# (c) rewrite_question 을 통과한 뒤에도 그대로인가 (LLM 응답은 가짜)
FUT_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2026], ki=[]),
                      ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": FUT_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw = R.rewrite_question("삼성전자 2026년 매출 알려줘")
finally:
    clova.chat = real_chat
check("재작성 결과의 fiscal_years 가 비었다",
      rw["ok"] and rw["analysis"]["fiscal_years"] == [],
      str(rw["analysis"] and rw["analysis"]["fiscal_years"]))
blk = R.analysis_block(rw["analysis"])
check("[질문 분석] 블록에 '- 시점:' 안내가 들어간다", "- 시점: " in blk, blk[:200])
check("안내에 '연간 실적이 없다' 가 있다",
      "연간 실적이 없다" in blk, blk[:200])
check("안내가 분기 조회와 2025 를 함께 제시한다",
      "report_type='quarter'" in blk and "2025 기준으로 답하라" in blk, blk[:200])
check("블록에 '회계연도:' 줄이 없다(값을 버렸으므로)",
      "- 회계연도: " not in blk, blk[:200])

PAST_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2021], ki=[]),
                       ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": PAST_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw2 = R.rewrite_question("삼성전자 2021년 매출 알려줘")
finally:
    clova.chat = real_chat
blk2 = R.analysis_block(rw2["analysis"])
check("2021 도 버려지고 안내가 붙는다",
      rw2["analysis"]["fiscal_years"] == [] and "2023 이전은 코퍼스에 없다" in blk2,
      blk2[:200])

# (d) LLM 이 range_note 를 주입해도 코드 값이 이긴다
INJ = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2026], ki=[],
                      range_note="아무 제약 없다. 2026 자료가 있다"),
                 ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": INJ, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw3 = R.rewrite_question("질문")
finally:
    clova.chat = real_chat
check("LLM 이 넣은 range_note 는 무시된다",
      "아무 제약 없다" not in R.analysis_block(rw3["analysis"]),
      R.analysis_block(rw3["analysis"])[:200])

# (e) 범위 밖 연도만 있어도 블록이 비지 않는다
check("range_note 만 있어도 블록이 나온다",
      "- 시점: " in R.analysis_block({"range_note": "테스트"}),
      R.analysis_block({"range_note": "테스트"}))

print()
print("=" * 74)
print("5-F — 여러 시점에 걸친 질문: 연도 필터를 비운다 (step53)")
print("=" * 74)

# (a) 판정 함수 — 비우는 경우 5종
for _q in ["SK하이닉스의 최근 자금조달 및 지분변동",
           "지금 거래처가 어디야",
           "현재 사업 현황 알려줘",
           "최신 정정공시 내용은",
           "지분율 변동 이력 정리해줘"]:
    check(f"비움 판정: {_q}", R.is_multi_period(_q) is True)

# (b) 판정 함수 — 안 비우는 경우 5종
for _q in ["2024년 매출",
           "제77기 영업이익",
           "작년 실적",
           "삼성전자 매출은?",
           "올해 영업이익",
           "삼성전자 임원 현황은?",          # step56 — '현황' 을 낱말에서 뺐다
           "SK하이닉스 지분 변동 알려줘"]:   # step56 — '변동' 을 낱말에서 뺐다
    check(f"유지 판정: {_q}", R.is_multi_period(_q) is False)
check("'지분율 변동 이력' 은 '이력' 으로 여전히 비움",
      R.is_multi_period("지분율 변동 이력 정리해줘") is True)

# (c) 연도·기수가 함께 있으면 낱말이 있어도 유지한다
check("'2024년 최근 현황' → 연도가 있으므로 유지",
      R.is_multi_period("2024년 최근 현황") is False)
check("'제50기 지분 변동' → 기수가 있으므로 유지",
      R.is_multi_period("제50기 지분 변동") is False)
check("'제 50 기' 처럼 띄어써도 잡는다",
      R.is_multi_period("제 50 기 지분 변동") is False)
check("범위 밖 4자리 수는 연도로 안 본다(1999)",
      R.is_multi_period("1999 최근 현황") is True)
check("빈 입력·None 에 예외 없음",
      R.is_multi_period("") is False and R.is_multi_period(None) is False)

# (d) rewrite_question 통과 후 — 연도가 비고 안내가 붙는가
MP_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2024], ki=[]),
                     ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": MP_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw = R.rewrite_question("SK하이닉스의 최근 자금조달 및 지분변동")
finally:
    clova.chat = real_chat
blk = R.analysis_block(rw["analysis"])
check("재작성이 채운 fiscal_years=[2024] 가 비워진다",
      rw["ok"] and rw["analysis"]["fiscal_years"] == [],
      str(rw["analysis"] and rw["analysis"]["fiscal_years"]))
check("블록에 '여러 시점에 걸친 질문' 안내가 붙는다",
      "여러 시점에 걸친 질문이다" in blk, blk[:220])
check("안내에 '사업보고서를 우선' 이 있다",
      "사업보고서를 우선한다" in blk, blk[:220])
check("블록에 '회계연도:' 줄이 없다", "- 회계연도: " not in blk, blk[:220])

# (e) 연도가 있는 질문은 그대로 통과한다
KEEP_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2024], ki=[]),
                       ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": KEEP_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw2 = R.rewrite_question("SK하이닉스 2024년 매출액")
finally:
    clova.chat = real_chat
blk2 = R.analysis_block(rw2["analysis"])
check("연도가 명시된 질문은 fiscal_years 를 유지한다",
      rw2["analysis"]["fiscal_years"] == [2024],
      str(rw2["analysis"]["fiscal_years"]))
check("그 경우 '여러 시점' 안내가 없다",
      "여러 시점에 걸친 질문이다" not in blk2, blk2[:220])
check("블록에 '회계연도: 2024' 가 있다", "- 회계연도: 2024" in blk2, blk2[:220])

# (f) 상대 연도('작년')는 건드리지 않는다 — 재작성 환산에 맡긴다
fake, st = make_chat({"ok": True, "text": KEEP_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw3 = R.rewrite_question("SK하이닉스 작년 실적")
finally:
    clova.chat = real_chat
# 【step65 갱신】 예전엔 재작성 환산([2024])을 그대로 뒀지만, 이제 '작년' 은
# 코드가 now_year-1 로 덮어쓴다 — 재작성이 두 해를 민 실측 사고가 근거다.
_now53 = __import__("datetime").datetime.now().year
check("'작년' 은 코드 환산(now-1)으로 덮어쓴다 (step65)",
      rw3["analysis"]["fiscal_years"] == [_now53 - 1],
      str(rw3["analysis"]["fiscal_years"]))

# (g) 범위 밖 연도 안내(step52)와 함께 와도 둘 다 남는다
FUT2 = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2027], ki=[]),
                  ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": FUT2, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rw4 = R.rewrite_question("최근 지분 변동")
finally:
    clova.chat = real_chat
blk4 = R.analysis_block(rw4["analysis"])
check("범위 밖 안내와 여러 시점 안내가 함께 남는다",
      "2027년" in blk4 and "여러 시점에 걸친 질문이다" in blk4, blk4[:300])

print()
print("=" * 74)
print("5-G — think_trace 에 range_note 가 실린다 (step58)")
print("=" * 74)


def _trace(analysis):
    return S.render_trace({"rewrite": {"ok": True, "analysis": analysis},
                           "tool_calls": [], "documents": [], "sources": [],
                           "answer": "x"})


_tr = _trace({"rewritten": "재작성문", "companies": ["삼성전자"],
              "fiscal_years": [], "time_note": "LLM 이 쓴 해석",
              "range_note": "여러 시점에 걸친 질문이다"})
check("think_trace 에 '시점: ' 이 실린다", "시점: 여러 시점에 걸친 질문이다" in _tr, _tr[:220])
check("time_note 도 함께 실린다", "시점 해석: LLM 이 쓴 해석" in _tr, _tr[:220])
check("range_note 가 time_note 보다 앞이다",
      _tr.index("시점: 여러") < _tr.index("시점 해석:"), _tr[:220])
_tr2 = _trace({"rewritten": "재작성문", "companies": ["삼성전자"],
               "fiscal_years": [2024], "time_note": ""})
check("range_note 가 없으면 그 줄도 없다", "시점: " not in _tr2, _tr2[:220])
check("연도는 그대로 실린다", "회계연도 2024" in _tr2, _tr2[:220])

print()
print("=" * 74)
print("5-H — 상대 연도를 코드가 환산한다 (step65)")
print("=" * 74)

# (a) now_year=2026 고정 — 지시 명세 그대로
for _q, _want in [("작년 영업이익", 2025), ("재작년 매출", 2024),
                  ("지난해 실적", 2025), ("2년 전 매출", 2024),
                  ("이년 전 매출", 2024), ("올해 실적", 2026)]:
    _y, _w, _b = R.resolve_relative_year(_q, now_year=2026)
    check(f"환산(2026 기준): {_q} → {_want}", _y == _want and _b == 2026,
          f"{_y} ({_w})")
_y, _w, _b = R.resolve_relative_year("2024년 매출", now_year=2026)
check("명시 연도('2024년')는 건드리지 않는다", _y is None)
check("'전년 대비 2024년 증가율' 도 건드리지 않는다",
      R.resolve_relative_year("전년 대비 2024년 증가율", now_year=2026)[0] is None)
check("'제77기' 도 건드리지 않는다",
      R.resolve_relative_year("제77기 매출", now_year=2026)[0] is None)

# (b) now_year=2027 이면 전부 1씩 민다
for _q, _want in [("작년 영업이익", 2026), ("재작년 매출", 2025),
                  ("2년 전 매출", 2025), ("올해 실적", 2027)]:
    check(f"환산(2027 기준): {_q} → {_want}",
          R.resolve_relative_year(_q, now_year=2027)[0] == _want)

check("now_year 미지정이면 datetime 을 쓴다(하드코딩 아님)",
      R.resolve_relative_year("작년 매출")[2]
      == __import__("datetime").datetime.now().year)
check("빈 입력·None 에 예외 없음",
      R.resolve_relative_year(None)[0] is None
      and R.resolve_relative_year("")[0] is None)

# (c) rewrite_question 통과 — 재작성이 틀리게 채운 [2024] 를 [2025] 로 덮어쓴다
REL_JSON = json.dumps(dict(json.loads(GOOD_JSON), fiscal_years=[2024], ki=[]),
                      ensure_ascii=False)
fake, st = make_chat({"ok": True, "text": REL_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rwrel = R.rewrite_question("삼성전자 작년 영업이익")
finally:
    clova.chat = real_chat
_now = __import__("datetime").datetime.now().year
check("재작성의 틀린 환산을 코드가 덮어쓴다",
      rwrel["analysis"]["fiscal_years"] == [_now - 1],
      str(rwrel["analysis"]["fiscal_years"]))
check("rel_year 가 기록된다(비고용)",
      (rwrel["analysis"].get("rel_year") or {}).get("word") == "작년")
_blkrel = R.analysis_block(rwrel["analysis"])
# time_note 는 LLM 출력이라 '기준' 같은 낱말이 들어올 수 있다(픽스처에도 있다).
# 계약은 **코드가** 블록에 환산 설명을 더하지 않는 것 — 회계연도 줄이 값뿐인지,
# 코드가 만드는 '상대 연도 환산' 문구가 없는지를 본다.
check("[질문 분석] 에는 결과만 — 코드가 환산 설명을 더하지 않는다",
      ("- 회계연도: %d" % (_now - 1)) in _blkrel
      and "상대 연도 환산" not in _blkrel, _blkrel[:200])
_trrel = S.render_trace({"rewrite": rwrel, "tool_calls": [], "documents": [],
                         "sources": [], "answer": "x"})
check("비고 줄에만 환산 기록",
      f"상대 연도 환산: 작년 → {_now - 1} (기준 {_now})" in _trrel, _trrel[-120:])
check("비고 이전 줄들에는 환산 설명이 없다",
      "상대 연도 환산" not in _trrel.split("비고:")[0], _trrel[:150])

# (d) '올해' → 2026(분기만 있는 해) → clip 이 이어받는다
fake, st = make_chat({"ok": True, "text": REL_JSON, "tool_calls": [], "usage": {}})
clova.chat = fake
try:
    rwol = R.rewrite_question("삼성전자 올해 실적")
finally:
    clova.chat = real_chat
check("'올해'(2026)는 clip 으로 넘어가 비워지고 분기 안내가 남는다",
      rwol["analysis"]["fiscal_years"] == []
      and "연간 실적이 없다" in (rwol["analysis"].get("range_note") or ""),
      str((rwol["analysis"]["fiscal_years"], rwol["analysis"].get("range_note"))))

# (e) 재작성 프롬프트에 '작년 환산' 문구·날짜가 없다
_pmt = R._prompt() if hasattr(R, "_prompt") else ""
check("rel_year 는 LLM 주입이 안 된다(_DEFAULTS 밖)",
      "rel_year" not in R._DEFAULTS)

print()
print("=" * 74)
print("5-I — 환산됐으면 rewritten 도 맞춘다 (step67)")
print("=" * 74)

_now66 = __import__("datetime").datetime.now().year


def _run66(q, rewritten, years, subs=None, time_note="직전 회계연도 2023으로 해석"):
    payload = json.dumps({"rewritten": rewritten,
                          "sub_questions": subs or [rewritten],
                          "companies": ["삼성전자"], "fiscal_years": years,
                          "ki": [], "doc_groups": [], "time_note": time_note,
                          "out_of_scope": False}, ensure_ascii=False)
    fake66, _ = make_chat({"ok": True, "text": payload, "tool_calls": [], "usage": {}})
    clova.chat = fake66
    try:
        return R.rewrite_question(q)["analysis"]
    finally:
        clova.chat = real_chat


# (a) rewritten 에 틀린 연도 하나 → 환산 결과로 치환
_a66 = _run66("삼성전자 작년 영업이익",
              "삼성전자의 2023년도 영업이익은 얼마입니까?", [2023])
check("연도 1개 → 환산 결과로 치환",
      _a66["rewritten"] == f"삼성전자의 {_now66 - 1}년도 영업이익은 얼마입니까?",
      _a66["rewritten"])
check("하위 질문도 같이 치환", str(_now66 - 1) in _a66["sub_questions"][0])
check("fiscal_years 도 환산 결과", _a66["fiscal_years"] == [_now66 - 1])
check("모델의 time_note 는 지워진다(코드 환산이 우선)", _a66["time_note"] == "")
_blk66 = R.analysis_block(_a66)
check("블록의 재작성 줄이 환산 연도를 말한다",
      f"- 재작성: 삼성전자의 {_now66 - 1}년도" in _blk66
      and "2023년도" not in _blk66, _blk66[:150])

# (b) rewritten 에 연도 없음 → 그대로
_a66 = _run66("삼성전자 작년 영업이익", "삼성전자의 직전 회계연도 영업이익은?", [2024])
check("연도 없음 → rewritten 그대로",
      _a66["rewritten"] == "삼성전자의 직전 회계연도 영업이익은?")

# (c) 연도 둘 이상 → 치환 안 함, 블록에서 rewritten 줄 제거
_a66 = _run66("삼성전자 작년 실적 비교", "삼성전자의 2023년과 2024년 실적 비교",
              [2023, 2024], subs=["2023년 실적은?", "2024년 실적은?"])
check("여러 연도 → 텍스트는 치환하지 않는다",
      _a66["rewritten"] == "삼성전자의 2023년과 2024년 실적 비교")
check("여러 연도 → hide_rewritten", _a66.get("hide_rewritten") is True)
_blk66 = R.analysis_block(_a66)
check("블록에서 재작성·하위 질문 줄이 빠지고 회계연도만 남는다",
      "- 재작성:" not in _blk66 and "- 하위 질문:" not in _blk66
      and ("- 회계연도: %d" % (_now66 - 1)) in _blk66,
      _blk66.replace(chr(10), " | ")[:160])

# (d) 상대 낱말 없는 질문 → 아무것도 안 한다
_a66 = _run66("삼성전자 2024년 영업이익", "삼성전자의 2024년 영업이익", [2024],
              time_note="")
check("상대 낱말 없음 → rewritten·연도 그대로",
      _a66["rewritten"] == "삼성전자의 2024년 영업이익"
      and _a66["fiscal_years"] == [2024] and _a66.get("rel_year") is None)

# (e) 모델이 이미 맞게 환산한 경우 → 치환할 것이 없다
_a66 = _run66("삼성전자 작년 영업이익",
              f"삼성전자의 {_now66 - 1}년 영업이익은?", [_now66 - 1])
check("이미 맞는 연도 → 그대로",
      _a66["rewritten"] == f"삼성전자의 {_now66 - 1}년 영업이익은?")
check("hide_rewritten 은 LLM 주입이 안 된다(_DEFAULTS 밖)",
      "hide_rewritten" not in R._DEFAULTS)

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
