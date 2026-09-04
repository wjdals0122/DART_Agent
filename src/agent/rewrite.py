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
        "[출력 JSON — 이 8개 키를 전부 넣는다]",
        '{"rewritten": "질문을 명확하게 다시 쓴 문장", "sub_questions": ["..."],',
        ' "companies": [], "fiscal_years": [], "ki": [], "doc_groups": [],',
        ' "time_note": "", "out_of_scope": false}',
        "",
        "[규칙]",
        "- rewritten: 상대 시점을 절대 시점으로, 축약 회사명을 정식 법인명으로,",
        "  모호한 표현을 공시 용어로 바꾼다. 질문의 의도를 바꾸지 마라.",
        "  없는 조건을 더하지 마라",
        "- 여러 질문이 섞여 있으면 sub_questions 로 나누고, rewritten 에는 전체를",
        "  명확히 다시 쓴 것을 넣는다. 질문이 하나면 sub_questions 도 1개다",
        "- '올해·최근·현재' 는 오늘 날짜가 아니라 코퍼스에서 확인 가능한 가장 최근",
        "  시점(2026년 3월 접수분까지)으로 해석한다. 해석 근거를 time_note 에 적는다",
        "- '작년 매출' 처럼 연도가 상대적이면 회계연도로 환산한다.",
        "  2025 회계연도 사업보고서는 2026년 3월에 접수된다는 점에 주의하라",
        "- 질문에 연도가 명시돼 있으면(예: '2024년') **그 연도를 그대로** 쓴다.",
        "  상대 표현('작년·올해·최근')일 때만 환산한다. 명시된 연도를 바꾸지 마라",
        "- companies: 위 목록의 법인명으로 정규화한다. '전체·모든·상장사' 류는",
        "  회사가 아니므로 companies 를 [] 로 둔다",
        "- fiscal_years: 질문에 명시됐거나('2024년') '작년'처럼 특정 연도로 환산되는",
        "  회계연도(정수)만. 시점이 열려 있는 질문('최신·최종·현재·작성기준일 이후·",
        "  이력·정리·현황')은 **비워라** — 연도를 걸면 다른 해의 건이 통째로 빠진다(실측)",
        "- ki: '제50기' 같은 기수의 N(정수). **제N기는 fiscal_years 가 아니라",
        "  ki 에 넣는다. 연도로 환산하지 마라** — 도구가 회사별 표로 환산한다.",
        "  기수 언급이 없으면 []",
        "- doc_groups: 질문이 공시 유형을 **명시적으로 말했을 때만** 채운다",
        "  (\"주요사항보고서에서\", \"거래소 공시 중\", \"지분공시\" 등).",
        "  periodic(정기보고서)·exchange(거래소 수시공시)·major(주요사항보고서)·",
        "  holding(지분공시) 중에서.",
        "  **내용으로 추정하지 마라.** 소송·재무·이사회가 어디 실리는지 짐작해서",
        "  채우면 안 된다 — 실제로 유형을 좁혔다가 정답 문서를 놓친 사례가 있다.",
        "  확신이 없으면 빈 배열 [] 로 둔다",
        "- out_of_scope: 질문 시점이 2023.01~2026.03 밖이면 true",
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
        '회계연도로 해석", "out_of_scope": false}',
        "",
        "[예시 2 — 질문: \"2023년 매출 1위는? 그리고 삼성전자의 2023년 배당은?\"]",
        "(질문 둘 혼합 · 명시된 연도는 그대로 · 매출·배당·정정공시처럼 **내용**만",
        " 있고 공시 유형을 말하지 않았으므로 doc_groups 는 빈 배열이다)",
        '{"rewritten": "2023 회계연도 매출액이 가장 큰 회사와 삼성전자의 2023 '
        '회계연도 배당 내용을 묻는다", "sub_questions": ["2023 회계연도 매출액이 '
        '가장 큰 회사는 어디인가?", "삼성전자의 2023 회계연도 배당은 어떠했는가?"], '
        '"companies": ["삼성전자"], "fiscal_years": [2023], "doc_groups": [], '
        '"time_note": "", "out_of_scope": false}',
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
        # 기수 가드 — fiscal_years 에 1900 미만 값이 오면 그것은 연도가 아니라
        # 기수다(실측: "제50기" → fiscal_years=[50] → 검색 6회 전부 0건).
        # ki 로 옮기고 fiscal_years 에서 뺀다. 환산은 도구가 표로 한다.
        kis = list(a.get("ki") or [])
        years = []
        for y in (a.get("fiscal_years") or []):
            (kis if y < 1900 else years).append(y)
        a["fiscal_years"] = years
        a["ki"] = sorted(set(kis))
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
                         or a.get("time_note")):
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
        if a.get("time_note"):
            L.append(f"- 시점 해석: {a['time_note']}")
        if a.get("out_of_scope"):
            L.append("- 주의: 질문 시점이 코퍼스 범위(2023.01~2026.03) 밖일 수 있다")
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
