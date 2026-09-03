"""반기·분기보고서 경고 줄 검증. **LLM 도 인덱스도 부르지 않는다.**

  python eval/test_period_note.py

왜 이 검증이 있나 — API 실측에서 "SK하이닉스 2024년 연결 매출액" 에
**반기보고서(2024.06)의 상반기 매출**을 답했다. 문서가 반기보고서라는 사실은
source 라벨에 적혀 있었는데도 모델이 그냥 썼다. 그래서 정정 경고와 같은 방식으로
**<document> 바로 윗줄**에 "이건 연간 확정치가 아니다" 를 붙인다.

여기서 확인하는 것
  - 반기·분기보고서에만 붙는다 (사업보고서·이벤트 공시에는 안 붙는다)
  - [기재정정] 접두가 붙어도 알아본다
  - report_nm 에 (YYYY.MM) 이 없어도 죽지 않는다
  - build_context 안에서 정정 경고와 함께, <document> 바깥 윗줄에 놓인다
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


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def res(report_nm, text="매출액 12,394,044", preserved=False, **src):
    """검색 결과 한 건 모양. 기수(제NN기)를 안 넣어야 annotate 가 문서표를 안 읽는다."""
    return {"text": text, "preserved": preserved, "chunk_id": "c1", "doc_id": "d1",
            "source": {"corp_name": "SK하이닉스", "report_nm": report_nm,
                       "rcept_dt": "20240814", "rcept_no": "20240814000123",
                       "section": "III. 재무에 관한 사항 > 연결 손익계산서",
                       "note": "", **src}}


print("=" * 74)
print("1 — 어떤 보고서에 붙는가")
print("=" * 74)

n = C.period_note(res("반기보고서 (2024.06)"))
check("반기보고서에 붙는다", bool(n), (n or "").splitlines()[0][:60])
check("반기라고 말한다", "반기보고서" in (n or ""))
check("연간 확정치가 아니라고 말한다", "연간" in (n or "") and "아니" in (n or ""))
check("2024 사업연도를 짚는다", "2024" in (n or ""))
check("무엇을 해야 하는지 말한다", 'report_type="annual"' in (n or ""))
check("추정임을 밝힌다", (n or "").startswith("※ 자동 주석(공시 원문 아님)"))

n = C.period_note(res("분기보고서 (2024.09)"))
check("분기보고서에 붙는다", bool(n), (n or "").splitlines()[0][:60])

check("사업보고서에는 안 붙는다", C.period_note(res("사업보고서 (2024.12)")) is None)
check("주요사항보고서에는 안 붙는다",
      C.period_note(res("주요사항보고서(유상증자결정)")) is None)
check("거래소공시에는 안 붙는다",
      C.period_note(res("단일판매ㆍ공급계약체결")) is None)
check("감사보고서에는 안 붙는다", C.period_note(res("감사보고서")) is None)

print()
print("=" * 74)
print("2 — 지저분한 입력에도 안 죽는다")
print("=" * 74)

n = C.period_note(res("[기재정정]반기보고서 (2024.06)"))
check("[기재정정] 접두가 붙어도 알아본다", bool(n))

n = C.period_note(res("반기보고서"))
check("(YYYY.MM) 이 없어도 붙는다", bool(n), (n or "")[:60])
check("없는 연도를 지어내지 않는다", "None" not in (n or "") and "-1" not in (n or ""))

check("source 가 통째로 없어도 None", C.period_note({"text": "x"}) is None)
check("report_nm 이 없어도 None", C.period_note({"source": {}}) is None)
check("빈 dict 도 None", C.period_note({}) is None)

print()
print("=" * 74)
print("3 — build_context 안에서의 자리")
print("=" * 74)

ctx = C.build_context([res("반기보고서 (2024.06)")])
head = ctx.splitlines()[0]
check("경고가 첫 줄이다", head.startswith("※ 자동 주석"), head[:70])
check("<document> 태그 바깥이다", ctx.index("※") < ctx.index("<document"))
check("본문은 그대로", "12,394,044" in ctx)

ctx = C.build_context([res("사업보고서 (2024.12)")])
check("사업보고서에는 경고가 없다", "연간 확정치" not in ctx)

ctx = C.build_context([res("[기재정정]반기보고서 (2024.06)", preserved=True)])
check("정정 경고와 기간 경고가 함께 나온다",
      "정정 전 자료" in ctx and "연간 확정치" in ctx)
check("정정 경고가 먼저다", ctx.index("정정 전 자료") < ctx.index("연간 확정치"))

print()
print("=" * 74)
if FAIL:
    print(f"{len(FAIL)}건 실패: " + ", ".join(FAIL))
    sys.exit(1)
print("전부 통과 · LLM 실제 호출 0회")
