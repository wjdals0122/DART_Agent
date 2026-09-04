"""Agent 대화 루프. 질문 하나를 받아 도구를 부르고 답을 만든다.

  from src.agent.loop import answer
  answer("SK하이닉스 2024년 연결 매출액이 얼마야?")

【이 파일의 계약 — 어떤 경우에도 지킨다】
반환은 언제나 아래 모양이다. LLM 이 죽든, 도구가 죽든, 시간이 다 되든 마찬가지다.

  {"answer": str, "sources": [...], "tool_calls": [...], "elapsed": float}

`answer` 는 비지 않는다. 최악의 경우에도 "공시에서 확인되지 않음" 이 들어간다.
평가 중 예외가 나면 그 문항은 0점이라 여기서 전부 막는다.

【HyperCLOVA X 도구 왕복 — 실측한 것】
  요청  extra={"tools": [...], "toolChoice": "auto"}   · maxTokens >= 1024 필수
  응답  result.message.toolCalls[].{id, type, function:{name, arguments}}
        finishReason == "tool_calls"
  되돌려주기
        {"role":"assistant","content":"","toolCalls":[받은 toolCall 그대로]}
        {"role":"tool","toolCallId":"call_...","name":"함수명","content":"결과 문자열"}

**arguments 는 JSON 문자열이 아니라 객체로 온다.** OpenAI 와 다르다.
json.loads() 를 걸면 터진다. 그래도 문자열로 올 경우를 대비해 isinstance 로 가른다.

【무한루프·폭주 막기】
- 도구 호출 누적 6회. 넘으면 실행하지 않고 "한도 초과" 를 도구 결과로 돌려준다
- 전체 DEADLINE(200초). 넘으면 tools 없이 한 번 더 불러 **강제로 답을 받아낸다**
- 같은 (도구, 인자) 재호출은 캐시를 주고 "이미 조회함" 을 붙인다.
  이게 없으면 못 찾은 질문에서 같은 검색을 끝없이 반복한다
- **비용 상한**. `usage.use_budget()` 으로 예산을 걸어 두면 매 LLM 호출 **직전에**
  확인하고, 넘었으면 그 자리에서 멈춘다(문항 경계까지 기다리지 않는다).
  멈춘 사실은 `note` 와 `stopped_by_budget` 에 남는다

【토큰·낭비 계측】
반환 dict 에 `usage`(문항 단위 합계)와 `waste`(낭비 신호)가 들어 있다.
호출 하나하나는 `usage.record()` 가 `eval/token_log.jsonl` 에 append 한다.
`waste` 는 프롬프트·루프를 고칠 자리를 가리킨다 — 재호출 차단이 잦으면 검색어가
안 바뀌는 것이고, 한도 도달이 잦으면 계획이 헤매는 것이다.
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path

from src.agent import usage as usage_mod

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"
TOOLS_JSON = ROOT / "config" / "tools.json"

MAX_TOOL_CALLS = 6
# 주최측 공지(2026-09-04): "문항 당 응답 대기 시간(타임아웃)은 300초입니다."
# 그보다 짧게 잡아야 답을 만들어 보낼 시간이 남는다 — 타임아웃에 딱 맞추면
# 답변 생성 도중에 상대가 먼저 끊어 '미응답' 이 된다.
# 층위: 주최측 300 > API 하드 240(server.HARD_DEADLINE) > 에이전트 소프트 200(여기).
# (예전 값 50.0 은 "60초 이내 응답" 이라는 옛 명세 기준이었다)
DEADLINE = 200.0
MAX_TOKENS = 2048          # 도구 호출은 1024 이상이어야 한다(문서 명시)
TEMPERATURE = 0.1
FALLBACK = "공시에서 확인되지 않음"

# 도구 결과를 다 넣은 직후 딱 한 번 끼워 넣는 형식 리마인더.
#
# 【왜 필요한가】
# 시스템 프롬프트가 4,000자인데 도구 결과가 그 뒤로 12,000자쯤 쌓인다. 답을 쓸 때쯤이면
# 형식 규칙이 멀어져서, 실측으로 (1) 사칙연산을 calculate 없이 암산하고
# (2) 근거 절을 빠뜨리거나 대괄호를 떼먹었다. 가까이에서 한 번 더 말해 준다.
#
# 【왜 이 자리인가】
# "모델이 답변만 내놓으려는 턴" 은 불러 보기 전엔 알 수 없다. 그걸 알아내려면
# 한 번 부르고 버린 뒤 다시 부르는 셈이라 LLM 호출이 두 배가 된다.
# 도구 결과가 들어온 직후면 다음 턴이 십중팔구 답변 턴이고, 아니어도 손해가 없다
# (모델이 도구를 더 부르면 루프는 그냥 계속 돈다).
REMINDER = "\n".join([
    "위 자료만으로 답하라.",
    "사칙연산이 필요하면 암산하지 말고 calculate 도구를 부른다.",
])

# 도구를 한 번도 안 부르고 답하려 할 때 **먼저** 던지는 지시.
#
# 【왜 프롬프트가 아니라 여기인가】
# system.txt 에 "도구를 부르지 않고 답하지 마라" 가 이미 있는데 지켜지지 않았다.
# e2e 첫 실행에서 두산에너빌리티 문항이 도구 0회로 "공시에서 확인되지 않습니다" 를
# 냈다. 조회를 안 했으니 확인될 리가 없다. 규칙 문장으로 세 번 막아도 새어 나오는
# 것은 코드에서 정한다(이 리포의 반복된 교훈).
#
# 한 번만 붙인다. 그래도 안 부르면 그대로 진행한다 — 무한히 조를 수는 없다.
FORCE_TOOL = (
    "아직 공시를 조회하지 않았다. 답하기 전에 반드시 search_disclosure 또는 "
    "list_disclosures 를 호출해 근거를 확보하라. 조회 결과 근거가 없으면 "
    "그때 '공시에서 확인되지 않음'이라고 답한다."
)

# 목록은 메타데이터라서 금액·사유·진행 상태의 근거가 될 수 없다. 목록만 받은 뒤
# 본문 내용까지 답하려는 경로를 한 번 되돌린다. 순수 건수·비중 질문이라면 모델이
# "목록만으로 충분하다"고 판단해 같은 답을 내도 다음 턴에는 그대로 받는다.
LIST_ONLY_NUDGE = (
    "목록은 문서 내용의 근거가 아니다. 질문이 공시 건수·유형·접수일 같은 "
    "목록 메타데이터만 묻는지 다시 확인하라. 금액·사유·정정 내용·진행 상태를 "
    "하나라도 묻는다면, 접수번호를 하나씩 대입하지 말고 질문의 사건명·계약명·"
    "항목명 같은 고유어로 search_disclosure 를 호출해 본문 검색이 관련 문서를 "
    "고르게 하라. 시점은 date_from/date_to, 종류는 doc_group 으로 좁혀라. "
    "목록 메타데이터만으로 답이 완결되면 도구 없이 답변 본문만 다시 출력하라."
)

# 도구를 한 번도 안 부르고 바로 답하려는 경우에만 한 번 더 던지는 검토 지시.
# 거절 답변에 '공개되지 않을 가능성이 높습니다', '관련 기관에 문의하세요' 같은
# 공시 근거 없는 일반 지식이 붙는 걸 막는다. 시스템 프롬프트에 같은 규칙을 넣어
# 봤지만 지켜지지 않았다(실측 2/2). 답을 본 뒤에 지적해야 먹힌다.
REVIEW = (
    "위 답변을 검토하라. 공시에서 확인되지 않는다고 밝힌 뒤에는 그 이유를 추측해 "
    "설명하지 마라. '공개되지 않을 가능성이 높습니다', '관련 기관에 문의하세요', "
    "'내부 회의록에서 확인할 수 있습니다' 같은 문장은 공시 근거가 없는 일반 지식이므로 "
    "삭제하라. 확인되지 않는 항목은 한 문장으로 밝히고 끝낸다. "
    "질문의 다른 항목 중 공시에서 확인되는 것이 있으면 그것만 이어서 답하라. "
    "고쳐 쓴 답변 **본문만** 출력하라. '죄송합니다', '말씀하신 대로', "
    "'수정된 답변은 다음과 같습니다', '다른 질문이 있으시면' 같은 말머리·맺음말을 "
    "붙이지 마라. 검토했다는 사실 자체를 언급하지 마라."
)

# ---------------------------------------------------------------- 최종 답변 검문
#
# 【왜 코드인가】
# 이 리포의 반복된 교훈이다 — 근거 절을 프롬프트로 세 번 시도해 세 번 실패하고
# 후처리로 해결했다. 아래 둘도 프롬프트(system.txt [계산]·[확인되지 않음])에 같이
# 적어 두었지만, **지켜지는지는 코드가 확인한다.**
#
# 【왜 tool 메시지가 아니라 user 메시지인가 — 지시와 다른 점】
# 지시는 "tool 메시지로 넣으라" 였다. 그렇게 하지 않았다. HCX 의 tool 역할 메시지는
# **바로 앞 assistant 메시지의 toolCalls 에 있는 toolCallId 를 가리켜야** 한다.
# 여기는 모델이 도구를 부르지 **않은** 턴이라 가리킬 id 가 없다. 없는 id 로 tool
# 메시지를 만들면 HTTP 400 이 나고, 평가 중 400 은 그 문항을 통째로 잃는다.
# 그래서 이미 검증된 세 개(FORCE_TOOL·REVIEW·LIST_ONLY_NUDGE)와 **같은 방식**으로
# assistant(답변) + user(지시) 를 넣는다. 모델에게 보이는 내용은 지시 그대로다.
#
# 검문은 **문항당 최대 한 번**이다. 추가 턴에서도 같은 상태면 그대로 내보낸다.
AUDIT_MARGIN = 20.0         # 남은 시간이 이보다 적으면 검문을 건너뛴다
                            # (데드라인 200초 기준. 검문 한 턴 = LLM 1회 ≈ 5~15초)

AUDIT_CALC = (
    "답변에 비율·배수 표현이 있으나 calculate 를 사용하지 않았다. "
    "calculate 로 값을 구한 뒤 답변을 다시 작성하라."
)
AUDIT_NO_SEARCH = (
    "아직 어떤 공시도 조회하지 않았다. search_disclosure 또는 "
    "list_disclosures 로 먼저 확인한 뒤 답하라."
)
# 검문 4 — 부른 도구가 **전부** 빗나갔는데 그대로 거절하려 할 때.
#
# 【왜 필요한가 — 2026-09-04 실측】
# 회귀 7번이 `search_disclosure(fiscal_year=2024, rcept_dt="20240130")` 로 0건을 받고
# **두 번 다** 재시도 없이 "찾을 수 없습니다" 로 끝냈다. 도구 결과 note 에
# "이 조합은 항상 0건이 된다 · 둘 중 하나를 빼고 다시 조회하라" 가 실려 있었는데도.
#
# 루프에 이 되돌림이 없었다 — FORCE_TOOL 은 **도구 0회**일 때만, LIST_ONLY_NUDGE 는
# **list 가 ok** 일 때만 발동한다. 그래서 not_found 한 번이면 문항이 끝났다.
# 7번 하나가 아니라 **검색이 한 번 빗나간 모든 문항**에 걸린다.
AUDIT_ALL_BAD = (
    "조회한 모든 도구가 결과를 돌려주지 않았다. 마지막 도구 결과의 note 를 다시 읽고, "
    "필터를 완화해 한 번 더 조회하라. 특히 rcept_dt·fiscal_year·doc_group 중 "
    "불필요한 것을 빼라. 그래도 없으면 그때 확인되지 않음으로 답하라."
)
# 검문 4 의 거절 표현. 처음 셋은 최초 지시가 지목한 것.
# 뒤의 넷은 step33(2026-09-04) 추가 — 두산퓨얼셀 실측에서 전부 빗나간 도구 뒤
# "정보가 없어 답변할 수 없습니다" 로 끝났는데 어느 표현에도 안 걸렸다.
_ALL_BAD_REFUSE = ("확인되지 않", "찾을 수 없", "확인할 수 없",
                   "답변할 수 없", "답변드릴 수 없", "제공할 수 없", "알 수 없")

# 검문 5 — 도구를 부르겠다고 **말만 하고** 답변을 끝낼 때.
#
# 【왜 필요한가 — 2026-09-04 실측】
# 회귀 3번(KB금융 자기자본)이 최근 5회 중 **2회** 이렇게 실패했다.
#   list_disclosures -> ok · search_disclosure 를 부르지 않음
#   답변: "…이를 위해 search_disclosure 도구를 사용하여 해당 문서의 본문을 검색하겠습니다."
# `LIST_ONLY_NUDGE` 를 받고 그 지시를 **답변으로 되읊는다.** 잡음이 아니라 재현되는 유형이다.
AUDIT_ANNOUNCED = (
    "도구를 사용하겠다고 했으나 실제로 호출하지 않았다. 지금 그 도구를 호출하라. "
    "설명하지 말고 호출만 하라."
)
_TOOL_NAMES = ("search_disclosure", "list_disclosures", "calculate", "resolve_company")
_ANNOUNCE = ("하겠습니다", "해야 합니다", "필요합니다", "검색하여", "조회하여")
# 검문 5 확장(step33 · 2026-09-04) — 도구 호출을 **텍스트로** 출력하고 끝낸 실측:
#   [{"name": "search_disclosure", "arguments": {"query": ...}}]
# 예고 표현이 없어 위 조건이 못 잡았다. 도구 이름 뒤 이 창 안에 '(' 나 '{' 가
# 오면 호출 시늉으로 본다(JSON 은 이름과 '{' 사이에 `", "arguments": ` 가 끼어
# 바로 뒤 문자만 봐서는 못 잡는다). 실제로 부른 도구 언급은 called 검사가 걸러 준다.
_CALL_WINDOW = 20

# 검문 6 — 여러 값의 크기를 비교하면서 calculate 를 안 썼을 때.
#
# 【왜 필요한가 — 2026-09-04 실측】
# "2024년 영업이익이 가장 높은 회사는?" 에서 자기가 나열한 값 중에서도
# 기아(12조 6,671억)가 13배 높은데 삼성E&A(9,716억)를 골랐다.
# 억원과 백만원이 섞인 값을 눈으로 견준 것이다. 검문 1(needs_calc)은
# %·배 표기만 잡아서 이 답변을 통과시켰다.
AUDIT_COMPARE = (
    "여러 값의 크기를 비교했으나 calculate 를 사용하지 않았다. "
    "단위가 다른 값이 섞여 있을 수 있다. calculate 로 같은 단위로 환산해 "
    "비교한 뒤 답변을 다시 작성하라. 그리고 조회한 범위를 답변에 밝혀라."
)
# "가장 높/크/많", "최대", "1위" — 지시가 지목한 표현 그대로.
# '최대' 는 '최대주주' 라는 공시 상용어에 오탐되므로 그 낱말만 먼저 걷어낸다.
_SUPERLATIVE = ("가장 높", "가장 크", "가장 많", "최대", "1위")

# '4.55배' '234.73%' '3.06 퍼센트' 처럼 **숫자에 붙은** 비율·배수 표기만 잡는다.
# 그냥 '배' 를 찾으면 배당·배출·배터리·분배가 전부 걸린다.
_RATIO = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|퍼센트|배)")
_DELTA = ("증가", "감소")
_ONLY_NUM = re.compile(r"[^\d,.]")
# 검문 2 의 거절 표현. _REFUSAL 보다 좁게 — 지시가 이 둘을 지목했다.
_AUDIT_REFUSE = ("확인되지 않", "확인할 수 없")


def needs_calc(text: str, tool_calls: list, documents: list) -> bool:
    """비율·배수를 말하면서 calculate 를 안 불렀나. 예외를 던지지 않는다.

    【왜 '문서에 없는 숫자' 만 보는가 — 지시를 좁힌 부분】
    지시는 "배·%·증가·감소가 있으면" 이었다. 그대로 하면 공시에서 **그대로 베낀**
    비율(예: '매출액대비 3.06%')에도 걸려 거의 모든 답변이 검문에 걸린다.
    한 문항에 LLM 호출이 하나씩 더 붙고 시간도 는다.

    검문의 이름이 '계산 미검증' 이다. 계산을 했는지 여부는 **그 숫자가 검색된
    문서에 그대로 있는가**로 갈린다. 문서에 있으면 인용이고, 없으면 만들어 낸 값이다.
    실제 사례가 정확히 그랬다 — '234.73%' 는 어느 문서에도 없었다(차액을 비율로 썼다).
    """
    try:
        if any((c or {}).get("name") == "calculate" for c in (tool_calls or [])):
            return False
        body = "\n".join(str((d or {}).get("text") or "") for d in (documents or []))
        flat = body.replace(",", "")
        for sent in _SENT.findall(text or ""):
            toks = _RATIO.findall(sent)
            if not toks and any(w in sent for w in _DELTA):
                toks = [m.group(0) for m in _NUM3.finditer(sent)]
            for t in toks:
                num = _ONLY_NUM.sub("", t).strip(".,")
                if not num:
                    continue
                if num in body or num.replace(",", "") in flat:
                    continue                   # 문서에서 베낀 값 — 계산이 아니다
                return True
        return False
    except Exception:                          # noqa: BLE001 — 검문이 답을 막으면 안 된다
        traceback.print_exc(file=sys.stderr)
        return False


def refused_without_search(text: str, tool_calls: list) -> bool:
    """아무것도 조회하지 않고 '확인되지 않는다' 고 했나."""
    try:
        if tool_calls:
            return False
        return any(w in (text or "") for w in _AUDIT_REFUSE)
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return False


def all_tools_failed(text: str, tool_calls: list) -> bool:
    """도구를 불렀는데 **하나도** ok 가 아니고, 그대로 거절하려 하나.

    `refused_without_search` 와 조건이 **배타적**이다 — 저쪽은 도구 0회, 이쪽은 1회 이상.
    `LIST_ONLY_NUDGE` 와도 겹치지 않는다 — 그쪽은 list 가 ok 여야 발동한다.
    """
    try:
        calls = tool_calls or []
        if not calls:
            return False
        if any((c or {}).get("status") == "ok" for c in calls):
            return False
        return any(w in (text or "") for w in _ALL_BAD_REFUSE)
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return False


def compares_without_calc(text: str, tool_calls: list) -> bool:
    """'가장 높다'류 표현 + 숫자 2개 이상인데 calculate 를 안 불렀나.

    【'숫자' 를 어떻게 세는가 — 지시("숫자가 2개 이상")를 좁힌 부분】
    아무 숫자나 세면 연도가 낀다 — "2024년 영업이익이 가장 높은…" 을 되풀이하는
    답변은 값이 하나뿐이어도 (2024 + 값) 으로 걸린다. 무근거 표시(`_YEAR`)와 같은
    기준으로 연도(1900~2100)를 빼고, 3자리 이상 숫자(`_NUM3`)를 **서로 다른 것만**
    센다. 비교란 서로 다른 값이 둘 이상 있다는 뜻이다.
    """
    try:
        if any((c or {}).get("name") == "calculate" for c in (tool_calls or [])):
            return False
        body = (text or "").replace("최대주주", "")   # 상용어 오탐 방지
        if not any(w in body for w in _SUPERLATIVE):
            return False
        nums = {m.group(0).replace(",", "") for m in _NUM3.finditer(body)}
        nums = {n for n in nums if not _YEAR.match(n)}
        return len(nums) >= 2
    except Exception:                          # noqa: BLE001 — 검문이 답을 막으면 안 된다
        traceback.print_exc(file=sys.stderr)
        return False


def announced_but_not_called(text: str, tool_calls: list) -> bool:
    """도구를 부르겠다고 해 놓고 안 불렀나.

    【오탐을 어떻게 막는가 — 세 조건을 다 만족해야 한다】
      (1) 도구 이름이 나온다
      (2) 예고 표현이 **같은 문장 안에** 있다
      (3) 그 도구를 이번 문항에서 **부르지 않았다**

    (2) 를 문장 단위로 본 이유 — "search_disclosure 로 조회한 결과 …입니다.
    추가 확인이 필요합니다." 처럼 멀쩡한 답변에서 두 조각이 다른 문장에 흩어져
    있을 수 있다. (3) 이 가장 강한 방어다. 실제로 부른 도구를 언급하는 것은
    정상 답변이므로 절대 걸리지 않는다.

    【확장(step33) — 예고 표현 없이 호출 시늉만 있는 경우】
    안 부른 도구 이름 뒤 `_CALL_WINDOW`자 안에 '(' 나 '{' 가 오면 발동한다.
    도구 호출 JSON 을 답변 본문에 텍스트로 출력하고 끝낸 실측을 잡는다.
    """
    try:
        called = {(c or {}).get("name") for c in (tool_calls or [])}
        body = text or ""
        for sent in _SENT.findall(body):
            if not any(w in sent for w in _ANNOUNCE):
                continue
            for name in _TOOL_NAMES:
                if name in sent and name not in called:
                    return True
        # 호출 시늉 — 안 부른 도구 이름 + 근처의 '(' 또는 '{'
        for name in _TOOL_NAMES:
            if name in called:
                continue
            pos = body.find(name)
            while pos != -1:
                tail = body[pos + len(name): pos + len(name) + _CALL_WINDOW]
                if "(" in tail or "{" in tail:
                    return True
                pos = body.find(name, pos + 1)
        return False
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return False


# 답변 끝의 '근거' 목록을 찾아내는 규칙. 줄 머리가 '근거' 이고 그 아래가
# '-' 나 '*' 로 시작하는 목록인 구간만 잡는다. 산문형 '근거: …' 는 건드리지 않는다.
_GEUN = re.compile(r"\n[ \t]*(?:\*\*)?근거(?:\*\*)?[ \t]*:?[ \t]*\n"
                   r"(?:[ \t]*[-*][^\n]*\n?)+\s*$")
_RCEPT = re.compile(r"\d{14}")

# 근거가 하나도 없는데 사실을 단정하는 답변에 붙일 표시.
#
# 【왜 지우지 않는가】
# 통째로 지우면 맞는 답도 잃는다. 표시만 하면 평가자가 판단할 수 있고,
# 우리도 이런 사례가 몇 건인지 셀 수 있다(`unsourced_claim`).
#
# 【무엇을 단정으로 보는가 — 좁게 잡았다】
# (1) 3자리 이상 숫자. 단, 연도(1900~2100)는 뺀다 — 질문을 되풀이하는 자리에 흔하다
# (2) "없었습니다" "접수되지 않았습니다" 같은 사실 단정 어미
# **거절 문장은 단정이 아니다.** "공시에서 확인되지 않습니다" 가 걸리면 정상 거절까지
# 오염 표시를 받는다. 그래서 거절 표현이 든 문장은 먼저 걷어내고 나머지만 본다.
UNSOURCED_MARK = "※ 아래 내용은 공시 조회로 확인되지 않았습니다."
_SENT = re.compile(r"[^.!?\n]+[.!?\n]?")
_NUM3 = re.compile(r"\d[\d,]{2,}(?:\.\d+)?")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_REFUSAL = ("확인되지 않", "확인할 수 없", "찾을 수 없", "확인이 필요",
            "제공할 수 없", "알 수 없", "확인해 드릴 수 없")
_ASSERT = ("없었습니다", "없었다", "접수되지 않았", "제출되지 않았",
           "발생하지 않았", "변경되지 않았", "수정되지 않았", "이루어지지 않았")


def has_unsourced_claim(text: str, sources: list) -> bool:
    """근거가 0건인데 숫자나 사실 단정이 있으면 True."""
    if sources:
        return False
    for sent in _SENT.findall(text or ""):
        if any(w in sent for w in _REFUSAL):
            continue                       # 거절 문장은 단정이 아니다
        for m in _NUM3.finditer(sent):
            if not _YEAR.match(m.group(0).replace(",", "")):
                return True
        if any(w in sent for w in _ASSERT):
            return True
    return False


def mark_unsourced(text: str) -> str:
    """맨 앞에 한 줄만 붙인다. 본문은 건드리지 않는다."""
    body = (text or "").strip()
    if body.startswith(UNSOURCED_MARK):
        return body
    return f"{UNSOURCED_MARK}\n\n{body}"



def _read(name: str) -> str:
    p = PROMPTS / name
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def build_system() -> str:
    """planner(계획 규칙) 를 앞에, system(답변 규칙) 을 뒤에."""
    planner, system = _read("planner.txt"), _read("system.txt")
    if not system:
        system = _read("system_min.txt")
    head = "당신은 DART 전자공시를 근거로 답하는 분석 어시스턴트다."
    body = system[len(head):].lstrip() if system.startswith(head) else system
    return f"{head}\n\n{planner}\n\n{body}".strip()


def load_tools() -> list:
    try:
        return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))["tools"]
    except (OSError, ValueError, KeyError) as e:
        print(f"[loop] {TOOLS_JSON} 를 읽지 못했다 — 도구 없이 진행한다 "
              f"({type(e).__name__}: {e})", file=sys.stderr)
        return []


def warmup() -> float:
    """검색 인덱스를 미리 올린다. 걸린 초를 돌려준다.

    첫 검색은 인덱스·임베딩 모델을 올리느라 35초쯤 걸린다. 그건 답변 시간이 아니라
    기동 시간이다. 데드라인 안에 그게 들어가면 첫 질문만 억울하게 잘린다.
    부르는 쪽에서 answer() 전에 한 번 불러 준다.
    """
    t = time.time()
    try:
        from src.search.hybrid import get_searcher
        get_searcher()
    except Exception as e:                     # noqa: BLE001 — 예열 실패해도 답은 해야 한다
        print(f"[loop] 예열 실패({type(e).__name__}: {e}). 첫 검색이 느릴 것이다",
              file=sys.stderr)
    return round(time.time() - t, 2)


# ---------------------------------------------------------------- 도구 결과 -> 문자열


def _args_of(call: dict) -> tuple:
    """(인자 dict, 오류메시지). arguments 는 보통 객체로 오지만 문자열도 대비한다."""
    fn = (call or {}).get("function") or {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except ValueError as e:
            return {}, f"인자 파싱 실패: {e}"
        return (v, None) if isinstance(v, dict) else ({}, f"인자가 객체가 아니다: {type(v).__name__}")
    return {}, f"인자 형식을 모르겠다: {type(raw).__name__}"


def _render(name: str, out: dict) -> str:
    """도구 결과를 LLM 이 읽을 문자열로. 검색 결과는 build_context 로 조립한다."""
    head = f'status={out.get("status")}'
    note = out.get("note") or ""
    if note:
        head += f" · {note}"

    if name == "search_disclosure" and out.get("results"):
        try:
            from src.agent.context import build_context
            body = build_context(out["results"])
        except Exception as e:                 # noqa: BLE001 — 조립이 실패해도 답은 해야 한다
            traceback.print_exc(file=sys.stderr)
            body = f"(컨텍스트 조립 실패: {type(e).__name__}: {e})"
        return f"{head}\n\n{body}"

    slim = {k: v for k, v in out.items() if k not in ("results", "note", "status", "pool")}
    if out.get("results"):
        slim["results"] = out["results"][:30]
    return f"{head}\n{json.dumps(slim, ensure_ascii=False)[:6000]}"


def _summary(name: str, out: dict) -> str:
    """사람이 한 줄로 볼 요약."""
    if name == "calculate":
        return f"value={out.get('value')}"
    if name == "list_disclosures":
        return f"total={out.get('total')} · {out.get('by_correction')}"
    n = len(out.get("results") or [])
    extra = f" · 후보 {out['n_candidates']:,}" if out.get("n_candidates") else ""
    return f"{n}건{extra}"


# 산문형 근거 줄 — "근거: [○○ 사업보고서 …, 접수번호 …]" 한 줄짜리.
#
# 【왜 예전엔 안 뗐고, 왜 이제 떼는가】
# `_GEUN` 은 목록형(`근거\n- …`)만 잡는다. 산문형을 남긴 이유는 코드에 이렇게 남아 있다 —
# `drop_baseless_geun` 이 "근거: 상법 제391조에 따라…" 처럼 **라벨만 근거이고 내용은
# 일반 지식**인 문단을 따로 처리하기 때문이다. 그 처리는 **출처가 0건일 때만** 돈다.
# 그래서 출처가 있는 답변에서는 산문형 줄이 그대로 남아 근거가 두 번 나왔다
# (e2e_v4 29·32·33번).
#
# 두 규칙은 겹치지 않는다. `drop_baseless_geun` 이 잡는 것은 **출처 라벨이 없는** 문단이고,
# 아래는 **출처 라벨뿐인** 줄이다. 그래서 조건을 좁혀 아래 셋을 다 만족할 때만 뗀다.
#   (1) 답변의 **마지막 줄**이고
#   (2) `근거:` 또는 `출처:` 로 시작하고
#   (3) 그 줄에 대괄호 출처 라벨(또는 접수번호)만 있고 다른 서술이 없다
# 본문 중간의 "근거: …" 는 문장의 일부일 수 있으므로 건드리지 않는다.
_GEUN_PROSE = re.compile(r"^[ \t]*(?:\*\*)?(?:근거|출처)(?:\*\*)?[ \t]*[:：][ \t]*(.*)$")
_SRC_LABEL = re.compile(r"\[[^\]]*\]")


def _only_sources(rest: str) -> bool:
    """'근거:' 뒤에 출처 라벨(또는 접수번호)만 있고 서술이 없나."""
    left = _SRC_LABEL.sub("", rest or "")
    left = _RCEPT.sub("", left)
    left = re.sub(r"[\s,、·และ&and및와과()]+", "", left, flags=re.I)
    return rest.strip() != "" and left == ""


def strip_prose_geun(text: str) -> str:
    """마지막 줄이 '출처 라벨뿐인 근거: 줄' 이면 뗀다. 아니면 그대로."""
    try:
        lines = (text or "").rstrip().split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return text or ""
        m = _GEUN_PROSE.match(lines[-1])
        if m and _only_sources(m.group(1)):
            return "\n".join(lines[:-1]).rstrip()
        return "\n".join(lines).rstrip()
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return text or ""


def strip_sources_section(text: str) -> str:
    """모델이 쓴 '근거' 목록을 떼어낸다. 형식이 흔들리므로 우리가 다시 붙인다."""
    prev = None
    out = text or ""
    while prev != out:                     # 여러 번 붙였을 수도 있다
        prev = out
        out = _GEUN.sub("\n", out).rstrip()
        out = strip_prose_geun(out)        # 목록형을 뗀 뒤 산문형이 드러나기도 한다
    return out


_WS_ANY = re.compile(r"\s+")


def _flat(s: str) -> str:
    return _WS_ANY.sub("", s or "")


def used_sources(text: str, sources: list) -> list:
    """답변 본문이 **실제로 가리킨** 출처만 고른다. 아니면 빈 리스트.

    【2026-09-03 변경 — 예전에는 하나도 안 맞으면 전부 붙였다】
    옛 규칙은 `return used or list(sources)` 였다. "근거가 없는 것보다 많은 게 낫다"
    였는데, 회귀 6번에서 대가를 치렀다 — '삼성전자 이사회에서 반대한 이사가 있었나'
    라는 질문에 **자기주식취득결정 5건**이 근거로 달렸다. 답변은 "확인되지 않습니다"
    였고 본문은 그 문서들을 한 번도 가리키지 않았다. 검색이 엉뚱한 것을 물어 왔고,
    후처리가 그걸 그대로 근거로 승격시킨 것이다.

    【무엇을 '가리켰다' 로 보는가 — 세 가지】
    접수번호만 보면 너무 좁다. 모델은 접수번호 없이 문서 이름만 적기도 한다.
      1. 출처의 접수번호 14자리가 본문에 있다
      2. 출처 라벨 문자열이 통째로 본문에 있다 (공백 무시)
      3. 회사명과 보고서명이 **둘 다** 본문에 있다 (공백 무시)
    셋 중 하나면 인용으로 본다. 하나도 아니면 그 출처는 답변과 무관하다.
    """
    body = text or ""
    flat = _flat(body)
    nums = set(_RCEPT.findall(body))
    out = []
    for s in sources:
        m = _RCEPT.search(s or "")
        if m and m.group(0) in nums:
            out.append(s)
            continue
        if _flat(s) and _flat(s) in flat:
            out.append(s)
            continue
        corp, rep = _corp_report(s)
        if corp and rep and _flat(corp) in flat and _flat(rep) in flat:
            out.append(s)
    # 【되돌림 — 2026-09-04】 하나도 못 찾으면 전부 붙인다.
    # step11 에서 이 줄을 없앴더니 e2e 40문항 중 **21문항**이 근거 절 없이 나갔다.
    # 그 21문항은 전부 search_disclosure 가 ok 였고 5~16건을 물어 왔다 —
    # 근거가 없는 게 아니라 답변 본문이 그 문서를 안 가리켰을 뿐이다.
    # 프롬프트 예시로 본문 인용을 끌어내 봤지만 회귀 7문항에서 3→4로 사실상 그대로였다.
    # 모델이 안 붙이는 것을 프롬프트로는 못 고친다. 근거 표기가 채점 항목이므로
    # **무관한 문서가 섞이는 손해보다 절반이 비는 손해가 크다.**
    # 무관한 문서가 섞이는 사례는 회귀 6번(자기주식 5건)에서 실제로 봤고, 알고 되돌린다.
    return out or list(sources)


def _corp_report(source: str) -> tuple:
    """'[삼성전자 사업보고서 (2024.12), 2025-03-11, 접수번호 …]' -> ('삼성전자', '사업보고서')."""
    head = (source or "").strip().lstrip("[").split(",")[0].strip()
    corp, _, rest = head.partition(" ")
    rep = rest.split("(")[0].strip()
    return corp, rep


# 조회 범위 줄에 쓸 공시 종류. report_nm 에서 바로 읽는다 — 지어내지 않는다.
_KIND = (("사업보고서", "정기공시"), ("반기보고서", "정기공시"), ("분기보고서", "정기공시"),
         ("주요사항보고서", "주요사항보고서"), ("대량보유", "지분공시"))


def scope_line(sources: list, body: str) -> str:
    """근거 절이 비었을 때 대신 적는 '무엇을 조회했는가' 한 줄.

    비워 두면 "찾아보지도 않았다" 와 구분되지 않는다. 조회는 했으나 답변이 그 문서를
    쓰지 않았다는 사실 자체가 평가자에게 필요한 정보다.
    """
    corps, kinds = [], set()
    for s in sources:
        corp, rep = _corp_report(s)
        if corp and corp not in corps:
            corps.append(corp)
        kinds.add(next((v for k, v in _KIND if k in rep), "공시"))
    kind = kinds.pop() if len(kinds) == 1 else "공시"
    who = "·".join(corps) if corps else "코퍼스"
    tail = ("해당 기재 없음" if any(w in (body or "") for w in _REFUSAL)
            else "답변에 인용된 문서 없음")
    return f"조회: {who} {kind} {len(sources)}건 — {tail}"


# ---------------------------------------------------------------- 상투구 제거
#
# 【REVIEW 는 '필터' 가 아니었다 — 2026-09-03 확인】
# REVIEW 는 코드가 지우는 규칙이 아니라 **모델에게 보내는 지시문**이다.
# 완전일치도 부분일치도 정규식도 아니고, 그냥 "삭제하라" 는 문장이었다.
# 그래서 회귀 6번에서 "관련 기관에 문의하시기 바랍니다" 가 그대로 남았다.
# 프롬프트로 세 번 막아 세 번 실패한 전례가 이 리포에 이미 있다. 코드로 지운다.
#
# 【부분일치로 잡는다. 문장 단위로만 지운다】
# 어미가 계속 바뀐다("문의하세요" · "문의하시기 바랍니다" · "문의해 보시기 바랍니다").
# 어간만 잡고, 걸린 **문장 하나**만 버린다. 문단을 통째로 지우지 않는다.
BOILERPLATE = (
    "관련 기관에 문의", "전문가와 상담", "전문가에게 상담", "확인하시기 바랍니다",
    "참고하시기 바랍니다", "문의하시기 바랍니다", "문의하여 주시기",
    "공개되지 않을 가능성", "내부 회의록에서 확인", "추가적인 정보가 필요하시면",
    "도움이 될 것", "문의해 주시기",
    # step33(2026-09-04) 추가 — "공식 홈페이지나 언론 보도 등을 참고하시는 것을
    # 추천드립니다" 가 실측으로 남았다. 지시의 "참고하시기 바랍니다" 는 위에 이미 있다.
    "참고하시는 것을 추천", "문의하시는 것을 권장", "확인해 보시기 바랍니다",
)


# ---------------------------------------------------------------- 서두 군더더기
#
# e2e_v4 40문항 중 **15건**이 "네, 공시 내용을 시간 순서에 따라 정리하겠습니다" 류로
# 시작했다. 채점자가 읽는 첫 문장인데 내용이 없다.
#
# 아래 낱말 목록은 그 조사에서 쓴 것을 **그대로** 가져왔다. 새로 만들지 않았다.
LEADIN_WORDS = ("네,", "네.", "예,", "위 자료를 바탕으로", "위 자료에 따르면", "제공된 자료",
                "답변드리겠습니다", "다음과 같습니다", "정리하겠습니다", "정리하였습니다",
                "아래와 같이", "요청하신", "조회한 결과", "알겠습니다", "확인하였습니다",
                "답변하겠습니다", "정리해", "말씀하신")

# 【무엇을 '사실이 없는 줄' 로 보는가 — 낱말만으로는 못 가른다】
# 위 낱말이 들어 있어도 "네, 반대한 이사가 있었습니다" 는 **답을 담은 문장**이다.
# 지우면 안 된다. 가르는 기준은 **문장이 무엇을 하는가**다.
#   - 뒤에 올 내용을 **예고**하는 문장   -> 지운다
#   - 값·판정을 **진술**하는 문장        -> 남긴다
# 예고문은 끝맺음이 정해져 있다. 아래 어미로 끝나거나 콜론으로 끝난다.
# ("…정리하겠습니다" "…다음과 같습니다:" "…표로 정리한 것입니다")
# 진술문은 그렇지 않다("…있었습니다" "…66,192,960백만원입니다").
# 두 조건(낱말 + 예고 어미)을 **모두** 만족하는 첫 줄만 지운다.
LEADIN_TAIL = ("정리하겠습니다", "정리했습니다", "정리하였습니다", "정리한 것입니다",
               "다음과 같습니다", "답변하겠습니다", "답변드리겠습니다", "요약했습니다",
               "요약하였습니다", "분류하였습니다", "분류했습니다", "설명하겠습니다",
               "알려드리겠습니다", "말씀드리겠습니다")


def strip_leadin(text: str) -> str:
    """답변 **첫 줄**이 내용 없는 예고문이면 지운다. 본문 중간은 건드리지 않는다."""
    try:
        body = (text or "").strip()
        if not body:
            return text or ""
        lines = body.split("\n")
        first = lines[0].strip()
        head = first[:60]
        if not any(w in head for w in LEADIN_WORDS):
            return body
        # 끝의 마침표·콜론을 떼고 어미를 본다 ("…정리하겠습니다." 도 예고문이다)
        naked = first.rstrip().rstrip(".。:：!？? ").rstrip()
        if not (first.rstrip().endswith((":", "：")) or naked.endswith(LEADIN_TAIL)):
            return body                        # 값을 진술하는 문장이다 — 남긴다
        rest = "\n".join(lines[1:]).strip()
        return rest or body                    # 지워서 비면 원문 그대로
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return text or ""


def strip_boilerplate(text: str) -> str:
    """공시 근거가 없는 상투적 안내 문장을 지운다. 문단 구조는 유지한다.

    지워야 하는 것 — "추가 정보는 관련 기관에 문의하시기 바랍니다",
    "전문가와 상담하십시오", "원문을 확인하시기 바랍니다", "참고하시기 바랍니다".
    남겨야 하는 것 — 수치·사실을 말하는 문장, "공시에서 확인되지 않습니다" 같은 거절.
    """
    try:
        out_paras = []
        for para in re.split(r"(\n\s*\n)", text or ""):
            if para.strip() == "" or para.startswith("\n"):
                out_paras.append(para)          # 문단 구분자는 그대로
                continue
            # **줄 단위로 돈다.** 문단을 통째로 _SENT 에 넣으면 줄바꿈이 사라진다 —
            # 번호 목록("1. …\n2. …")이 한 줄로 뭉개진다. 실제로 그랬다.
            out_lines = []
            for line in para.split("\n"):
                kept = [s for s in _SENT.findall(line)
                        if not any(w in s for w in BOILERPLATE)]
                out_lines.append("".join(kept).rstrip())
            out_paras.append("\n".join(out_lines).strip("\n").strip())
        joined = "".join(out_paras)
        # 문장을 다 지워 빈 답이 되면 원문을 돌려준다 — 지우려다 답을 잃지 않는다
        return strip_leadin(joined.strip() or (text or "").strip())
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return text or ""


def drop_baseless_geun(text: str) -> str:
    """출처가 하나도 없는데 '근거:' 라고 주장하는 문단을 잘라낸다.

    거절 답변에서 모델이 '근거: 상법 제391조에 따라…', '근거: 기업 내부 의사결정
    과정의 보안을 위해…' 처럼 **라벨만 근거이고 내용은 일반 지식**인 문단을 붙인다.
    프롬프트로 세 번 막아 봤지만 표현만 바꿔 가며 계속 나왔다(실측).
    도구가 돌려준 출처가 하나도 없으면 '근거' 라고 부를 것 자체가 없으므로 여기서 끊는다.
    """
    paras = re.split(r"\n\s*\n", text or "")
    kept = [p for p in paras
            if not re.match(r"^\s*(?:\*\*)?근거(?:\*\*)?\s*[:：]", p)]
    return "\n\n".join(kept).strip() or (text or "").strip()


def attach_sources(text: str, sources: list) -> tuple:
    """근거 절을 **파이썬이** 만들어 붙인다. (본문, 붙인 출처 목록).

    LLM 에게 맡겼더니 출처가 둘 이상일 때 대괄호를 떼먹었다(실측 2/2).
    형식이 흔들리면 채점에서 샌다. 문자열은 fmt_source() 가 만든 것을 그대로 쓴다.
    """
    body = strip_sources_section(text)
    if not sources:
        # 붙일 출처가 없다 = 도구가 아무것도 못 가져왔다. 그런 답에 '근거' 는 없다
        return drop_baseless_geun(body), []
    # 【무엇에 대고 인용을 찾는가 — 여기서 한 번 틀릴 뻔했다】
    # `body`(모델이 쓴 근거 절을 떼어낸 것)에 대고 찾으면 안 된다.
    # 모델은 출처를 본문 중간이 아니라 **자기가 쓴 근거 절에** 적는 일이 많다.
    # 그걸 먼저 떼고 찾으면 "안 가리켰다" 로 잘못 판정해, 맞는 답에서도 근거가 사라진다.
    # (실측: 최근 회귀 7문항 중 4건이 그렇게 오판됐다.)
    # 그래서 **모델이 준 원문 그대로**(text)에 대고 찾는다. 우리가 붙인 목록은
    # 아직 없으므로 순환하지 않는다.
    picked = used_sources(text, sources)
    if not picked:
        # `used_sources` 가 하나도 못 찾으면 전부 돌려주므로(2026-09-04 되돌림)
        # `sources` 가 비지 않은 한 여기로 오지 않는다. 그래도 방어로 남긴다 —
        # 비우지 말고 **무엇을 조회했는지**는 적는다.
        return f"{drop_baseless_geun(body)}\n\n근거\n- {scope_line(sources, body)}", []
    out = []
    for s in picked:                       # 순서를 지키며 중복 제거
        if s not in out:
            out.append(s)
    lines = "\n".join(f"- {s}" for s in out)
    return f"{body}\n\n근거\n{lines}", out


# ---------------------------------------------------------------- 본체


def answer(question, model=None, max_tool_calls=MAX_TOOL_CALLS,
           deadline=DEADLINE, verbose=False, qid=None) -> dict:
    """질문 하나에 답한다. 어떤 경우에도 규격 dict 를 돌려준다.

    `qid` 는 토큰 로그에 남길 문항 식별자다. 없으면 로그의 qid 가 null 이 되고
    문항 단위 집계(평균 토큰 추정)에서 빠진다.
    """
    t0 = time.time()
    out = {"answer": "", "sources": [], "tool_calls": [], "elapsed": 0.0,
           "llm_seconds": 0.0, "tool_seconds": 0.0, "messages": [], "note": "",
           "usage": {"prompt": 0, "completion": 0, "total": 0,
                     "llm_calls": 0, "unknown_calls": 0, "est_won": 0.0},
           # est_won 은 입력·출력 단가가 다르므로 총 토큰이 아니라 내역으로 계산한다
           "waste": {"repeat_blocked": 0, "retry_after_bad": 0,
                     "hit_call_limit": False, "http_retry": 0, "retry_429": 0,
                     # HTTP 400(40009 산발 오류) 재시도 횟수와, 도구를 떼고
                     # 다시 보냈는지. think_trace 에 그대로 적는다
                     "retry_400": 0, "tools_dropped": False,
                     "retry_wait_s": 0.0, "resent_prompt_tokens_est": 0,
                     "no_tool_nudge": False, "list_only_nudge": False,
                     "answered_without_tools": False,
                     # 최종 답변 검문이 걸렸으면 그 종류 —
                     # calc·compare·no_search·all_bad·announced 중 하나
                     "audit": ""},
           "stopped_by_budget": False, "unsourced_claim": False,
           # 검색이 올린 청크 원문. 평가 서버가 retrieved_context 로 그대로 내보낸다.
           # sources 는 출처 문자열뿐이라 본문이 없다.
           "documents": []}
    try:
        out = _run(question, out, t0, model, max_tool_calls, deadline, verbose, qid)
    except Exception as e:                     # noqa: BLE001 — 마지막 방어선
        traceback.print_exc(file=sys.stderr)
        out["note"] = f"루프 실패: {type(e).__name__}: {e}"
    if not (out.get("answer") or "").strip():
        out["answer"] = FALLBACK + (f" (사유: {out['note']})" if out.get("note") else "")
    # 공시 근거 없는 상투구는 근거 절을 붙이기 **전에** 지운다.
    # 나중에 지우면 근거 절까지 함께 흔들린다.
    try:
        out["answer"] = strip_boilerplate(out["answer"])
    except Exception as e:                     # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        out["note"] = (out.get("note") or "") + f" · 상투구 제거 실패: {e}"
    # 근거 절은 모델이 아니라 여기서 만든다. 형식이 흔들리면 안 된다.
    #
    # 【retrieved 를 따로 붙드는 이유 — 2026-09-03】
    # attach_sources 는 이제 '본문이 가리킨 것' 만 남기므로 out["sources"] 가 비어 나올 수
    # 있다. 그런데 아래 무근거 표시는 **도구가 아무것도 못 가져왔는가**를 물어야 한다.
    # 필터링된 목록으로 물으면, 문서를 제대로 조회하고 답한 문항에까지
    # "공시 조회로 확인되지 않았습니다" 딱지가 붙는다(실측: e2e 재실행 3번째 문항).
    retrieved = list(out.get("sources") or [])
    try:
        out["answer"], out["sources"] = attach_sources(out["answer"], retrieved)
    except Exception as e:                     # noqa: BLE001 — 붙이다 죽으면 안 된다
        traceback.print_exc(file=sys.stderr)
        out["note"] = (out.get("note") or "") + f" · 근거 절 조립 실패: {e}"
    # 근거 0건인데 숫자·사실을 단정했으면 지우지 말고 표시만 한다
    try:
        if has_unsourced_claim(out["answer"], retrieved):
            out["unsourced_claim"] = True
            out["answer"] = mark_unsourced(out["answer"])
    except Exception as e:                     # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        out["note"] = (out.get("note") or "") + f" · 무근거 표시 실패: {e}"
    out["elapsed"] = round(time.time() - t0, 2)
    return out


def _keep_documents(out: dict, name: str, tool_out: dict) -> None:
    """검색이 올린 청크를 원문째 모아 둔다. 같은 청크는 한 번만.

    `sources` 는 `[회사 보고서명 …, 접수번호 …]` 문자열이라 본문이 없다.
    평가 서버가 "답변 생성에 참고한 검색 문서" 를 내보내려면 본문이 필요하다.
    """
    if name != "search_disclosure":
        return
    seen = {d["chunk_id"] for d in out["documents"]}
    for h in (tool_out.get("results") or []):
        cid = str(h.get("chunk_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        src = h.get("source") or {}
        out["documents"].append({
            "chunk_id": cid,
            "corp_name": str(src.get("corp_name") or ""),
            "report_nm": str(src.get("report_nm") or ""),
            "rcept_no": str(src.get("rcept_no") or ""),
            "rcept_dt": str(src.get("rcept_dt") or ""),
            "section": str(src.get("section") or ""),
            "note": str(src.get("note") or ""),
            # 인접 청크 보강본이 있으면 그쪽이 LLM 이 실제로 본 내용이다
            "text": str(h.get("augmented") or h.get("text") or ""),
        })


def _account(out: dict, res: dict, model: str, qid) -> None:
    """LLM 호출 1건을 문항 합계에 더하고 토큰 로그에 남긴다.

    usage 가 없는 응답(실패·재시도 소진 등)은 0 으로 채우지 않고 `unknown_calls` 로
    센다. "안 썼다" 와 "모른다" 를 섞으면 예산이 조용히 샌다.
    """
    row = usage_mod.record(
        model=res.get("model") or model,
        usage_dict=res.get("usage"),
        qid=qid,
        ok=bool(res.get("ok")),
        retries=res.get("retries", 0),
        retry_429=res.get("retry_429", 0),
        retry_wait_s=res.get("retry_wait_s", 0.0),
        elapsed=res.get("elapsed"),
        note="" if res.get("ok") else str(res.get("error") or "")[:200],
    )
    u, w = out["usage"], out["waste"]
    u["llm_calls"] += 1
    if row["prompt_tokens"] is None or row["completion_tokens"] is None:
        # 내역을 모르면 금액을 낼 수 없다. 출력 단가가 입력의 4배라 비율을 가정하면
        # 그 추정이 최대 4배 틀린다. 0 으로 채우지 않고 '모른다' 로 센다.
        u["unknown_calls"] += 1
    else:
        u["prompt"] += row["prompt_tokens"]
        u["completion"] += row["completion_tokens"]
        u["total"] = u["prompt"] + u["completion"]
        u["est_won"] = round(usage_mod.won(u["prompt"], u["completion"]), 2)

    w["http_retry"] += row["retries"]
    w["retry_429"] += row["retry_429"]
    # 400 재시도는 토큰 로그가 아니라 응답 dict 에서 바로 읽는다
    w["retry_400"] += int(res.get("retry_400") or 0)
    w["tools_dropped"] = w["tools_dropped"] or bool(res.get("tools_dropped"))
    w["retry_wait_s"] = round(w["retry_wait_s"] + row["retry_wait_s"], 1)
    # 재시도는 같은 본문을 다시 보낸다. 서버가 그것을 과금했는지는 알 수 없으므로
    # '다시 보낸 프롬프트 토큰' 이라는 사실만 추정치로 남기고 비용에는 더하지 않는다.
    if row["retries"] and row["prompt_tokens"]:
        w["resent_prompt_tokens_est"] += row["retries"] * row["prompt_tokens"]


def _run(question, out, t0, model, max_calls, deadline, verbose, qid=None) -> dict:
    from src.agent import clova
    from src.tools import TOOLS

    tools = load_tools()
    msgs = [{"role": "system", "content": build_system()},
            {"role": "user", "content": str(question)}]
    cache: dict = {}
    n_calls = 0
    reminded = False           # 리마인더는 딱 한 번만 넣는다
    reviewed = False           # 도구 없이 답하는 경로의 검토도 딱 한 번만
    nudged = False             # 조회 없이 답하려는 것을 되돌리는 지시도 한 번만
    list_only_nudged = False   # 목록만 보고 본문에 답하려는 경로도 한 번만
    audited = False            # 최종 답변 검문도 문항당 한 번만 — 무한 루프 금지
    bad_tools: set = set()     # 직전 턴에 error/not_found 를 돌려준 도구 이름

    while True:
        # 비용 상한 — LLM 을 부르기 **전에** 본다. 부르고 나서 보면 이미 쓴 뒤다.
        budget = usage_mod.active_budget()
        if budget is not None and budget.exceeded():
            out["stopped_by_budget"] = True
            out["note"] = "비용 상한 초과로 중단 — " + budget.line()
            break

        over_time = (time.time() - t0) > deadline
        extra = None if (over_time or not tools) else {"tools": tools, "toolChoice": "auto"}
        if over_time and not out["note"]:
            out["note"] = f"{deadline:.0f}초를 넘겨 도구 없이 마지막 답변을 받았다"

        t1 = time.time()
        res = clova.chat(msgs, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
                         model=model, extra=extra)
        out["llm_seconds"] += time.time() - t1
        _account(out, res, clova.resolve_model(model), qid)
        if not res.get("ok"):
            out["note"] = f"LLM 호출 실패: {res.get('error')}"
            break

        calls = res.get("tool_calls") or []
        if not calls:
            text = (res.get("text") or "").strip()
            # 아직 도구를 한 번도 안 불렀다면 두 번까지 되돌린다.
            #   1) FORCE_TOOL — 조회부터 하라 (근거 없는 거절을 막는다)
            #   2) REVIEW     — 그래도 도구 없이 답하면 추측 문장만이라도 걷어낸다
            if extra is not None and not out["tool_calls"]:
                if not nudged:
                    nudged = True
                    out["waste"]["no_tool_nudge"] = True
                    msgs.append({"role": "assistant", "content": text})
                    msgs.append({"role": "user", "content": FORCE_TOOL})
                    continue
                if not reviewed:
                    reviewed = True
                    msgs.append({"role": "assistant", "content": text})
                    msgs.append({"role": "user", "content": REVIEW})
                    continue
            # list_disclosures 는 문서 제목·접수일·건수만 준다. 성공한 본문 검색이
            # 하나도 없는데 답하려 하면 한 번 되돌려 목록→본문 단절을 막는다.
            if extra is not None and out["tool_calls"] and not list_only_nudged:
                listed = any(c["name"] == "list_disclosures" and c["status"] == "ok"
                             for c in out["tool_calls"])
                searched = any(c["name"] == "search_disclosure" and c["status"] == "ok"
                               for c in out["tool_calls"])
                if listed and not searched:
                    list_only_nudged = True
                    out["waste"]["list_only_nudge"] = True
                    msgs.append({"role": "assistant", "content": text})
                    msgs.append({"role": "user", "content": LIST_ONLY_NUDGE})
                    continue
            # 최종 답변 검문 — 문항당 한 번. 남은 시간이 8초 미만이면 건너뛴다.
            # 검문 순서는 계산 → 검색 없는 거절. 둘 다 걸려도 한 번만 되돌린다.
            if extra is not None and not audited and \
                    (deadline - (time.time() - t0)) >= AUDIT_MARGIN:
                # elif 사슬이라 **하나만** 걸린다. `audited` 가 문항당 1회를 보장한다.
                kind, note = None, None
                if needs_calc(text, out["tool_calls"], out["documents"]):
                    kind, note = "calc", AUDIT_CALC
                elif compares_without_calc(text, out["tool_calls"]):
                    kind, note = "compare", AUDIT_COMPARE
                elif refused_without_search(text, out["tool_calls"]):
                    kind, note = "no_search", AUDIT_NO_SEARCH
                elif all_tools_failed(text, out["tool_calls"]):
                    kind, note = "all_bad", AUDIT_ALL_BAD
                elif announced_but_not_called(text, out["tool_calls"]):
                    kind, note = "announced", AUDIT_ANNOUNCED
                if kind:
                    audited = True
                    out["waste"]["audit"] = kind
                    msgs.append({"role": "assistant", "content": text})
                    msgs.append({"role": "user", "content": note})
                    continue

            out["answer"] = text
            if not out["tool_calls"]:
                out["waste"]["answered_without_tools"] = True
            break
        if extra is None:
            out["answer"] = (res.get("text") or "").strip()
            break

        # assistant 가 무엇을 불렀는지 그대로 되돌려 놓아야 tool 메시지가 이어진다
        msgs.append({"role": "assistant", "content": res.get("text") or "",
                     "toolCalls": calls})

        for call in calls:
            name = ((call.get("function") or {}).get("name")) or ""
            args, err = _args_of(call)
            n_calls += 1

            if err:
                tool_out = {"status": "error", "note": err,
                            "results": [], "sources": []}
            elif n_calls > max_calls:
                tool_out = {"status": "error", "results": [], "sources": [],
                            "note": "호출 한도 초과. 지금까지의 근거로 답변할 것"}
            elif name not in TOOLS:
                tool_out = {"status": "error", "results": [], "sources": [],
                            "note": f"그런 도구가 없다: {name}. "
                                    f"쓸 수 있는 것: {', '.join(TOOLS)}"}
            else:
                key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True,
                                        default=str))
                if name in bad_tools:
                    # 앞 턴에서 error/not_found 를 받고 같은 도구를 다시 부른 것 =
                    # 모델이 스스로 고쳐 재시도했다. 낭비는 아니지만, 잦으면
                    # 검색어 규칙이나 도구 note 가 부족하다는 신호다.
                    out["waste"]["retry_after_bad"] += 1
                    bad_tools.discard(name)
                if key in cache:
                    # 캐시 '결과' 를 다시 주면 안 된다. 같은 문서를 또 보여주면 모델이
                    # "아직 못 찾았다" 로 읽고 같은 호출을 반복한다(실측: 6회 중 2회 낭비).
                    # 결과 없이 error 로 끊어야 다음 단계로 넘어간다.
                    out["waste"]["repeat_blocked"] += 1
                    tool_out = {"status": "error", "results": [], "sources": [],
                                "note": "이미 같은 인자로 조회했다. 그 결과는 위 대화에 "
                                        "그대로 있다. 다시 부르지 말고, 다른 검색어·다른 "
                                        "시점으로 바꾸거나 지금까지의 근거로 답하라"}
                else:
                    t2 = time.time()
                    tool_out = TOOLS[name](**args)
                    out["tool_seconds"] += time.time() - t2
                    cache[key] = tool_out

            if tool_out.get("status") in ("error", "not_found"):
                bad_tools.add(name)

            for s in (tool_out.get("sources") or []):
                if s not in out["sources"]:
                    out["sources"].append(s)
            _keep_documents(out, name, tool_out)
            out["tool_calls"].append({
                "n": n_calls, "name": name, "arguments": args,
                "status": tool_out.get("status"),
                "summary": _summary(name, tool_out),
                "note": tool_out.get("note") or ""})

            msgs.append({"role": "tool", "toolCallId": call.get("id") or "",
                         "name": name, "content": _render(name, tool_out)})

        if not reminded:
            # 도구 호출 횟수에는 넣지 않는다. 이건 메시지일 뿐이다
            msgs.append({"role": "user", "content": REMINDER})
            reminded = True

        if n_calls >= max_calls:
            # 다음 턴에는 도구를 주지 않는다 — 있는 근거로 답하게 한다
            tools = []
            out["waste"]["hit_call_limit"] = True
            if not out["note"]:
                out["note"] = f"도구 호출 {max_calls}회 한도에 도달해 답변 단계로 넘어갔다"

    if verbose:
        out["messages"] = msgs
    return out
