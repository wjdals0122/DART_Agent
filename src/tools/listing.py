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

_DOCS = None
_SERIES_MAX = None


def _load():
    """manifest 를 한 번만 읽는다. 계열별 최신 접수번호도 같이 만들어 둔다."""
    global _DOCS, _SERIES_MAX
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
    _DOCS, _SERIES_MAX = docs, smax
    return _DOCS, _SERIES_MAX


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
        anchor = next((d for d in docs if str(d.get("rcept_no")) == rc), None)
        if anchor is None:
            out["status"] = "not_found"
            out["note"] = f"접수번호 {rc} 인 공시가 코퍼스에 없다"
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
            sel = [d for d in sel if d.get("doc_group") in want]
        if fiscal_year is not None:
            try:
                years = {int(fiscal_year)}
            except (TypeError, ValueError):
                years = {int(y) for y in fiscal_year}
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
    out["note"] = " · ".join(notes)
    return out


def _ymd(v):
    if v is None:
        return None
    s = "".join(ch for ch in str(v) if ch.isdigit())
    return s or None
