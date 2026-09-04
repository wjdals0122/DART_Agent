"""Agent 도구 레지스트리와 safe_tool 래퍼.

【safe_tool 이 유일한 방어선이다】
평가 중 도구에서 예외가 나면 그 문항은 0점이다(CLAUDE.md 규칙 10).
LLM 은 스키마에 없는 인자를 지어내고, 문자열이어야 할 자리에 숫자를 넣고,
필수 인자를 빼먹는다. 그걸 전부 여기서 흡수한다.

  - 함수가 **kwargs 를 안 받으면 시그니처에 없는 키는 **버린다**
  - TypeError -> {"status":"error","note":"인자 오류: ..."}
  - 그 밖의 예외 -> traceback 은 stderr 로 남기고(규칙 9) 반환은 error dict
  - 반환이 dict 가 아니거나 필수 키가 없으면 채워서 모양을 맞춘다

  from src.tools import TOOLS
  TOOLS["calculate"](expression="1+1")        # 항상 dict
"""
from __future__ import annotations

import functools
import inspect
import sys
import traceback

from src.tools.calc import calculate as _calculate
from src.tools.listing import list_disclosures as _list_disclosures

BASE = ("status", "results", "sources", "note")
MAX_TOP_K = 10


def safe_tool(fn):
    """어떤 입력에도 예외를 던지지 않는 함수로 감싼다."""
    sig = inspect.signature(fn)
    takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values())
    names = {n for n, p in sig.parameters.items()
             if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}

    @functools.wraps(fn)
    def wrapper(**kw):
        dropped = []
        if not takes_kwargs:
            dropped = sorted(k for k in kw if k not in names)
            kw = {k: v for k, v in kw.items() if k in names}
        try:
            out = fn(**kw)
        except TypeError as e:
            out = {"status": "error", "note": f"인자 오류: {e}"}
        except Exception as e:                 # noqa: BLE001 — 여기가 마지막 방어선이다
            traceback.print_exc(file=sys.stderr)
            out = {"status": "error", "note": f"{type(e).__name__}: {e}"}

        if not isinstance(out, dict):
            out = {"status": "error", "note": f"도구가 dict 를 안 돌려줬다: {type(out).__name__}"}
        for k in BASE:
            out.setdefault(k, "" if k == "note" else ([] if k != "status" else "ok"))
        if dropped:
            out["note"] = (out["note"] + " · " if out["note"] else "") \
                + f"모르는 인자를 버렸다: {dropped}"
        return out

    wrapper.__wrapped_name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------- 검색 · 회사 해석


# query 가 비어 왔을 때 돌려줄 안내.
#
# 【기본 질의어를 지어내지 않는 이유】
# corp_name 만으로 '삼성전자' 같은 질의어를 만들면 검색은 되지만 무엇을 찾는지는
# 아무도 모른다. 그 결과로 답이 나가면 조용한 오염이다(CLAUDE.md 규칙 1).
# 대신 모델이 다시 부를 수 있게 무엇이 빠졌는지와 예시를 준다. 실측으로
# "query 가 비어 있다" 만 돌려줬을 때 모델은 재시도 없이 답변을 포기했다.
EMPTY_QUERY_NOTE = (
    "query 인자가 비어 있다. 무엇을 찾을지 검색어를 넣어 다시 호출하라. "
    "예: '연결 손익계산서 매출액', '사업의 개요 주요 제품'"
)


def _search_disclosure(query=None, corp_name=None, fiscal_year=None, rcept_no=None,
                       rcept_dt=None, date_from=None, date_to=None, doc_group=None,
                       report_type=None, top_k=5, include_superseded=False,
                       ki=None) -> dict:
    """기존 Retriever 를 그대로 부른다. 인덱스가 무거우므로 여기서 늦게 임포트한다."""
    from src.search.hybrid import get_searcher

    if query is None or not str(query).strip():
        return {"status": "error", "note": EMPTY_QUERY_NOTE}
    # 제N기(기수)는 도구가 표로 환산한다 — 모델이 짐작으로 환산해 틀린 실측 4건.
    # ki 와 fiscal_year 가 함께 오면 ki 를 우선한다. 환산 불가면 연도 필터 없이 간다.
    ki_note = ""
    if ki is not None and str(ki).strip() != "":
        from src.agent.context import fiscal_year_of_ki

        y, ki_note = fiscal_year_of_ki(corp_name, ki)
        fiscal_year = y                        # None 이면 연도 필터를 걸지 않는다
    if rcept_no:
        # 접수번호 검증은 list_disclosures 와 **같은 함수**를 쓴다.
        # 두 곳에서 따로 판단하면 안내가 갈리고, 모델은 갈린 안내를 못 고친다.
        from src.tools.listing import rcept_problem

        bad = rcept_problem(rcept_no)
        if bad:
            return {"status": "error", "note": bad, "results": [], "sources": []}
    try:
        k = max(1, min(int(top_k), MAX_TOP_K))
    except (TypeError, ValueError):
        k = 5
    res = get_searcher().search(str(query), corp_name=corp_name, fiscal_year=fiscal_year,
                                rcept_no=rcept_no, rcept_dt=rcept_dt, doc_group=doc_group,
                                rcept_dt_from=date_from, rcept_dt_to=date_to,
                                report_type=report_type,
                                top_k=k, include_superseded=bool(include_superseded))
    res.pop("pool", None)                      # LLM 에게 줄 것이 아니다
    res["sources"] = _sources(res.get("results") or [])
    _explain_empty(res, rcept_dt, fiscal_year)
    if ki_note:
        res["note"] = (ki_note + (" · " + res["note"] if res.get("note") else ""))
    return res


# rcept_dt + fiscal_year 는 어떤 문서에서는 **구조적으로** 0건이 된다.
# 실측(corpus/manifest.jsonl 4,204건):
#   periodic 1,054건 — 보고서명에 (YYYY.MM) 이 있어 fiscal_year 가 있다
#   exchange 1,469 · holding 1,083 · major 598 — **전부(100%) 없다**
# 세 유형은 report_nm 이 「단일판매ㆍ공급계약체결」 「주식등의대량보유상황보고서(일반)」
# 처럼 기간 표기가 없어 fiscal_year 가 -1 이고, 어떤 연도와도 맞지 않는다.
COMBO_NOTE = (
    "rcept_dt 와 fiscal_year 를 함께 걸었다. 거래소공시(단일판매ㆍ공급계약 체결/해지, "
    "신규시설투자, 투자판단 관련 주요경영사항)·주요사항보고서·지분공시(주식등의 "
    "대량보유상황보고서)는 보고서명에 기간 표기가 없어 fiscal_year 가 없으므로 "
    "이 조합은 항상 0건이 된다(코퍼스 4,204건 중 3,150건이 그렇다). "
    "둘 중 하나를 빼고 다시 조회하라"
)


def _explain_empty(res: dict, rcept_dt, fiscal_year) -> None:
    """필터가 0건을 낸 이유를 note 에 덧붙인다. 결과가 있으면 아무것도 안 한다.

    【왜】 조용한 0건은 모델이 고칠 수가 없다. 실측으로 회귀 7번이 두 번 연속
    `fiscal_year=2024` + `rcept_dt="20240130"` 을 넣어 후보 0건을 받고 그대로
    "확인되지 않습니다" 로 끝냈다. 도구 결과 옆에서 말해야 먹힌다(이 리포의 교훈).

    **후보가 0건일 때만** 붙인다. 후보가 있었는데 융합에서 걸러진 경우라면
    이 조합 때문이 아니므로 "항상 0건" 이 거짓말이 된다.
    """
    try:
        if res.get("results"):
            return
        if res.get("n_candidates"):            # 후보는 있었다 — 다른 이유다
            return
        if not (rcept_dt and fiscal_year is not None):
            return
        res["note"] = ((res.get("note") or "") + " · " + COMBO_NOTE).lstrip(" ·")
    except Exception:                          # noqa: BLE001 — 안내가 답을 막으면 안 된다
        import traceback
        traceback.print_exc(file=sys.stderr)


def _sources(hits) -> list:
    """근거 표기에 쓸 source 문자열. 답변의 '근거' 절이 이걸 그대로 옮겨 적는다."""
    from src.agent.context import fmt_source

    out = []
    for h in hits:
        try:
            lab = fmt_source(h.get("source") or {})
        except (KeyError, TypeError):
            continue
        if lab not in out:
            out.append(lab)
    return out


def _resolve_company(name=None) -> dict:
    from src.search.aliases import resolve_company

    if name is None or not str(name).strip():
        return {"status": "error", "note": "name 이 비어 있다"}
    r = dict(resolve_company(str(name)))
    r.setdefault("note", "")
    if r["status"] == "ok":
        r["note"] = r["note"] or f"{r['corp_name']}(종목코드 {r['stock_code']}) 로 확정"
    r["results"] = r.pop("candidates", [])
    return r


calculate = safe_tool(_calculate)
list_disclosures = safe_tool(_list_disclosures)
search_disclosure = safe_tool(_search_disclosure)
resolve_company = safe_tool(_resolve_company)

TOOLS = {
    "search_disclosure": search_disclosure,
    "list_disclosures": list_disclosures,
    "calculate": calculate,
    "resolve_company": resolve_company,
}

__all__ = ["TOOLS", "safe_tool", "calculate", "list_disclosures",
           "search_disclosure", "resolve_company"]
