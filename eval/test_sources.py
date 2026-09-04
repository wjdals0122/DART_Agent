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
