"""서두 군더더기 제거 + 산문형 근거 줄 제거 검증. **LLM 을 부르지 않는다.**

  python eval/test_leadin.py

【왜】 e2e_v4 40문항 실측.
  - **15건**이 "네, 공시 내용을 시간 순서에 따라 정리하겠습니다" 류로 시작했다.
    채점자가 읽는 첫 문장인데 내용이 없다
  - **3건**(29·32·33)이 본문 끝의 산문형 "근거: [...]" 한 줄과 코드가 붙인
    "근거" 절을 **둘 다** 달고 나갔다

【가르는 기준 — 낱말만으로는 못 가른다】
"네, 반대한 이사가 있었습니다" 는 낱말이 걸려도 **답을 담은 문장**이라 남겨야 한다.
그래서 두 조건을 **모두** 본다 — 예고 낱말이 있고, **예고 어미**(…정리하겠습니다,
…다음과 같습니다, 콜론으로 끝남)로 끝날 때만 지운다.
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

from src.agent.loop import (attach_sources, strip_boilerplate,  # noqa: E402
                            strip_leadin, strip_prose_geun,
                            strip_sources_section)

FAIL = []
S_LGU = "[LG유플러스 사업보고서 (2024.12), 2025-03-17, 접수번호 20250317000701]"


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


print("=" * 74)
print("1 — 지워야 할 서두 (e2e_v4 실제 첫 줄에서 골랐다)")
print("=" * 74)

BODY = "2024년 연결 매출액은 300,870,903백만원입니다."
GO = [
    ("1번", "네, 공시 내용을 시간 순서에 따라 정리하겠습니다."),
    ("10번", "요청하신 내용을 표로 정리하겠습니다."),
    ("13번", "삼성E&A에 대한 요청하신 정보는 다음과 같습니다:"),
    ("9번", "아래는 POSCO홀딩스와 관련하여 요청하신 정보를 표로 정리한 것입니다."),
    ("38번", "포스코홀딩스의 사업 구조와 주요 사업부문에 대해 아래와 같이 요약했습니다."),
]
for name, head in GO:
    got = strip_leadin(head + "\n" + BODY)
    check(f"지운다 — {name}", got.strip() == BODY, repr(got[:70]))

print()
print("=" * 74)
print("2 — 지우면 안 되는 서두 (사실이 함께 있다)")
print("=" * 74)

STAY = [
    ("판정을 담은 문장", "네, 반대한 이사가 있었습니다."),
    ("수치를 담은 문장", "네, 2024년 연결 매출액은 300,870,903백만원입니다."),
    ("거절도 사실이다", "요청하신 내용은 공시에서 확인되지 않습니다."),
]
for name, head in STAY:
    got = strip_leadin(head + "\n" + BODY)
    check(f"남긴다 — {name}", got.startswith(head), repr(got[:60]))

check("예고 낱말이 없으면 손대지 않는다",
      strip_leadin("제77기 매출액이다.\n" + BODY).startswith("제77기"))
check("첫 줄만 본다 — 본문 중간의 같은 표현은 그대로",
      "다음과 같습니다:" in strip_leadin(BODY + "\n세부 내역은 다음과 같습니다:\n- 가"),
      repr(strip_leadin(BODY + "\n세부 내역은 다음과 같습니다:\n- 가")))
check("첫 줄뿐이면 지우지 않는다(빈 답 금지)",
      strip_leadin("요청하신 내용을 표로 정리하겠습니다.").strip() != "")
for bad in (None, "", "   "):
    try:
        strip_leadin(bad)
        ok = True
    except Exception:                          # noqa: BLE001
        ok = False
    check(f"strip_leadin({bad!r}) 예외 없음", ok)

check("strip_boilerplate 를 지나면 서두도 사라진다",
      strip_boilerplate("네, 다음과 같이 정리하겠습니다.\n" + BODY).strip() == BODY,
      repr(strip_boilerplate("네, 다음과 같이 정리하겠습니다.\n" + BODY)[:60]))

print()
print("=" * 74)
print("3 — 산문형 근거 줄 (e2e_v4 29·32·33번)")
print("=" * 74)

dup = f"{BODY}\n\n근거: {S_LGU}"
check("마지막 줄의 산문형 근거를 뗀다", strip_prose_geun(dup).strip() == BODY,
      repr(strip_prose_geun(dup)))
check("'출처:' 도 뗀다",
      strip_prose_geun(f"{BODY}\n출처: {S_LGU}").strip() == BODY)
check("접수번호만 있어도 뗀다",
      strip_prose_geun(f"{BODY}\n근거: 20250317000701").strip() == BODY)
check("여러 개 나열해도 뗀다",
      strip_prose_geun(f"{BODY}\n근거: {S_LGU}, {S_LGU}").strip() == BODY)

check("서술이 섞이면 남긴다",
      "근거: 상법 제391조" in strip_prose_geun(f"{BODY}\n근거: 상법 제391조에 따라 판단했다."),
      repr(strip_prose_geun(f"{BODY}\n근거: 상법 제391조에 따라 판단했다.")))
check("본문 중간의 '근거:' 는 건드리지 않는다",
      "근거: " in strip_prose_geun(f"근거: {S_LGU}\n{BODY}"),
      repr(strip_prose_geun(f"근거: {S_LGU}\n{BODY}")))
check("근거 줄이 없으면 그대로", strip_prose_geun(BODY).strip() == BODY)
for bad in (None, "", "  "):
    try:
        strip_prose_geun(bad)
        ok = True
    except Exception:                          # noqa: BLE001
        ok = False
    check(f"strip_prose_geun({bad!r}) 예외 없음", ok)

print()
print("=" * 74)
print("4 — 근거가 두 번 나오지 않는다 (29번 모양 그대로)")
print("=" * 74)

model_out = (f"{BODY}\n\n근거: {S_LGU}")
body = strip_sources_section(model_out)
check("strip_sources_section 이 산문형도 뗀다", "근거:" not in body, repr(body))

final, used = attach_sources(model_out, [S_LGU])
check("근거가 한 번만 나온다", final.count("근거") == 1, repr(final))
check("그 근거는 목록형이다", "\n근거\n- [" in final, repr(final[-90:]))
check("출처는 인용으로 잡힌다", used == [S_LGU], str(used))
print()
print("  --- 고친 뒤 ---")
for ln in final.splitlines():
    print("   ", ln)

# 목록형 + 산문형이 함께 있는 경우
both = f"{BODY}\n\n근거\n- {S_LGU}\n\n근거: {S_LGU}"
final2, _ = attach_sources(both, [S_LGU])
check("목록형·산문형이 함께 있어도 한 번만", final2.count("근거") == 1, repr(final2))

print()
print("=" * 74)
print("3개년 제시 형식 (step59) — 후처리가 지우지 않는지")
print("=" * 74)

from src.agent import loop as _L  # noqa: E402

THREE = """SK하이닉스의 연결 매출액입니다.
- 2025년(제77기) 66,192,960백만원
- 2024년(제76기) 32,765,719백만원
- 2023년(제75기) 44,621,568백만원

질문에 연도가 없어 최근 3개 사업연도를 함께 제시했습니다. 특정 연도를 원하시면 알려주시면 그 기준으로 답변드리겠습니다."""

_kept = _L.strip_boilerplate(THREE)
check("strip_boilerplate 가 '알려주시면' 을 지우지 않는다", "알려주시면" in _kept)
check("strip_boilerplate 가 '답변드리겠습니다' 를 지우지 않는다",
      "답변드리겠습니다" in _kept)
check("3개년 목록 세 줄이 그대로", _kept.count("년(제") == 3, _kept[:120])
check("strip_leadin 이 첫 줄을 지우지 않는다",
      "연결 매출액입니다" in _L.strip_leadin(THREE), _L.strip_leadin(THREE)[:60])
check("실제 값이면 자리표시자로 안 잡힌다",
      _L.strip_placeholders(THREE).count("년(제") == 3)

# 예시를 그대로 베끼면 걷어낸다
COPIED = """ㅇㅇ전자의 연결 매출액입니다.
- 2025년(제NN기) 000,000,000백만원
- 2024년(제NN기) 000,000,000백만원

근거
- [○○기업 사업보고서 (2024.12), 2025-03-11, 접수번호 20250311XXXXXX]"""
check("예시를 베낀 '제NN기' 문단은 제거된다",
      "제NN기" not in _L.strip_placeholders(COPIED),
      _L.strip_placeholders(COPIED)[:120])
check("'000,000,000' 도 제거된다",
      "000,000,000" not in _L.strip_placeholders(COPIED))
check("PLACEHOLDERS 에 두 자리표시자가 있다",
      "제NN기" in _L.PLACEHOLDERS and "000,000,000" in _L.PLACEHOLDERS)

print()
print("=" * 74)
if FAIL:
    print(f"{len(FAIL)}건 실패: " + ", ".join(FAIL))
    sys.exit(1)
print("전부 통과 · LLM 실제 호출 0회")
