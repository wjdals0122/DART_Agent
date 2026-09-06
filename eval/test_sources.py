"""근거 절 선별 + 상투구 제거 검증. **LLM 을 부르지 않는다.**

  python eval/test_sources.py

【왜】 회귀 6번에서 두 가지가 한꺼번에 드러났다.
  - '삼성전자 이사회에서 반대한 이사가 있었나' 에 **자기주식취득결정 5건**이 근거로
    달렸다. 답변 본문은 그 문서들을 한 번도 가리키지 않았다. 후처리가
    "하나도 안 맞으면 전부 붙인다" 였기 때문이다
  - "관련 기관에 문의하시기 바랍니다" 가 그대로 남았다. REVIEW 는 코드 필터가
    아니라 모델에게 보내는 지시문일 뿐이라 지켜지지 않았다
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

import os  # noqa: E402

# 후처리(근거 절)만 시험한다 — 재작성 단계가 켜지면 answer() 경로의 턴 수가 밀린다
os.environ["DART_REWRITE"] = "0"

from src.agent.loop import (attach_sources, scope_line,  # noqa: E402
                            strip_boilerplate, used_sources)

FAIL = []
S_SAM = "[삼성전자 사업보고서 (2024.12), 2025-03-11, 접수번호 20250311001085]"
S_HYN = "[SK하이닉스 사업보고서 (2024.12), 2025-03-19, 접수번호 20250319000665]"
S_TREA = "[삼성전자 주요사항보고서(자기주식취득결정), 2025-02-18, 접수번호 20250218000019]"


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


print("=" * 74)
print("1 — 본문이 가리킨 출처만 고른다")
print("=" * 74)

body = ("2024년 연결 매출액은 300,870,903백만원이다 "
        "[삼성전자 사업보고서 (2024.12), 2025-03-11, 접수번호 20250311001085].")
check("접수번호로 인용", used_sources(body, [S_SAM, S_HYN]) == [S_SAM])

check("라벨 통째로 인용", used_sources(f"근거는 {S_HYN} 이다.", [S_SAM, S_HYN]) == [S_HYN])
check("회사명+보고서명만 적어도 인용",
      used_sources("삼성전자 사업보고서에 따르면 매출은 300조다.", [S_SAM, S_HYN]) == [S_SAM])
check("둘 다 인용하면 둘 다",
      used_sources(f"{S_SAM} 와 {S_HYN}", [S_SAM, S_HYN]) == [S_SAM, S_HYN])

refuse = ("삼성전자 이사회에서 반대한 이사가 있었는지는 공시에서 확인되지 않습니다.")
# 【2026-09-04 되돌림】 하나도 못 찾으면 전부 붙인다.
# step11 에서 이걸 없앴더니 e2e 40문항 중 21문항이 근거 절 없이 나갔다.
# 그 21문항은 전부 검색이 ok 였다 — 근거가 없는 게 아니라 본문이 안 가리켰을 뿐이다.
# 무관한 문서가 섞이는 손해(회귀 6번 자기주식 5건)보다 그쪽 손해가 크다.
check("본문이 안 가리키면 전부 붙인다(되돌림)",
      used_sources(refuse, [S_TREA]) == [S_TREA],
      repr(used_sources(refuse, [S_TREA])))
check("무관한 문장이어도 전부 붙는다",
      used_sources("무관한 문장.", [S_SAM, S_HYN]) == [S_SAM, S_HYN])

print()
print("=" * 74)
print("2 — 근거 절이 비면 조회 범위를 적는다")
print("=" * 74)

out, used = attach_sources(refuse, [S_TREA, S_TREA.replace("0019", "0020")])
check("본문이 안 가리켜도 근거가 붙는다", len(used) == 2, str(len(used)))
check("근거 절이 있다", "\n근거\n" in out, repr(out[-90:]))
check("조회 범위 줄은 안 쓴다", "조회:" not in out, repr(out.splitlines()[-1]))
check("본문은 그대로", refuse.split("는")[0] in out)

# sources 가 아예 없을 때 근거 절을 만들지 않는 것은 그대로다
out_e, used_e = attach_sources(refuse, [])
check("출처 0건이면 근거 절 없음", "\n근거\n" not in out_e and used_e == [],
      repr(out_e[-60:]))

line = scope_line([S_SAM, S_HYN], "매출액은 300조다.")
check("여러 회사를 나열한다", "삼성전자·SK하이닉스" in line, line)
check("정기공시로 종류를 밝힌다", "정기공시 2건" in line, line)
check("거절이 아니면 다른 꼬리말", "답변에 인용된 문서 없음" in line, line)

out2, used2 = attach_sources(body, [S_SAM, S_HYN])
check("인용한 것만 근거 절에", used2 == [S_SAM], str(used2))
check("안 쓴 출처는 빠진다", "20250319000665" not in out2.split("근거")[-1])

# 모델은 출처를 본문 중간이 아니라 **자기가 쓴 근거 절에** 적는 일이 많다.
# 그 절을 먼저 떼고 판정하면 맞는 답에서도 근거가 통째로 사라진다(실측 4/7).
model_out = ("삼성전자의 2024년 연결 매출액은 300,870,903백만원입니다.\n\n"
             f"근거\n- {S_SAM}\n")
out3, used3 = attach_sources(model_out, [S_SAM, S_HYN])
check("모델이 자기 근거 절에만 적어도 인용으로 본다", used3 == [S_SAM], str(used3))
check("그 경우 조회 범위로 바뀌지 않는다", "조회:" not in out3, repr(out3[-80:]))

print()
print("=" * 74)
print("3 — 상투구는 지우고 정상 문장은 남긴다")
print("=" * 74)

# 지워야 할 4종
GO = [("관련 기관에 문의", "추가 정보가 필요하시면 관련 기관에 문의하시기 바랍니다."),
      ("전문가와 상담", "구체적인 판단은 전문가와 상담하십시오."),
      ("확인하시기 바랍니다", "자세한 내용은 공시 원문을 확인하시기 바랍니다."),
      ("참고하시기 바랍니다", "이 점 참고하시기 바랍니다.")]
for name, sent in GO:
    got = strip_boilerplate("2024년 매출은 300조다. " + sent)
    check(f"지운다 — {name}", sent not in got and "300조" in got, repr(got))

# 남겨야 할 2종
STAY = [("수치 문장", "2024년 연결 매출액은 300,870,903백만원입니다."),
        ("거절 문장", "삼성전자 이사회 표결 내역은 공시에서 확인되지 않습니다.")]
for name, sent in STAY:
    got = strip_boilerplate(sent)
    check(f"남긴다 — {name}", got.strip() == sent, repr(got))

# step33 추가 3종 — "공식 홈페이지나 언론 보도 등을 참고하시는 것을
# 추천드립니다" 가 실측으로 남았다
GO33 = [("참고하시는 것을 추천",
         "공식 홈페이지나 언론 보도 등을 참고하시는 것을 추천드립니다."),
        ("문의하시는 것을 권장", "자세한 내용은 회사에 문의하시는 것을 권장합니다."),
        ("확인해 보시기 바랍니다", "공시 원문을 확인해 보시기 바랍니다.")]
for name, sent in GO33:
    got = strip_boilerplate("2024년 매출은 300조다. " + sent)
    check(f"지운다(step33) — {name}", sent not in got and "300조" in got, repr(got))
STAY33 = [("확인 서술문", "감사인은 연결재무제표를 직접 확인해 보았다."),
          ("참고 서술문", "이 값은 사업보고서 주석을 참고해 산출한 수치다.")]
for name, sent in STAY33:
    got = strip_boilerplate(sent)
    check(f"남긴다(step33) — {name}", got.strip() == sent, repr(got))

# step46 — 정정 전 값 대조 문장 제거. 프롬프트([정정공시] 절)로 두 번 샌 자리.
from src.agent.loop import strip_correction_contrast  # noqa: E402

GO46 = [
    ("기존 …에서 증가 — 회귀 7번 실측",
     "최종 계약금액은 644억원입니다. 이는 정정공시를 통해 기존 583억원에서 증가한 금액입니다.",
     "644", "583"),
    ("정정 전 …에서 변경 — 회귀 3번 유형",
     "정정 후 총자본은 57,846십억원이다. 정정 전 57,824십억원에서 변경되었다.",
     "57,846", "57,824"),
    ("'이전' + 값",
     "계약금액은 644억원이다. 이전 계약금액 583억원과는 다르다.",
     "644", "583"),
    ("'에서 감소'",
     "확정 취득금액은 37,413백만원이다. 기존 40,000백만원에서 감소하였다.",
     "37,413", "40,000"),
]
for name, sent, keep, drop in GO46:
    got = strip_correction_contrast(sent, "최종 계약금액은?")
    check(f"지운다(step46) — {name}",
          keep in got and drop not in got, repr(got))

STAY46 = [
    ("정상 답변", "2024년 연결 매출액은 300,870,903백만원입니다.",
     "매출액이 얼마야?"),
    ("질문이 대조를 요구", "최종 계약금액은 644억원입니다. 기존 583억원에서 증가한 금액입니다.",
     "정정 전 금액과 정정 후 금액이 얼마에서 얼마로 바뀌었어?"),
    ("'이전' + 연도만(값 없음)", "2023년 이전의 공시는 확인되지 않습니다.",
     "공시 이력 알려줘"),
]
for name, sent, q in STAY46:
    got = strip_correction_contrast(sent, q)
    check(f"남긴다(step46) — {name}", got.strip() == sent.strip(), repr(got))

check("지워서 값이 안 남으면 원문 유지(step46)",
      strip_correction_contrast("기존 583억원에서 증가했습니다.", "질문")
      == "기존 583억원에서 증가했습니다.")
for bad in (None, "", "  "):
    try:
        strip_correction_contrast(bad, None)
        ok = True
    except Exception:                          # noqa: BLE001
        ok = False
    check(f"strip_correction_contrast({bad!r}) 예외 없음(step46)", ok)

# step72 — 낱말 목록의 사각지대를 정규식으로 메운다 (오늘 세 번째 관측)
GO72 = [
    ("'에서 N으로 증가' — 회귀 7번 FAIL 의 유일한 경로",
     "최종 계약금액은 644억원입니다. 계약금액이 583억원에서 644억원으로 "
     "증가했다는 내용을 확인할 수 있습니다.", "583"),
    ("'최초 …이었으나' — step54 회귀 1회차 실측",
     "최종 계약금액은 644억원입니다. 이 계약의 최초 계약금액은 583억원이었으나, "
     "변경공시를 통해 64억원이 추가되어 최종적으로 644억원이 되었습니다.", "583"),
    ("'에서 N으로 변경'",
     "확정 금액은 644억원이다. 583억원에서 644억원으로 변경되었다.", "583"),
    ("'당초 …에서'",
     "최종 취득금액은 37,413백만원이다. 당초 계획 40,000백만원에서 줄었다.",
     "40,000"),
]
for name, sent, drop in GO72:
    got = strip_correction_contrast(sent, "최종 계약금액은?")
    check(f"step72 지운다 — {name}",
          drop not in got and "644억원" in got or
          (drop not in got and "37,413" in got), repr(got))

STAY72 = [
    ("'최초' 단독(숫자 없음)", "최초 공시는 그대로다. 값은 644억원이다."),
    ("'최초' 와 숫자 사이 단어 3개 이상(먼 숫자)",
     "최초 공시된 사업 계획 전체 예산은 644억원이다."),
    ("질문이 대조를 요구하면 그대로",
     "계약금액이 583억원에서 644억원으로 증가했다."),
]
for name, sent in STAY72:
    q72 = ("정정 전후 얼마에서 얼마로 바뀌었어?"
           if "요구" in name else "최종 계약금액은?")
    got = strip_correction_contrast(sent, q72)
    check(f"step72 남긴다 — {name}", got.strip() == sent.strip(), repr(got))

check("step72 — 값 보존 가드 유지(지우면 값이 사라지는 경우 원문)",
      strip_correction_contrast("583억원에서 644억원으로 증가했다.", "질문")
      == "583억원에서 644억원으로 증가했다.")

# step60 — 소수점을 문장 끝으로 봐서 답변이 문장 중간부터 시작한 사고.
# V4 실호출 실측: answer 가 "55배** 큰 금액입니다." 로 시작해 나갔다.
V4_ORIG = "\n".join([
    "HD현대일렉트릭이 2024년 1월 30일에 체결한 고압차단기 공급계약의 "
    "최종 계약금액은 644억원입니다. 이는 정정 전 583억원보다 약 **4.55배** 큰 금액입니다.",
    "",
    "근거",
    "- [HD현대일렉트릭 분기보고서 (2026.03), 2026-05-15, 접수번호 20260515002315]",
])
_v4 = strip_correction_contrast(V4_ORIG, "고압차단기 공급계약의 최종 계약금액은?")
check("step60 — 문장 중간 조각('55배**')이 남지 않는다",
      "55배**" not in _v4, repr(_v4[:120]))
check("step60 — 대조 문장은 통째로 지워진다", "정정 전 583" not in _v4, repr(_v4[:120]))
check("step60 — 확정값 644 는 남는다", "644억원" in _v4, repr(_v4[:120]))
check("step60 — 근거 절이 남는다", "근거" in _v4 and "20260515002315" in _v4)
check("step60 — 첫 글자가 회사명이다(문장 중간 시작 아님)",
      _v4.startswith("HD현대일렉트릭"), repr(_v4[:40]))

from src.agent.loop import (_MIN_KEEP_RATIO, _RATIO_MIN_SENTS, _SENT,  # noqa: E402
                            _SENT_NUMSAFE)

_DEC = "약 **4.55배** 큰 금액입니다."
check("step60 — 소수점은 문장을 끊지 않는다",
      _SENT_NUMSAFE.findall(_DEC) == [_DEC], str(_SENT_NUMSAFE.findall(_DEC)))
check("step60 — 옛 _SENT 는 실제로 갈랐다(회귀 근거)",
      len(_SENT.findall(_DEC)) == 2, str(_SENT.findall(_DEC)))
check("step60 — 문장 끝 마침표는 여전히 끊는다",
      len(_SENT_NUMSAFE.findall("첫 문장이다. 둘째 문장이다.")) == 2,
      str(_SENT_NUMSAFE.findall("첫 문장이다. 둘째 문장이다.")))
check("step60 — 숫자 뒤 마침표 + 공백은 문장 끝이다",
      len(_SENT_NUMSAFE.findall("금액은 644. 다음 문장이다.")) == 2)
check("step60 — 소수 여러 개도 안 끊긴다",
      _SENT_NUMSAFE.findall("1.5배에서 2.75배로 늘었다.")
      == ["1.5배에서 2.75배로 늘었다."])

# 3문장 이상인데 절반 미만으로 줄면 아예 지우지 않는다
_HALF = ("최종 금액은 644억원입니다. 이는 정정 전 583억원에서 증가한 값입니다. "
         "기존 583억원과 비교하면 크게 늘어난 금액입니다.")
_got = strip_correction_contrast(_HALF, "최종 금액은?")
check("step60 — 3문장 이상이 절반 미만으로 줄면 원문 유지",
      _got == _HALF, f"{len(_got)}/{len(_HALF)} {_got!r}")
check("step60 — _MIN_KEEP_RATIO 가 0.5", _MIN_KEEP_RATIO == 0.5)
check("step60 — 두 문장짜리는 비율 안전장치를 타지 않는다(step46 유지)",
      strip_correction_contrast(
          "최종 계약금액은 644억원입니다. 기존 583억원에서 증가했습니다.", "최종 금액은?")
      .strip() == "최종 계약금액은 644억원입니다.",
      repr(strip_correction_contrast(
          "최종 계약금액은 644억원입니다. 기존 583억원에서 증가했습니다.", "최종 금액은?")))
check("step60 — _RATIO_MIN_SENTS 가 3", _RATIO_MIN_SENTS == 3)

# step64 — 환산이 틀린 단위 괄호를 뗀다. 문장은 지우지 않는다(step60 재발 방지).
from src.agent.loop import strip_bad_unit_paren  # noqa: E402

# 지울 4종 — 지시 명세 그대로
GO64 = [
    ("백만원→억원 10배 오류 (v6 [9] 실측)",
     "영업이익은 30,000백만원(30억원)입니다.", "영업이익은 30,000백만원입니다."),
    ("백만원→억원 100배 오류 (실호출 실측)",
     "매출은 780,000백만원(78억원)이다.", "매출은 780,000백만원이다."),
    ("십억원→억원 오류",
     "자산은 1,000십억원(10억원)이다.", "자산은 1,000십억원이다."),
    ("억원→백만원 오류",
     "금액은 5억원(5,000백만원)이다.", "금액은 5억원이다."),
]
for name, src64, want64 in GO64:
    got64, n64 = strip_bad_unit_paren(src64)
    check(f"step64 지운다 — {name}", got64 == want64 and n64 == 1,
          f"{got64!r} n={n64}")

# 남길 5종 — 지시 명세 그대로
STAY64 = [
    ("환산 정확", "영업이익은 30,000백만원(300억원)입니다."),
    ("숫자 없는 괄호", "매출액(연결 기준)은 300조다."),
    ("단위 없는 괄호", "영업이익(주29)은 100억이다."),
    ("오차 1% 이내", "약 1,005백만원(10억원)이다."),
    ("중첩 괄호", "매출액(연결(주1) 기준)은 300억이다."),
]
for name, src64 in STAY64:
    got64, n64 = strip_bad_unit_paren(src64)
    check(f"step64 남긴다 — {name}", got64 == src64 and n64 == 0,
          f"{got64!r} n={n64}")

# 경계·안전
check("step64 — 같은 단위끼리는 판정하지 않는다",
      strip_bad_unit_paren("644억원(583억원)")[1] == 0)
check("step64 — 여러 개면 틀린 것만 뗀다",
      strip_bad_unit_paren("30,000백만원(300억원)과 5억원(5,000백만원)")
      == ("30,000백만원(300억원)과 5억원", 1))
check("step64 — 문장의 다른 부분은 한 글자도 안 바뀐다",
      strip_bad_unit_paren("앞문장. 30,000백만원(30억원) 뒷문장.")[0]
      == "앞문장. 30,000백만원 뒷문장.")
check("step64 — 천원 환산도 본다",
      strip_bad_unit_paren("1백만원(1,000천원)")[1] == 0
      and strip_bad_unit_paren("1백만원(100천원)")[1] == 1)
for bad64 in (None, "", "괄호(만) 있는 (문장)"):
    try:
        strip_bad_unit_paren(bad64)
        ok64 = True
    except Exception:                          # noqa: BLE001
        ok64 = False
    check(f"step64 — 예외 없음 {bad64!r}", ok64)

# step40 (3-F) — 역질문 블록은 상투구가 아니다. 지우면 채점 항목을 잃는다.
ASKBACK = ("해당 항목은 공시에서 확인되지 않습니다.\n\n"
           "확인 가능한 관련 정보:\n"
           "(a) 2025년 확정 실적(매출·영업이익)으로 정리해 드릴까요?\n"
           "(b) 해당 계약의 공시 이력(체결일·상대방·유보 사유)으로 정리해 드릴까요?\n"
           "필요한 항목을 알려주시면 그 범위로 답변드리겠습니다.")
got = strip_boilerplate(ASKBACK)
check("역질문 '드릴까요?' 를 안 지운다", "정리해 드릴까요?" in got, repr(got[-80:]))
check("역질문 '알려주시면' 을 안 지운다", "알려주시면 그 범위로" in got, repr(got[-60:]))
check("역질문 블록 전체가 그대로", got.strip() == ASKBACK, "")

print()
print("=" * 74)
print("4 — 문장만 지우고 문단을 지우지 않는다")
print("=" * 74)

para = ("첫 문단 첫 문장이다. 관련 기관에 문의하시기 바랍니다. 첫 문단 셋째 문장이다."
        "\n\n둘째 문단이다.")
got = strip_boilerplate(para)
check("걸린 문장만 사라진다", "관련 기관에 문의" not in got, repr(got))
check("같은 문단의 다른 문장은 남는다",
      "첫 문단 첫 문장이다." in got and "첫 문단 셋째 문장이다." in got, repr(got))
check("둘째 문단은 그대로", "둘째 문단이다." in got, repr(got))
check("문단 구분이 살아 있다", "\n\n" in got, repr(got))

check("전부 상투구면 원문을 돌려준다(빈 답 금지)",
      strip_boilerplate("관련 기관에 문의하시기 바랍니다.").strip() != "")
for bad in (None, "", "  "):
    try:
        strip_boilerplate(bad)
        ok = True
    except Exception:                          # noqa: BLE001
        ok = False
    check(f"strip_boilerplate({bad!r}) 예외 없음", ok)

print()
print("=" * 74)
print("5 — 회귀 6번 답변 전체를 그대로 통과시켜 본다")
print("=" * 74)

REAL = ("삼성전자 이사회에서 반대한 이사가 있었는지에 대한 정보는 제공된 공시에서 "
        "확인되지 않습니다. 이사회 발언·표결 내역은 공시 대상이 아니므로 이와 관련된 "
        "내용을 공시에서 찾아볼 수 없습니다. 추가적인 정보가 필요하시면 관련 기관에 "
        "문의하시기 바랍니다.")
srcs = [S_TREA, S_TREA.replace("0019", "0020"), S_TREA.replace("0019", "0021")]
fixed, used3 = attach_sources(strip_boilerplate(REAL), srcs)
check("상투구가 사라졌다", "관련 기관에 문의" not in fixed, repr(fixed[:160]))
check("거절 문장은 남았다", "확인되지 않습니다" in fixed)
# 되돌림 뒤에는 자기주식 문서가 근거에 남는다. **알고 되돌린 부작용이다.**
check("자기주식 문서가 근거에 남는다(되돌림의 대가)", "20250218000019" in fixed,
      repr(fixed.splitlines()[-1]))
check("근거 절이 비지 않는다", "\n근거\n" in fixed)
print()
print("  --- 고친 뒤 답변 ---")
for ln in fixed.splitlines():
    print("   ", ln)

print()
print("=" * 74)
print("6 — 근거를 걸러도 '무근거' 딱지가 잘못 붙지 않는다")
print("=" * 74)
#
# attach_sources 가 이제 '본문이 가리킨 것' 만 남기므로 sources 가 비어 나올 수 있다.
# 무근거 표시는 **도구가 아무것도 못 가져왔는가**를 물어야 한다. 필터링된 목록으로
# 물으면 제대로 조회하고 답한 문항에까지 딱지가 붙는다(실측: e2e 재실행 3번째 문항).
from src.agent import clova, loop as loop_mod            # noqa: E402
from src.agent import usage as usage_mod                 # noqa: E402

usage_mod.set_enabled(False)
real_chat = clova.chat
ANSWER = "제76기 연결 영업이익은 2,357,283,417,485원입니다."


def fake_chat(messages, **kw):
    """도구를 한 번 부르고, 도구가 없으면 수치를 답한다."""
    if kw.get("extra"):
        return {"ok": True, "text": "", "finish_reason": "tool_calls", "usage": {},
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "search_disclosure",
                                             "arguments": {"query": "영업이익"}}}]}
    return {"ok": True, "text": ANSWER, "tool_calls": [], "finish_reason": "stop",
            "usage": {}}


import src.tools as T                                    # noqa: E402

real_tools = dict(T.TOOLS)
T.TOOLS = dict(T.TOOLS)
T.TOOLS["search_disclosure"] = lambda **kw: {
    "status": "ok", "results": [], "sources": [S_SAM], "note": ""}
clova.chat = fake_chat
try:
    r = loop_mod.answer("영업이익은?", deadline=50.0)
finally:
    clova.chat = real_chat
    T.TOOLS = real_tools

check("조회는 했으므로 무근거 딱지가 없다", not r["unsourced_claim"],
      repr(r["answer"][:70]))
check("근거 절에 출처가 붙는다", "20250311001085" in r["answer"], repr(r["answer"][-70:]))
check("본문 수치는 그대로", "2,357,283,417,485" in r["answer"])

print()
print("=" * 74)
if FAIL:
    print(f"{len(FAIL)}건 실패: " + ", ".join(FAIL))
    sys.exit(1)
print("전부 통과 · LLM 실제 호출 0회")
