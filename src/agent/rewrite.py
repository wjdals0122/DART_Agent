"""질문 재작성(사전 분석) — 에이전트 루프 맨 앞의 LLM 1회.

  from src.agent.rewrite import rewrite_question
  rw = rewrite_question("한화오션의 작년 정정공시 내용 알려줘")
  # {"ok": True, "analysis": {...}, "note": "", "res": <clova 응답>, "elapsed": 4.2}

【왜 필요한가 — 2026-09-04 실측】
실패 원인 다수가 "질문을 도구 인자로 옮기는" 단계의 실수였다:
  - 회사명을 query 에만 넣고 corp_name 을 비움 (후보 145,510)
  - rcept_dt 에 최초 신고일을 못 박아 정정공시가 빠짐
  - fiscal_year 와 rcept_dt 를 함께 걸어 구조적 0건
  - corp_name='전체'
그리고 "올해·작년·최근" 같은 상대 시점이 해석되지 않는다.

【계약 — 어떤 경우에도 지킨다】
- `rewrite_question()` 은 **예외를 던지지 않는다.** LLM 실패(400/429/타임아웃)·
  JSON 파싱 실패·시간 초과 전부 {"ok": False, "note": 사유} 로 돌아온다.
  부르는 쪽(loop)은 ok=False 면 이 단계를 통째로 건너뛰고 지금과 똑같이 진행한다.
- 재작성에 `REWRITE_BUDGET_S`(15초) 이상 걸리면 **결과를 버린다.**
  (429 백오프가 길어지면 시간 자체는 이미 쓴 뒤다 — clova.chat 에 재시도 조절
  손잡이가 없어 지출을 막지는 못하고, 낡은 분석을 쓰는 것만 막는다)
- 분석은 **힌트다.** 도구 인자를 코드가 강제로 채우지 않는다. out_of_scope 여도
  검색을 막지 않는다. 시스템 프롬프트의 [질문 분석] 블록으로만 전달한다.
- API 응답의 question 필드에는 언제나 **원문**이 들어간다. 재작성문을 넣으면
  주최측 규격 위반이다(테스트로 못박음 — eval/test_rewrite.py).
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path

REWRITE_BUDGET_S = 15.0
MAX_TOKENS = 1024

# ---------------------------------------------------------------- 회계연도 범위
#
# 【실측치다. 짐작해서 적은 값이 아니다 — 2026-09-06】
# corpus/manifest.jsonl 의 report_nm '(YYYY.MM)' 을 전량(1,054건) 집계한 결과:
#
#   2023  338건  (annual 109 · half 75 · quarter 154)
#   2024  332건  (annual  97 · half 82 · quarter 153)
#   2025  311건  (annual  85 · half 77 · quarter 149)
#   2026   73건  (**quarter 73 뿐**)
#
# 즉 코퍼스에 문서가 있는 사업연도는 2023~2026 이지만, **연간 실적을 물을 수 있는
# 최대 사업연도는 2025** 다. 2026 은 2026.03 분기보고서 73건뿐이고 그것은
# 2026 회계연도 1분기이지 연간 실적이 아니다. 2026 회계연도 사업보고서는
# 2027년 3월경 접수되므로 이 코퍼스에 없다.
#
# 이 두 상수는 `eval/test_rewrite.py` 가 manifest 를 다시 집계해 대조한다 —
# 코퍼스가 바뀌면 테스트가 먼저 깨진다(LLM 0회).
FY_MIN = 2023           # 가장 이른 사업연도
FY_MAX = 2025           # 연간 실적이 있는 가장 늦은 사업연도
FY_MAX_QUARTER = 2026   # 분기보고서까지 포함한 가장 늦은 사업연도
                        # 실측: 2026 은 quarter 73건 · annual 0건
                        # (전부 「분기보고서 (2026.03)」 — 2026 회계연도 1분기)

# 범위 밖 연도가 왔을 때 [질문 분석] 블록에 남기는 안내.
# 검문(확률적)에 맡기지 않고 여기서 결정적으로 처리한다 — 범위 밖 연도를 그대로
# fiscal_year 로 넘기면 하드필터가 0건을 만들고, 그 0건에서 모델이 회복하지 못한
# 사례가 있었다(v6 1·9 — 도구 0회 거절).
_FY_NOTE_FUTURE = (
    "질문이 가리키는 {y}년의 연간 실적은 코퍼스에 없다. "
    "확인 가능한 최신 사업연도는 {fy_max} 이다. 답변에 이 사실을 밝히고 "
    "{fy_max} 기준 실적을 제시하라"
)
# FY_MAX 는 넘지만 분기보고서는 있는 해(2026). "연간이 없다" 는 맞지만
# "2025 로 답하라" 만 시키면 **분기를 물은 질문에 틀린 해를 답하게 만든다.**
# 실측 73건(2026.03 분기보고서)이 걸린 경로라 문구를 나눈다.
_FY_NOTE_QUARTER_ONLY = (
    "질문이 가리키는 {y}년은 연간 실적이 없다. "
    "{y}.03 분기보고서는 있으므로, 분기 실적을 물었다면 report_type='quarter' 로 "
    "조회하고, 연간을 물었다면 {fy_max} 기준으로 답하라"
)
_FY_NOTE_PAST = (
    "질문이 가리키는 {y}년은 코퍼스에 없다 — {fy_min} 이전은 코퍼스에 없다. "
    "확인 가능한 가장 이른 사업연도는 {fy_min} 이다. 답변에 이 사실을 밝혀라"
)


# ------------------------------------------------- 여러 시점에 걸친 질문 (step53)
#
# 【왜 코드로 하는가 — 실측 2026-09-06】
# "SK하이닉스의 최근 자금조달 및 지분변동" 을 두 번 돌렸더니 답이 갈렸다.
#   1회차  근거 2023.06~2026.03 (7건) — 좋은 답
#   2회차  근거 2024.03~2024.12 (4건) — 단위 1000배 오류 · 최대주주 혼동
# 재작성기가 실행마다 fiscal_years 를 다르게 채운 탓이다. 프롬프트 규칙(2-B)으로는
# 매번 지켜지지 않는다 — 같은 질문에 답이 갈리는 것을 프롬프트로 막을 수 없다.
#
# 【조건을 좁게 잡은 이유】
# "연도가 없으면 전부 비운다" 로 하면 "삼성전자 매출은?" 까지 필터가 빠져 후보가
# 4배가 된다. 그래서 **여러 시점을 훑어야 답이 되는 질문**만 고른다.
#   (a) 질문 원문에 4자리 연도(2020~2030)도 '제N기' 표기도 없고
#   (b) 아래 낱말 중 하나가 있을 때
# '작년·전년·올해' 같은 상대 연도는 여기서 안 건드린다 — 코드 환산이 한다(step65).
_Q_YEAR = re.compile(r"(?<!\d)20(?:2\d|30)(?!\d)")      # 2020~2030
_Q_KI = re.compile(r"제\s*\d+\s*기")
# 【'현황'·'변동' 을 뺐다 — step56】
# "삼성전자 임원 현황은?" 처럼 단일 시점 질문에도 '현황' 이 흔하고, "지분 변동"
# 하나로 여러 시점을 뜻한다고 볼 수 없다. 오탐(연도 필터가 빠져 후보가 4배 →
# 분기·반기가 상위 잠식)이 미탐보다 나쁘다.
# "지분율 변동 이력" 같은 질문은 '이력' 으로 여전히 잡힌다.
_MULTI_PERIOD_WORDS = ("최근", "지금", "현재", "요즘", "최신", "이력", "추이")

# 【부작용 대비】 필터를 빼면 후보가 늘어 분기·반기가 상위를 잠식할 수 있다.
# 그래서 "연간 실적은 사업보고서 우선" 을 안내에 넣는다. report_type 을 코드가
# 강제하지는 **않는다** — 그러면 분기를 묻는 질문을 막게 된다.
_MULTI_PERIOD_NOTE = ("여러 시점에 걸친 질문이다. 연도 필터 없이 조회하되, "
                      "연간 실적을 묻는 경우 사업보고서를 우선한다")


def is_multi_period(question) -> bool:
    """질문이 '여러 시점에 걸친' 것인가. 예외를 던지지 않는다."""
    try:
        q = str(question or "")
        if _Q_YEAR.search(q) or _Q_KI.search(q):
            return False
        return any(w in q for w in _MULTI_PERIOD_WORDS)
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return False


# ------------------------------------------------- 상대 연도 환산 (step65)
#
# 【왜 코드로 하는가 — 실측 2026-09-06】
# "삼성전자 작년 영업이익" 을 재작성이 "직전 회계연도 2024" 로 환산했다.
# 오늘은 2026년이므로 작년은 2025 다. 그 결과 2024 사업보고서를 검색했고,
# 거기서 다시 2023 열(6,566,976)을 읽었다 — 두 해가 밀렸다.
# 산수는 모델이 아니라 코드가 한다. 현재 연도는 datetime 에서 잡는다(하드코딩 금지).
#
# 순서 주의 — '재작년' 이 '작년' 을, '전전년' 이 '전년' 을 품는다. 긴 것 먼저.
_REL_N_YEARS = re.compile(r"([0-9]+|[일이삼사오육칠팔구]|십)\s*년\s*전")
_KOR_DIGIT = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
              "육": 6, "칠": 7, "팔": 8, "구": 9, "십": 10}
_REL_WORDS = (
    ("재작년", 2), ("전전년", 2),
    ("작년", 1), ("전년", 1), ("지난해", 1),
    ("올해", 0), ("금년", 0), ("이번 해", 0), ("이번해", 0),
)


def resolve_relative_year(question, now_year=None):
    """(환산 연도 또는 None, 걸린 표현, 기준 연도). 예외를 던지지 않는다.

    `now_year` 는 테스트에서 고정할 수 있게 인자로 뺐다. 기본은 datetime.
    """
    try:
        if now_year is None:
            from datetime import datetime
            now_year = datetime.now().year
        q = str(question or "")
        # 명시 연도·기수가 있으면 건드리지 않는다 — "2024년 매출의 전년 대비 증가율"
        # 에서 '전년' 이 2024 를 덮어쓰면 안 된다. 명시가 언제나 이긴다(step53 과 동일).
        if _Q_YEAR.search(q) or _Q_KI.search(q):
            return None, "", now_year
        m = _REL_N_YEARS.search(q)               # "N년 전" 이 낱말보다 구체적이다
        if m:
            tok = m.group(1)
            n = int(tok) if tok.isdigit() else _KOR_DIGIT[tok]
            return now_year - n, m.group(0), now_year
        for word, back in _REL_WORDS:
            if word in q:
                return now_year - back, word, now_year
        return None, "", now_year
    except Exception:                          # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return None, "", now_year


def clip_fiscal_years(years) -> tuple:
    """(코퍼스 범위 안의 연도만, 범위 밖에 대한 안내 문자열).

    예외를 던지지 않는다. 숫자가 아닌 값은 조용히 버리지 않고 **연도가 아니므로**
    애초에 여기 오기 전(_parse)에 걸러진다.

    범위 밖 값은 **버린다.** 남겨서 fiscal_year 필터로 넘기면 후보가 0건이 되고,
    0건에서 되돌아오는 것은 모델의 확률에 달린 일이 된다. 필터를 걸지 않으면
    검색은 그 회사의 전 기간에서 돌고, 무엇을 답해야 하는지는 안내가 알려 준다.
    """
    kept, notes, seen = [], [], set()
    for y in (years or []):
        try:
            y = int(y)
        except (TypeError, ValueError):
            continue
        if FY_MIN <= y <= FY_MAX:
            kept.append(y)
        elif y in seen:
            continue
        elif FY_MAX < y <= FY_MAX_QUARTER:
            seen.add(y)
            notes.append(_FY_NOTE_QUARTER_ONLY.format(y=y, fy_max=FY_MAX))
        elif y > FY_MAX_QUARTER:
            seen.add(y)
            notes.append(_FY_NOTE_FUTURE.format(y=y, fy_max=FY_MAX))
        else:
            seen.add(y)
            notes.append(_FY_NOTE_PAST.format(y=y, fy_min=FY_MIN))
    return kept, " / ".join(notes)

# 출력 JSON 의 키와 기본값. 이 모양 밖의 키는 버린다.
_DEFAULTS = {
    "rewritten": "",
    "sub_questions": [],
    "companies": [],
    "fiscal_years": [],
    "ki": [],
    "doc_groups": [],
    "time_note": "",
    "out_of_scope": False,
}

# 재작성 실패 원본 기록. e2e_v5 에서 3/40 이 파싱 실패로 생략됐는데 원본이
# 안 남아 원인을 못 짚었다(재현으로 확인: JSON 없이 산문만 출력하는 형식 이탈).
_ROOT = Path(__file__).resolve().parents[2]
FAIL_LOG = _ROOT / "logs" / "rewrite_fail.jsonl"


def _log_fail(question, text, note) -> None:
    try:
        FAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(FAIL_LOG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "note": note, "question": str(question)[:500],
                                 "text": str(text)[:4000]}, ensure_ascii=False) + "\n")
    except Exception:                          # noqa: BLE001 — 로그가 답을 막으면 안 된다
        traceback.print_exc(file=sys.stderr)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)

# doc_groups 는 질문이 공시 유형을 **명시적으로** 말했을 때만 인정한다.
#
# 【왜 코드인가 — 2026-09-04 실측 2회】
# "내용으로 추정하지 마라" 를 규칙으로도, 예시 주석으로도 넣어 봤지만 두 번 다
# "영업이익…정정공시…" 질문에 ["periodic"] 을 채웠다. 프롬프트로 세 번 막아도
# 새는 것은 코드에서 정한다(이 리포의 반복된 교훈). 실제로 유형을 좁혔다가
# 정답 문서를 놓친 사례가 있다(OCI홀딩스 Wacker 특허소송 — major 로 좁힘).
# 질문 원문에 아래 낱말이 있어야 그 그룹을 남긴다. 없으면 버린다.
_GROUP_WORDS = {
    "periodic": ("정기공시", "정기보고서", "사업보고서", "반기보고서", "분기보고서"),
    "exchange": ("거래소 공시", "거래소공시", "수시공시", "단일판매"),
    "major": ("주요사항보고서", "주요사항 보고서"),
    "holding": ("지분공시", "지분 공시", "대량보유"),
}


def _filter_doc_groups(groups, question) -> list:
    """질문이 명시한 유형만 남긴다. 모르는 값·추정된 값은 버린다 (예외 없음)."""
    try:
        q = str(question or "")
        return [g for g in (groups or [])
                if g in _GROUP_WORDS and any(w in q for w in _GROUP_WORDS[g])]
    except Exception:                          # noqa: BLE001 — 힌트 정제가 답을 막으면 안 된다
        traceback.print_exc(file=sys.stderr)
        return []


def _corp_names() -> str:
    """코퍼스 70개사 법인명. 못 읽어도 재작성은 진행한다(목록 없이)."""
    try:
        from src.search.aliases import all_companies
        return " · ".join(c["corp_name"] for c in all_companies())
    except Exception:                          # noqa: BLE001 — 목록은 보조 정보다
        traceback.print_exc(file=sys.stderr)
        return "(목록을 읽지 못했다 — 질문에 적힌 회사명을 그대로 쓴다)"


def build_prompt() -> str:
    return "\n".join([
        "너는 DART 공시 질의응답 시스템의 질문 분석기다. 사용자 질문을 검색하기 좋게",
        "다시 쓰고 구조화한다. JSON 하나만 출력한다. 설명·마크다운·코드펜스 금지.",
        "",
        "[코퍼스]",
        "- 70개사, 2023년 1월 ~ 2026년 3월에 접수된 공시만 있다",
        "- 법인명 목록 (companies 에는 이 표기만 쓴다):",
        "  " + _corp_names(),
        "",
        "[출력 JSON — 이 6개 키를 전부 넣는다]",
        '{"rewritten": "질문을 명확하게 다시 쓴 문장", "sub_questions": ["..."],',
        ' "companies": [], "fiscal_years": [], "doc_groups": [], "time_note": ""}',
        "",
        "[규칙]",
        "- rewritten: 상대 시점을 절대 시점으로, 축약 회사명을 정식 법인명으로,",
        "  모호한 표현을 공시 용어로 바꾼다. 질문의 의도를 바꾸지 마라.",
        "  없는 조건을 더하지 마라",
        "- 여러 질문이 섞여 있으면 sub_questions 로 나누고, rewritten 에는 전체를",
        "  명확히 다시 쓴 것을 넣는다. 질문이 하나면 sub_questions 도 1개다",
        "- '올해·최근·현재' 는 오늘 날짜가 아니라 코퍼스에서 확인 가능한 가장 최근",
        "  시점(2026년 3월 접수분까지)으로 해석한다. 해석 근거를 time_note 에 적는다",
        "- 질문에 연도가 명시돼 있으면(예: '2024년') **그 연도를 그대로** 쓴다.",
        "  명시된 연도를 바꾸지 마라",
        "- companies: 위 목록의 법인명으로 정규화한다. '전체·모든·상장사' 류는",
        "  회사가 아니므로 companies 를 [] 로 둔다",
        "- fiscal_years: 질문에 명시된('2024년') 회계연도(정수)만.",
        "  시점이 열려 있는 질문('최신·최종·현재·작성기준일 이후·",
        "  이력·정리·현황')은 **비워라** — 연도를 걸면 다른 해의 건이 통째로 빠진다(실측)",
        "- '제50기' 같은 기수는 연도로 환산하지 말고 fiscal_years 에도 넣지 마라.",
        "  rewritten 에 '제50기' 표현을 그대로 남겨라 — 검색이 본문에서 기수를 찾는다",
        "- doc_groups: 질문이 공시 유형을 **명시적으로 말했을 때만** 채운다",
        "  (\"주요사항보고서에서\", \"거래소 공시 중\", \"지분공시\" 등).",
        "  periodic(정기보고서)·exchange(거래소 수시공시)·major(주요사항보고서)·",
        "  holding(지분공시) 중에서.",
        "  **내용으로 추정하지 마라.** 소송·재무·이사회가 어디 실리는지 짐작해서",
        "  채우면 안 된다 — 실제로 유형을 좁혔다가 정답 문서를 놓친 사례가 있다.",
        "  확신이 없으면 빈 배열 [] 로 둔다",
        "- 질문이 과거 사건을 언급해도 그 사건은 2023~2026 보고서에 기재될 수 있다.",
        "  범위 밖이라고 스스로 판정하지 마라 — 범위 판단은 검색 결과가 한다",
        "- 확신이 없으면 비워라. 추측해 채우지 마라",
        "",
        # 규칙 문장보다 예시가 잘 먹힌다(이 리포의 반복된 교훈). 실측으로
        # '가장 좋은 회사는?' 류 질문에서 마크다운 머리말·굵은 키 이름으로
        # 형식이 깨졌다 — 어려운 질문일수록 예시 모양을 베끼게 한다.
        "[예시 1 — 질문: \"요즘 실적 제일 좋은 데가 어디야?\"] (상대 시점 환산)",
        '{"rewritten": "코퍼스에서 확인 가능한 가장 최근 회계연도(2025년) 실적이 '
        '가장 좋은 회사는 어디인가?", "sub_questions": ["코퍼스에서 확인 가능한 '
        '가장 최근 회계연도(2025년) 실적이 가장 좋은 회사는 어디인가?"], '
        '"companies": [], "fiscal_years": [2025], "doc_groups": [], '
        '"time_note": "\'요즘\'을 코퍼스 최신 접수분(2026.03)에 담긴 2025 '
        '회계연도로 해석"}',
        "",
        "[예시 2 — 질문: \"2023년 매출 1위는? 그리고 삼성전자의 2023년 배당은?\"]",
        "(질문 둘 혼합 · 명시된 연도는 그대로 · 매출·배당·정정공시처럼 **내용**만",
        " 있고 공시 유형을 말하지 않았으므로 doc_groups 는 빈 배열이다)",
        '{"rewritten": "2023 회계연도 매출액이 가장 큰 회사와 삼성전자의 2023 '
        '회계연도 배당 내용을 묻는다", "sub_questions": ["2023 회계연도 매출액이 '
        '가장 큰 회사는 어디인가?", "삼성전자의 2023 회계연도 배당은 어떠했는가?"], '
        '"companies": ["삼성전자"], "fiscal_years": [2023], "doc_groups": [], '
        '"time_note": ""}',
        "",
        "예시의 회사·연도를 베끼지 마라 — 베낄 것은 **모양**이지 값이 아니다.",
        "위 예시처럼 **JSON 하나만** 출력한다. 설명·머리말·꼬리말·마크다운·굵은",
        "글씨 전부 금지. 출력의 첫 글자는 { 이고 마지막 글자는 } 다.",
    ])


def _parse(text: str):
    """모델 출력에서 JSON 하나를 꺼낸다. 실패하면 None (예외 없음).

    【왜 find/rfind 가 아니라 raw_decode 스캔인가 — 실측】
    모델이 JSON 앞뒤에 산문·코드펜스를 붙인다. 첫 '{' 와 마지막 '}' 를 자르는
    방식은 머리말에 '{' 가 있거나 꼬리말에 '}' 가 있으면 통째로 깨진다.
    `JSONDecoder.raw_decode` 는 그 위치에서 시작하는 **완결된 JSON 하나**만 읽고
    뒤는 무시하므로, '{' 후보를 차례로 시도하면 어디에 묻혀 있어도 꺼낸다.
    """
    try:
        s = _FENCE.sub("", (text or "").strip()).strip()
        dec = json.JSONDecoder()
        d = None
        pos = s.find("{")
        while pos != -1:
            try:
                cand, _ = dec.raw_decode(s[pos:])
                if isinstance(cand, dict):
                    d = cand
                    break
            except ValueError:
                pass
            pos = s.find("{", pos + 1)
        if d is None:
            return None
        out = {}
        for k, dv in _DEFAULTS.items():
            v = d.get(k, dv)
            if isinstance(dv, list):
                v = [x for x in v if isinstance(x, (str, int, float))] \
                    if isinstance(v, list) else []
                if k in ("fiscal_years", "ki"):
                    ys = []
                    for x in v:
                        try:
                            ys.append(int(x))
                        except (TypeError, ValueError):
                            continue
                    v = ys
                else:
                    v = [str(x).strip() for x in v if str(x).strip()]
            elif isinstance(dv, bool):
                v = bool(v)
            else:
                v = str(v or "").strip()
            out[k] = v
        return out
    except Exception:                          # noqa: BLE001 — 파싱 실패는 생략 사유일 뿐
        traceback.print_exc(file=sys.stderr)
        return None


def rewrite_question(question, model=None, budget_s=None) -> dict:
    """질문 하나를 분석한다. 예외 없음.

    반환: {"ok": bool, "analysis": dict|None, "note": str,
           "res": clova 응답|None, "elapsed": float}
    `res` 는 성공·실패와 무관하게 토큰 회계(_account)용으로 그대로 돌려준다.
    """
    if budget_s is None:
        budget_s = REWRITE_BUDGET_S
    out = {"ok": False, "analysis": None, "note": "", "res": None,
           "res_retry": None, "elapsed": 0.0}
    try:
        q = str(question or "").strip()
        if not q:
            out["note"] = "질문이 비어 있다"
            return out
        from src.agent import clova
        msgs = [{"role": "system", "content": build_prompt()},
                {"role": "user", "content": q}]
        t0 = time.time()
        res = clova.chat(msgs, max_tokens=MAX_TOKENS, temperature=0.1, model=model)
        out["elapsed"] = round(time.time() - t0, 2)
        out["res"] = res
        if not res.get("ok"):
            out["note"] = f"LLM 실패: {str(res.get('error'))[:120]}"
            return out
        if out["elapsed"] > budget_s:
            out["note"] = f"{budget_s:.0f}초 초과({out['elapsed']:.1f}초) — 결과를 버림"
            return out
        a = _parse(res.get("text") or "")
        if a is None:
            # 실측(e2e_v5 3/40): JSON 없이 산문만 출력하는 형식 이탈. 파서로는
            # 구제 불가라 원본을 남기고 같은 입력으로 한 번만 다시 부른다.
            # 【재시도 온도 0.5 — step41】 같은 온도(0.1) 재시도는 같은 산문을
            # 재생성해 무의미했다(22번 0/3 실측). 표본을 흔들어야 형식이 돌아온다.
            _log_fail(q, res.get("text"), "JSON 파싱 실패(1차)")
            time.sleep(2.0)
            res2 = clova.chat(msgs, max_tokens=MAX_TOKENS, temperature=0.5,
                              model=model)
            out["elapsed"] = round(time.time() - t0, 2)
            out["res_retry"] = res2            # 토큰 회계용 — 부른 건 부른 것이다
            if res2.get("ok"):
                a = _parse(res2.get("text") or "")
            if a is None:
                if res2.get("ok"):
                    _log_fail(q, res2.get("text"), "JSON 파싱 실패(재시도)")
                out["note"] = "JSON 파싱 실패(재시도 포함)"
                return out
        # 유형 추정 차단 — 질문이 명시하지 않은 doc_groups 는 여기서 걷어낸다
        a["doc_groups"] = _filter_doc_groups(a.get("doc_groups"), q)
        # 기수 가드 — fiscal_years 의 1900 미만 값은 연도가 아니라 기수다.
        # 【step44-B 되돌림】 ki 자동 환산은 표–벤치 기수 체계가 어긋나는 회사가
        # 있어(LG유플러스 제28기: 표 2023 vs 정답 기준 2025) **버린다**.
        # 기수는 rewritten 문장에 남아 검색어로 흘러간다.
        a["fiscal_years"] = [y for y in (a.get("fiscal_years") or []) if y >= 1900]
        a["ki"] = []
        # 상대 연도('작년·올해·N년 전')는 코드가 환산해 재작성 값을 **덮어쓴다**
        # (step65). 결과가 범위 밖이면 바로 아래 clip 이 처리한다.
        # rel_year 는 _DEFAULTS 에 없어 LLM 주입이 안 되고, think_trace 비고에만 쓴다.
        _ry, _rw, _rb = resolve_relative_year(q)
        if _ry is not None:
            a["fiscal_years"] = [_ry]
            a["rel_year"] = {"word": _rw, "year": _ry, "base": _rb}
        # 코퍼스 범위 밖 연도(2026 이상 · 2022 이하)는 버리고 안내를 남긴다.
        # range_note 는 **코드만 채운다** — _DEFAULTS 에 없으므로 LLM 이 넣어도
        # _parse 에서 이미 버려졌다. 여기서 무조건 덮어쓴다.
        a["fiscal_years"], a["range_note"] = clip_fiscal_years(a["fiscal_years"])
        # 여러 시점에 걸친 질문이면 연도 필터를 비운다 — 재작성기가 실행마다 다르게
        # 채워 같은 질문에 답이 갈렸다(step53 주석의 실측).
        # 안내는 **연도 필터가 비게 되었을 때** 붙인다. 재작성기가 애초에 비워 보낸
        # 경우에도 상황은 같으므로, 여기서 갈리면 결정적으로 만드는 의미가 없다.
        if is_multi_period(q):
            a["fiscal_years"] = []
            a["range_note"] = " / ".join(
                x for x in (a["range_note"], _MULTI_PERIOD_NOTE) if x)
        # 【step44-E 되돌림】 out_of_scope 오판이 도구 0회 거절을 만들었다(v6 1·9).
        # 필드는 프롬프트에서 뺐고, 남아 들어와도 무시한다 — 범위 판단은 검색이 한다.
        a["out_of_scope"] = False
        out["ok"], out["analysis"] = True, a
        return out
    except Exception as e:                     # noqa: BLE001 — 마지막 방어선
        traceback.print_exc(file=sys.stderr)
        out["note"] = f"{type(e).__name__}: {e}"
        return out


def analysis_block(a: dict) -> str:
    """시스템 프롬프트에 붙일 [질문 분석] 블록. analysis 가 비면 빈 문자열."""
    try:
        if not a or not (a.get("rewritten") or a.get("companies")
                         or a.get("fiscal_years") or a.get("ki")
                         or a.get("time_note") or a.get("range_note")):
            return ""
        L = ["[질문 분석 — 사전 분석기의 참고 정보]"]
        if a.get("rewritten"):
            L.append(f"- 재작성: {a['rewritten']}")
        subs = a.get("sub_questions") or []
        if len(subs) > 1:
            L.append("- 하위 질문: " + " / ".join(f"{i}) {s}"
                                                for i, s in enumerate(subs, 1)))
        if a.get("companies"):
            L.append("- 회사: " + "·".join(a["companies"])
                     + " (corp_name 필터에 이 법인명을 쓴다)")
        if a.get("fiscal_years"):
            L.append("- 회계연도: " + "·".join(str(y) for y in a["fiscal_years"]))
        if a.get("ki"):
            L.append("- 기수: " + "·".join(f"제{k}기" for k in a["ki"])
                     + " — 도구 호출에 ki=" + ",".join(str(k) for k in a["ki"])
                     + " 로 넣어라. 연도로 짐작 환산하지 마라")
        if a.get("doc_groups"):
            L.append("- 공시 종류: " + "·".join(a["doc_groups"]))
        # 범위 밖 연도 안내는 시점 해석보다 **먼저** 놓는다 — 코드가 판정한 사실이고,
        # LLM 이 쓴 time_note 와 어긋나면 이쪽이 맞다.
        if a.get("range_note"):
            L.append(f"- 시점: {a['range_note']}")
        if a.get("time_note"):
            L.append(f"- 시점 해석: {a['time_note']}")
        L.append("- 이 분석은 참고용이다. 검색 결과가 다르면 검색 결과를 따른다")
        return "\n".join(L)
    except Exception:                          # noqa: BLE001 — 블록 조립이 답을 막으면 안 된다
        traceback.print_exc(file=sys.stderr)
        return ""


def _main() -> int:
    """python -m src.agent.rewrite "질문"  →  분석 JSON 출력 (LLM 1회)."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--model", default=None)
    a = ap.parse_args()
    rw = rewrite_question(a.question, model=a.model)
    print(json.dumps({"ok": rw["ok"], "note": rw["note"], "elapsed": rw["elapsed"],
                      "analysis": rw["analysis"]}, ensure_ascii=False, indent=2))
    return 0 if rw["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(_main())
