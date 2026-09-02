"""공시 목록·건수 도구. `corpus/manifest.jsonl` **만** 읽는다.

【왜 별도 도구인가】
"삼성전자가 2024년에 낸 정정공시가 몇 건인가", "이 보고서가 몇 번 정정됐나" 같은 질문은
본문 검색으로 풀면 안 된다. 검색은 top-k 만 돌려주므로 **세는 일에는 구조적으로 부적합**하다.
manifest 에 이미 다 있는 정보라 그냥 세면 된다.

【건수는 파이썬이 센다】
`by_subtype` 의 건수·비중을 여기서 계산해 돌려준다. LLM 에게 목록을 주고 세게 하면
반드시 틀린다. `limit` 으로 잘리는 건 `results` 뿐이고 `total`·`by_subtype` 은 전량 기준이다.

【정렬은 최신순이다 — 바꾸지 말 것】
`results` 는 접수일 **내림차순**이다. 이 도구를 부르는 가장 흔한 이유가
"가장 최신 보고서가 무엇인가" 라서, limit 에 잘리는 쪽은 반드시 오래된 것이어야 한다.
`series_of` 결과만 자른 뒤에 오름차순으로 되돌린다(정정 이력은 시간순이 읽기 좋다).

【사업연도는 periodic 에만 있다 — 실측】
  periodic  1,054건 · 사업연도 있음 100%
  exchange  1,469건 · 0%   (report_nm 이 '단일판매ㆍ공급계약체결' 처럼 (YYYY.MM) 을 안 단다)
  major       598건 · 0%
  holding   1,083건 · 0%
그래서 `doc_group="exchange" + fiscal_year=…` 같은 조합은 **구조적으로 항상 0건**이다.
e2e 에서 두산퓨얼셀이 정확히 그 조합으로 죽었다 — exchange 문서를 83건 갖고 있는데도.
이제 그런 조합은 not_found 가 아니라 **error 로 이유와 대안(date_from/date_to)** 을 준다.

【목록은 내용이 아니다】
e2e 에서 모델이 목록 34건을 받고 본문을 한 번도 안 열어 보고 답했다.
성공 응답의 note 에 "내용을 보려면 rcept_no 를 골라 search_disclosure 를 불러라" 를
매번 붙인다. 프롬프트에만 적어 두면 도구 결과가 쌓일수록 멀어져서 잊힌다.

【접수번호를 추측하는 것을 막는다】
e2e 40문항에서 모델이 접수번호를 **지어냈다**. 질문의 날짜에 000001 을 붙여
조립하거나(20230310000001), 13자리를 넣거나, 시스템 프롬프트 예시에 있던
남의 회사 번호를 그대로 베꼈다. 그때 도구는 "조건에 맞는 공시가 없다" 라고만
답해서 모델이 무엇을 잘못했는지 알 수 없었다.
이제 형식·존재·회사 일치를 각각 확인하고 **무엇을 어떻게 고칠지** 를 note 로 준다.

【사업연도 주의】
manifest 의 `base_year` 는 **접수 연도**다. 2024 사업연도 사업보고서는 2025년에 접수되므로
base_year=2025 다. 사업연도로 거르려면 report_nm 의 '(2024.12)' 를 봐야 한다.
그 로직은 `src.agent.context.fiscal_year_of` 에 이미 있으므로 그걸 쓴다.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from src.agent.context import fiscal_year_of
from src.search.build_latest_mask import base_report_nm

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "corpus" / "manifest.jsonl"
MAX_LIMIT = 500
RCEPT_LEN = 14
FIX_HINT = ("접수번호를 추측하지 말고 list_disclosures(corp_name=...) 로 "
            "먼저 목록을 조회해 실제 접수번호를 확인하라")
NEXT_HINT = ("이것은 문서 목록이며 내용이 아니다. 내용을 확인하려면 results 에서 "
             "rcept_no 를 골라 search_disclosure(rcept_no=..., query=...) 를 호출하라")

_DOCS = None
_SERIES_MAX = None
_BY_RCEPT = None
_FY_SPAN = None
_FY_GROUPS = None


def _load():
    """manifest 를 한 번만 읽는다. 계열별 최신 접수번호도 같이 만들어 둔다."""
    global _DOCS, _SERIES_MAX, _BY_RCEPT, _FY_SPAN, _FY_GROUPS
    if _DOCS is not None:
        return _DOCS, _SERIES_MAX
    docs = []
    with open(MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            d["_fy"] = fiscal_year_of(d.get("report_nm"))
            d["_base_nm"] = base_report_nm(d.get("report_nm"))
            d["_series"] = (str(d.get("corp_name")), d["_base_nm"])
            docs.append(d)
    smax: dict = {}
    for d in docs:
        k, rc = d["_series"], str(d.get("rcept_no") or "")
        if rc > smax.get(k, ""):
            smax[k] = rc
    _BY_RCEPT = {str(d.get("rcept_no") or ""): d for d in docs}
    # 어느 doc_group 이 사업연도를 갖는지 **실측**한다. 상수로 박으면 데이터가
    # 바뀌었을 때 안내가 거짓말이 된다.
    seen: dict = {}
    for d in docs:
        g = str(d.get("doc_group") or "")
        has = isinstance(d.get("_fy"), int) and d["_fy"] > 0
        seen[g] = seen.get(g, False) or has
    _FY_GROUPS = {g for g, has in seen.items() if has}
    # 코퍼스가 담고 있는 사업연도 범위. 상수로 박지 않고 manifest 에서 구한다 —
    # 데이터가 바뀌면 안내 문구도 같이 바뀌어야 한다.
    years = [d["_fy"] for d in docs if isinstance(d.get("_fy"), int) and d["_fy"] > 0]
    _FY_SPAN = (min(years), max(years)) if years else (None, None)
    _DOCS, _SERIES_MAX = docs, smax
    return _DOCS, _SERIES_MAX


def fiscal_span() -> tuple:
    """코퍼스가 담고 있는 사업연도 (최소, 최대)."""
    _load()
    return _FY_SPAN


def fy_groups() -> set:
    """사업연도를 갖는 doc_group 들. 실측값이다(periodic 뿐)."""
    _load()
    return set(_FY_GROUPS)


def group_counts(stock_code: str) -> dict:
    """그 회사가 doc_group 별로 몇 건을 갖고 있는지. 0건 안내에 쓴다."""
    docs, _ = _load()
    out: dict = {}
    for d in docs:
        if str(d.get("stock_code")) == str(stock_code):
            g = str(d.get("doc_group") or "")
            out[g] = out.get(g, 0) + 1
    return out


def rcept_problem(value):
    """접수번호가 쓸 수 없는 값이면 이유 문장을, 멀쩡하면 None 을 돌려준다.

    search_disclosure 도 같은 검사를 쓴다 — 한 곳에서만 판단해야 안내가 갈리지 않는다.
    """
    _load()
    rc = str(value).strip()
    if not (rc.isdigit() and len(rc) == RCEPT_LEN):
        return (f"접수번호 {rc!r} 는 형식이 틀렸다 — {RCEPT_LEN}자리 숫자여야 한다. "
                f"{FIX_HINT}")
    if rc not in _BY_RCEPT:
        return f"접수번호 {rc} 이 코퍼스에 없다. {FIX_HINT}"
    return None


def _row(d, smax) -> dict:
    rc = str(d.get("rcept_no") or "")
    return {"rcept_no": rc, "rcept_dt": str(d.get("rcept_dt") or ""),
            "report_nm": str(d.get("report_nm") or ""),
            "doc_group": str(d.get("doc_group") or ""),
            "doc_subtype": str(d.get("doc_subtype") or ""),
            "is_correction": bool(d.get("is_correction")),
            "is_latest_in_series": rc == smax.get(d["_series"], "")}


def _ratios(counter: Counter, total: int) -> list:
    """비중의 합이 정확히 1.0 이 되게 맞춘다. 반올림 잔돈은 가장 큰 항목이 흡수한다."""
    items = counter.most_common()
    if not items or not total:
        return []
    out = [{"doc_subtype": k, "n": n, "ratio": round(n / total, 4)} for k, n in items]
    out[0]["ratio"] = round(out[0]["ratio"] + (1.0 - sum(x["ratio"] for x in out)), 4)
    return out


def list_disclosures(corp_name=None, doc_group=None, fiscal_year=None,
                     date_from=None, date_to=None, corrections_only=False,
                     series_of=None, limit=50, **_ignored) -> dict:
    """조건에 맞는 공시 목록과 건수·유형별 비중. 개별 공시 '내용' 은 돌려주지 않는다."""
    docs, smax = _load()
    out = {"status": "ok", "total": 0, "by_subtype": [],
           "by_correction": {"원본": 0, "기재정정": 0},
           "results": [], "sources": [], "note": ""}
    notes = []

    sel = docs
    if series_of:
        rc = str(series_of).strip()
        bad = rcept_problem(rc)                          # (c) 형식·존재
        if bad:
            out["status"] = "error"
            out["note"] = bad
            return out
        anchor = _BY_RCEPT[rc]
        if corp_name:                                    # (a) 앵커 회사가 다른가
            from src.search.aliases import resolve_company
            r = resolve_company(corp_name)
            if r["status"] == "ok" and \
                    str(anchor.get("stock_code")) != str(r["stock_code"]):
                out["status"] = "error"
                out["note"] = (
                    f"series_of={rc} 는 {anchor.get('corp_name')} 의 문서다. "
                    f"{r['corp_name']} 의 질문에는 쓸 수 없다. "
                    f"먼저 list_disclosures(corp_name={r['corp_name']!r}) 로 "
                    f"목록을 조회해 실제 접수번호를 확인하라")
                return out
        sel = [d for d in docs if d["_series"] == anchor["_series"]]
        notes.append(f"계열 기준: {anchor.get('corp_name')} · {anchor['_base_nm']}")
        if anchor.get("doc_group") != "periodic":
            notes.append("이벤트 공시는 제목이 같아도 매번 다른 사건이라 계열로 보기 어렵다")
    else:
        if corp_name:
            from src.search.aliases import resolve_company
            r = resolve_company(corp_name)
            if r["status"] != "ok":
                out["status"] = r["status"]
                out["note"] = r.get("note") or f"'{corp_name}' 을 회사 하나로 특정하지 못했다"
                if r.get("candidates"):
                    out["results"] = r["candidates"]
                return out
            code = str(r["stock_code"])
            sel = [d for d in sel if str(d.get("stock_code")) == code]
            notes.append(f"회사: {r['corp_name']}({code})")
        if doc_group:
            want = {doc_group} if isinstance(doc_group, str) else set(doc_group)
            # (a) 사업연도가 없는 doc_group 에 fiscal_year 를 걸면 언제나 0건이다.
            #     not_found 로 넘기면 모델이 이유를 모른 채 포기한다.
            if fiscal_year is not None and not (want & fy_groups()):
                ok = ", ".join(sorted(fy_groups())) or "없음"
                out["status"] = "error"
                out["note"] = (
                    f"{'/'.join(sorted(want))}(거래소 수시공시·주요사항보고서 등) 문서는 "
                    f"사업연도가 없다. fiscal_year 를 빼고 date_from/date_to 로 기간을 "
                    f"지정하라. fiscal_year 를 쓸 수 있는 것은 {ok} 뿐이다")
                return out
            # (b) 그 회사가 그 종류를 아예 안 갖고 있으면 보유 현황을 알려준다
            if corp_name and not any(d.get("doc_group") in want for d in sel):
                have = group_counts(code)
                out["status"] = "not_found"
                out["note"] = (
                    f"{r['corp_name']} 은 {'/'.join(sorted(want))} 문서가 없다. "
                    f"보유 현황: "
                    + " / ".join(f"{g} {n}" for g, n in sorted(have.items()))
                    + ". 다른 doc_group 으로 다시 시도하라")
                return out
            sel = [d for d in sel if d.get("doc_group") in want]
        if fiscal_year is not None:
            try:
                years = {int(fiscal_year)}
            except (TypeError, ValueError):
                years = {int(y) for y in fiscal_year}
            lo, hi = fiscal_span()                       # (b) 코퍼스 범위 밖인가
            if lo is not None and years and all(not (lo <= y <= hi) for y in years):
                out["status"] = "not_found"
                out["note"] = (f"코퍼스는 {lo}~{hi}년 공시만 담고 있다. "
                               f"fiscal_year={fiscal_year} 은 범위 밖이다")
                return out
            sel = [d for d in sel if d["_fy"] in years]
        f, t = _ymd(date_from), _ymd(date_to)
        if f:
            sel = [d for d in sel if str(d.get("rcept_dt") or "") >= f]
        if t:
            sel = [d for d in sel if str(d.get("rcept_dt") or "") <= t]
        if corrections_only:
            sel = [d for d in sel if d.get("is_correction")]
            notes.append("정정공시만")

    # 최신순(내림차순)으로 정렬한다. limit 로 자를 때 **최신이 남아야** 한다.
    # 오름차순으로 자르면 "최근 보고서" 를 묻는 질문에서 최신 문서가 통째로 사라진다.
    # 실측: LG이노텍 periodic 17건 · limit=10 → 2025.05 이후 7건이 전부 잘려 나가고,
    # 모델은 남은 것 중 가장 최신인 2024 사업보고서를 "최근" 으로 골랐다.
    sel = sorted(sel, key=lambda d: (str(d.get("rcept_dt") or ""),
                                     str(d.get("rcept_no") or "")), reverse=True)
    total = len(sel)
    out["total"] = total
    out["by_subtype"] = _ratios(Counter(str(d.get("doc_subtype") or "") for d in sel), total)
    n_corr = sum(1 for d in sel if d.get("is_correction"))
    out["by_correction"] = {"원본": total - n_corr, "기재정정": n_corr}

    try:
        lim = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        lim = 50
        notes.append(f"limit 을 읽을 수 없어 {lim} 으로 둔다")
    rows = [_row(d, smax) for d in sel[:lim]]
    if series_of:
        # 정정 이력은 시간순으로 읽는 것이 자연스럽다. **자른 뒤에** 되돌리므로
        # limit 에 걸려도 최신본이 사라지지 않는다.
        rows.reverse()
    out["results"] = rows
    if total > lim:
        notes.append(f"{total}건 중 최신 {lim}건만 results 에 담았다 "
                     f"(total·by_subtype 은 전량 기준)")
    if total == 0:
        out["status"] = "not_found"
        notes.append("조건에 맞는 공시가 없다")
    else:
        notes.append(NEXT_HINT)          # 목록은 내용이 아니다 — 매번 붙인다
    out["note"] = " · ".join(notes)
    return out


def _ymd(v):
    if v is None:
        return None
    s = "".join(ch for ch in str(v) if ch.isdigit())
    return s or None
