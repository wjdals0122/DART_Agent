"""「공시내용 진행 및 변경사항」 표 경고 검증. **LLM 도 인덱스도 부르지 않는다.**

  python eval/test_progress_note.py

왜 이 검증이 있나 — HD현대일렉트릭 사례(2026-09-03 조사, docs/WORKLOG.md step8-C).
정기보고서 「XI. 그 밖에 … > 1. 공시내용 진행 및 변경사항」의 진행상황 표에
계약금액총액 **583억원**, 계약기간 **'24.1.30 ~ '26.1.30** 이 실려 있었다.
그런데 그 계약은 2025-02-25 정정공시로 **644억원 / 2026-08-31** 로 바뀐 뒤였다.
사업보고서 접수일은 2025-03-20 으로 정정보다 **뒤인데도** 표가 갱신되지 않았다.

그 표에는 '정정 전' · '정정 후' 열이 **없다**. 열을 잘못 고른 게 아니라
**표 자체가 옛 값을 담고 있다.** 그래서 모델에게 "이 표는 요약이고 최신 확정치가
아닐 수 있다" 를 표 바로 위에서 알려 준다 — period_note 와 같은 방식이다.

여기서 확인하는 것
  - 그 절에만 붙는다 (재무제표·사업의 내용 절에는 안 붙는다)
  - 절 이름의 띄어쓰기·번호가 달라도 알아본다
  - source 가 없어도 죽지 않는다
  - build_context 안에서 <document> 바깥 윗줄에 놓인다
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

from src.agent import context as C

FAIL = []
SEC = ("[HD현대일렉트릭] [기재정정]사업보고서 (2024.12) > "
       "XI. 그 밖에 투자자 보호를 위하여 필요한 사항 > 1. 공시내용 진행 및 변경사항")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def res(section, report_nm="[기재정정]사업보고서 (2024.12)", text="| '24.1.30 | 583억원 |"):
    """기수(제NN기)를 안 넣어야 annotate 가 문서표를 안 읽는다."""
    return {"text": text, "preserved": False, "chunk_id": "c1", "doc_id": "d1",
            "source": {"corp_name": "HD현대일렉트릭", "report_nm": report_nm,
                       "rcept_dt": "20250320", "rcept_no": "20250320001309",
                       "section": section, "note": ""}}


print("=" * 74)
print("1 — 어느 절에 붙는가")
print("=" * 74)

n = C.progress_note(res(SEC))
check("진행 및 변경사항 절에 붙는다", bool(n), (n or "").splitlines()[0][:60])
# 2026-09-04 문구 교체 — "쓰지 마라" 에서 "어떻게 읽어라" 로.
# 옛 문구("최신 확정치가 아닐 수 있다")는 표에서 멀어지게 만들었고,
# e2e 2번의 정답이 바로 그 표에 있었는데 모델이 안 봤다.
check("표를 어떻게 읽는지 말한다", "(변경공시" in (n or ""), (n or "")[:80])
check("변경 반영값이라고 말한다", "반영된 값" in (n or ""))
check("표기가 없을 때를 따로 말한다", "표기가 없어도" in (n or ""))
check("어디서 대조할지 말한다", "거래소공시" in (n or ""))
check("'쓰지 마라' 로 읽히지 않는다", "최신 확정치가 아닐 수 있다" not in (n or ""))
check("추정임을 밝힌다", (n or "").startswith("※ 자동 주석(공시 원문 아님)"))

check("재무제표 절에는 안 붙는다",
      C.progress_note(res("III. 재무에 관한 사항 > 2. 연결재무제표")) is None)
check("사업의 내용 절에는 안 붙는다",
      C.progress_note(res("II. 사업의 내용 > 2. 주요 제품 및 서비스")) is None)
check("거래소공시 본문에는 안 붙는다",
      C.progress_note(res("단일판매ㆍ공급계약 체결",
                          report_nm="[기재정정]단일판매ㆍ공급계약체결")) is None)

print()
print("=" * 74)
print("2 — 절 이름이 조금 달라도 알아본다")
print("=" * 74)

for s in ("XI. 그 밖에 투자자 보호를 위하여 필요한 사항 > 1. 공시내용의 진행 및 변경사항",
          "XI. 그 밖에… > 공시내용진행및변경사항",
          "X. 그 밖에 투자자 보호를 위하여 필요한 사항 > 2. 공시 내용의 진행 및 변경 사항"):
    check(f"'{s[-22:]}'", bool(C.progress_note(res(s))))

check("'변경사항' 만 있으면 안 붙는다",
      C.progress_note(res("VIII. 임원 및 직원 등에 관한 사항 > 변경사항")) is None)

print()
print("=" * 74)
print("3 — 지저분한 입력")
print("=" * 74)

check("source 가 통째로 없어도 None", C.progress_note({"text": "x"}) is None)
check("section 이 없어도 None", C.progress_note({"source": {}}) is None)
check("빈 dict 도 None", C.progress_note({}) is None)

print()
print("=" * 74)
print("4 — build_context 안에서의 자리")
print("=" * 74)

ctx = C.build_context([res(SEC)])
lines = ctx.splitlines()
doc_i = next((i for i, l in enumerate(lines) if l.startswith('<document id="1"')), -1)
check("경고가 <document> 앞에 있다",
      doc_i > 0 and "(변경공시" in "\n".join(lines[:doc_i]), repr(lines[:doc_i]))
check("본문은 그대로", "583억원" in ctx)

ctx2 = C.build_context([res("III. 재무에 관한 사항 > 2. 연결재무제표")])
check("다른 절에는 경고가 없다", "(변경공시" not in ctx2)

print()
print("=" * 74)
print("진행상황 표 선조회 (step54) — LLM·인덱스 0회")
print("=" * 74)

from src.agent import loop as L  # noqa: E402

# (a) 조건 매칭 — 부르는 질문 4종
for _q in ["HD현대일렉트릭이 2024년 1월 30일 체결한 고압차단기 공급계약의 최종 계약금액은?",
           "수주 금액이 정정된 내역 알려줘",
           "그 투자 계획은 변경됐어?",
           "공급계약 진행 상황이 어떻게 돼?"]:
    check(f"선조회 O: {_q[:26]}", L.needs_progress_prefetch(_q) is True)

# (b) 조건 매칭 — 안 부르는 질문 4종
for _q in ["삼성전자 2024년 연결 매출액이 얼마야?",          # 주제·확정성 둘 다 없음
           "HD현대일렉트릭의 고압차단기 공급계약 상대방은?",     # 주제만 있음
           "최종 배당금은 얼마야?",                          # 확정성만 있음
           "임원 현황 알려줘"]:
    check(f"선조회 X: {_q[:26]}", L.needs_progress_prefetch(_q) is False)

check("빈 입력·None 에 예외 없음",
      L.needs_progress_prefetch("") is False and L.needs_progress_prefetch(None) is False)

# (c) 회사가 특정되지 않으면 부르지 않는다
_called = {"n": 0}


def _fake_progress(corp_name=None, **kw):
    _called["n"] += 1
    return {"status": "ok", "note": "", "sources": ["[HD현대일렉트릭 …]"],
            "results": [{"chunk_id": "c1", "text": "계약금액총액 644억원",
                         "source": {"corp_name": "HD현대일렉트릭",
                                    "report_nm": "사업보고서 (2024.12)",
                                    "rcept_no": "20250320000001",
                                    "rcept_dt": "20250320",
                                    "section": "XI. … > 1. 공시내용 진행 및 변경사항"}}]}


def _fresh_out(companies):
    return {"sources": [], "documents": [], "tool_calls": [], "tool_seconds": 0.0,
            "waste": {"progress_prefetch": False},
            "rewrite": {"ok": bool(companies), "analysis":
                        ({"companies": companies} if companies else None)}}


import src.tools as T  # noqa: E402
_real = T.TOOLS.get("get_progress_table")
T.TOOLS["get_progress_table"] = _fake_progress
try:
    Q = "고압차단기 공급계약의 최종 계약금액은?"
    _called["n"] = 0
    o = _fresh_out(None)
    pre = L._prefetch_progress(Q, o)
    check("회사가 없으면 호출하지 않는다", pre == "" and _called["n"] == 0,
          f"pre={pre[:30]!r} calls={_called['n']}")
    check("그 경우 tool_calls 도 안 늘어난다", o["tool_calls"] == [])

    # (d) 정상 경로
    _called["n"] = 0
    o = _fresh_out(["HD현대일렉트릭"])
    pre = L._prefetch_progress(Q, o)
    check("조건이 맞으면 1회 호출된다", _called["n"] == 1, str(_called["n"]))
    check("선조회 안내가 앞에 붙는다", pre.startswith("[선조회]"), pre[:40])
    check("표 본문이 들어간다", "644억원" in pre, pre[-80:])
    check("tool_calls 에 기록된다", len(o["tool_calls"]) == 1
          and o["tool_calls"][0]["name"] == "get_progress_table")
    check("think_trace 용 '진행상황 표 선조회' 문구가 남는다",
          "진행상황 표 선조회" in o["tool_calls"][0]["summary"],
          o["tool_calls"][0]["summary"])
    check("호출 한도 밖임을 n=0 으로 표시한다", o["tool_calls"][0]["n"] == 0)
    check("documents 에 청크가 들어간다", len(o["documents"]) == 1)
    check("sources 가 합쳐진다", o["sources"] == ["[HD현대일렉트릭 …]"])
    check("waste 플래그가 선다", o["waste"]["progress_prefetch"] is True)

    # (e) 조건에 안 맞으면 회사가 있어도 안 부른다
    _called["n"] = 0
    o = _fresh_out(["삼성전자"])
    pre = L._prefetch_progress("삼성전자 2024년 연결 매출액이 얼마야?", o)
    check("조건 불일치면 호출 0회", pre == "" and _called["n"] == 0)

    # (f) not_found 면 조용히 넘어간다
    T.TOOLS["get_progress_table"] = lambda **kw: {"status": "not_found",
                                                  "note": "표를 찾지 못했다",
                                                  "results": [], "sources": []}
    o = _fresh_out(["HD현대일렉트릭"])
    pre = L._prefetch_progress(Q, o)
    check("not_found 면 아무것도 넣지 않는다",
          pre == "" and o["tool_calls"] == [] and o["documents"] == []
          and o["sources"] == [], f"pre={pre[:30]!r}")

    # (g) 도구가 터져도 답을 막지 않는다
    def _boom(**kw):
        raise RuntimeError("터짐")

    T.TOOLS["get_progress_table"] = _boom
    o = _fresh_out(["HD현대일렉트릭"])
    check("도구 예외를 삼키고 빈 문자열", L._prefetch_progress(Q, o) == "")
finally:
    if _real is not None:
        T.TOOLS["get_progress_table"] = _real

print()
print("=" * 74)
if FAIL:
    print(f"{len(FAIL)}건 실패: " + ", ".join(FAIL))
    sys.exit(1)
print("전부 통과 · LLM 실제 호출 0회")
