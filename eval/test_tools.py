"""도구 계층 검증. 핵심은 하나 — **어떤 입력에도 예외가 나면 안 된다.**

  python eval/test_tools.py            # 검색기를 안 올리는 것만
  python eval/test_tools.py --search   # search_disclosure 까지 (인덱스 30초)

평가 중 도구에서 예외가 나면 그 문항은 0점이다(CLAUDE.md 규칙 10).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from src.tools import TOOLS  # noqa: E402

FAIL = []
BASE = ("status", "results", "sources", "note")


def check(name, cond, detail="") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def shape_ok(out) -> bool:
    return isinstance(out, dict) and all(k in out for k in BASE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", action="store_true", help="search_disclosure 까지 검증")
    a = ap.parse_args()

    schema = json.loads((ROOT / "config" / "tools.json").read_text(encoding="utf-8"))
    search_schema = next(t["function"] for t in schema["tools"]
                         if t["function"]["name"] == "search_disclosure")
    props = search_schema["parameters"]["properties"]
    check("search_disclosure date_from 스키마", "date_from" in props)
    check("search_disclosure date_to 스키마", "date_to" in props)

    # 실제 인덱스를 올리지 않고 공개 인자→Retriever 인자 전달만 검증한다.
    import src.search.hybrid as hybrid
    real_get_searcher = hybrid.get_searcher

    class SpySearcher:
        called = {}

        def search(self, query, **kw):
            self.called = {"query": query, **kw}
            return {"status": "ok", "results": [], "pool": [], "note": ""}

    spy = SpySearcher()
    hybrid.get_searcher = lambda: spy
    try:
        out = TOOLS["search_disclosure"](
            query="계약금액", corp_name="삼성전자",
            date_from="20240130", date_to="20250225")
    finally:
        hybrid.get_searcher = real_get_searcher
    check("date_from → rcept_dt_from 전달",
          spy.called.get("rcept_dt_from") == "20240130", str(spy.called))
    check("date_to → rcept_dt_to 전달",
          spy.called.get("rcept_dt_to") == "20250225", str(spy.called))
    check("날짜 범위 호출 모양 정상", shape_ok(out), str(out))

    names = list(TOOLS)
    if not a.search:
        names = [n for n in names if n != "search_disclosure"]

    # 1. 빈 인자
    print("\n--- 1. 모든 도구를 빈 인자로 ---")
    for n in names:
        out = TOOLS[n]()
        check(f"{n}() 예외 없음 · 모양 정상", shape_ok(out), f"status={out.get('status')}")

    # 2. 잡탕 인자 (LLM 이 지어낼 법한 것들)
    print("\n--- 2. 잡탕 인자 ---")
    junk = {"expression": None, "corp_name": 12345, "fiscal_year": "이천이십사",
            "limit": "많이", "query": ["리스트", "가", "왔다"], "없는키": {"a": 1},
            "top_k": -3, "series_of": 20260619000667}
    for n in names:
        out = TOOLS[n](**junk)
        check(f"{n}(잡탕) 예외 없음", shape_ok(out),
              f"status={out.get('status')} · {str(out.get('note'))[:70]}")

    # 3~5. calculate
    print("\n--- 3~5. calculate ---")
    out = TOOLS["calculate"](expression="(300870903-258935494)/258935494*100")
    check("증감률 계산", out["status"] == "ok" and round(out["value"], 1) == 16.2,
          f"{out.get('value')}")
    out = TOOLS["calculate"](expression="__import__('os').system('ls')")
    check("코드 실행 시도 거절", out["status"] == "error", out["note"])
    t0 = time.time()
    out = TOOLS["calculate"](expression="2**999999999")
    el = time.time() - t0
    check("거듭제곱 폭탄 거절 · 즉시", out["status"] == "error" and el < 1.0,
          f"{el:.3f}초 · {out['note']}")
    for e, why in [("1/0", "0 나누기"), ("x+1", "변수"), ("'a'*3", "문자열"),
                   ("1e300*1e300", "무한대"), ("[1,2][0]", "첨자")]:
        o = TOOLS["calculate"](expression=e)
        check(f"거절: {why} ({e})", o["status"] == "error", o["note"])
    o = TOOLS["calculate"](expression="3,477,640 - 2,554,111")
    check("콤마 있는 숫자", o["status"] == "ok" and o["value"] == 923529, str(o.get("value")))

    # 6. 정정공시만
    print("\n--- 6. list_disclosures(정정공시만) ---")
    out = TOOLS["list_disclosures"](corp_name="삼성전자", fiscal_year=2024,
                                    corrections_only=True)
    # 삼성전자의 정정공시 9건은 전부 holding/major 라 report_nm 에 '(YYYY.MM)' 이 없다.
    # fiscal_year 는 report_nm 에서 뽑으므로(접수 연도가 아니다) 0건이 나오는 게 맞다.
    check("삼성전자·2024·정정만 → 0건 (사업연도가 붙는 정기보고서 중엔 정정이 없다)",
          out["status"] == "not_found" and out["total"] == 0, f"total={out['total']}")
    out = TOOLS["list_disclosures"](corp_name="삼성전자", corrections_only=True)
    check("삼성전자 정정공시 전체 9건", out["total"] == 9, f"total={out['total']}")
    check("전부 정정공시", all(r["is_correction"] for r in out["results"]),
          f"{len(out['results'])}건")
    for r in out["results"][:4]:
        print(f"      {r['rcept_dt']} {r['rcept_no']} {r['doc_group']:<9} "
              f"{r['report_nm'][:44]}")

    # 7. 계열 추적
    print("\n--- 7. list_disclosures(KB금융, series_of=20260619000667) ---")
    out = TOOLS["list_disclosures"](corp_name="KB금융", series_of="20260619000667")
    ids = [r["rcept_no"] for r in out["results"]]
    dts = [r["rcept_dt"] for r in out["results"]]
    check("계열 조회 성공", out["status"] == "ok", f"total={out['total']}")
    # 이 계열에는 원본 1 + 정정 2 = 3건이 있다(manifest 로 직접 확인).
    # 지시서에는 2건으로 적혀 있었으나 20260324000835 정정본이 하나 더 존재한다.
    check("20260313001191 과 20260619000667 이 둘 다 있다",
          {"20260313001191", "20260619000667"} <= set(ids), str(ids))
    check("접수일 오름차순", dts == sorted(dts), str(dts))
    check("마지막 것만 is_latest_in_series=True",
          [r["is_latest_in_series"] for r in out["results"]]
          == [False] * (len(ids) - 1) + [True],
          str([r["is_latest_in_series"] for r in out["results"]]))
    for r in out["results"]:
        print(f"      {r['rcept_dt']} {r['rcept_no']} {r['report_nm']} "
              f"· 최신={r['is_latest_in_series']}")

    # 9. ratio 합
    print("\n--- 9. by_subtype 비중 합 ---")
    for kw in ({"corp_name": "삼성전자"}, {"corp_name": "KB금융", "doc_group": "periodic"},
               {"fiscal_year": 2024}):
        o = TOOLS["list_disclosures"](**kw, limit=5)
        s = sum(x["ratio"] for x in o["by_subtype"])
        check(f"ratio 합 = 1.0 {kw}", abs(s - 1.0) < 1e-9,
              f"합={s} · total={o['total']} · 구간 {len(o['by_subtype'])}개")
    o = TOOLS["list_disclosures"](corp_name="삼성전자", limit=3)
    check("limit 은 results 만 자른다", len(o["results"]) == 3 and o["total"] > 3,
          f"results={len(o['results'])} total={o['total']}")

    # 8. 검색
    if a.search:
        print("\n--- 8. search_disclosure ---")
        out = TOOLS["search_disclosure"](query="연결 손익계산서 매출액",
                                         corp_name="삼성전자", fiscal_year=2024)
        check("검색 성공", out["status"] == "ok" and out["results"],
              f"{len(out.get('results') or [])}건 · 후보 {out.get('n_candidates')}")
        check("sources 채워짐", bool(out["sources"]), str(out["sources"][:1]))
        check("pool 은 안 돌려준다", "pool" not in out)
        check("top_k 상한 10", len(TOOLS["search_disclosure"](
            query="매출액", corp_name="삼성전자", top_k=99)["results"]) <= 10)
        for h in (out["results"] or [])[:3]:
            print(f"      [{h['rank']}] {h['source']['report_nm']} · "
                  f"{h['source']['section'][:60]}")

    # tools.json 이 스키마로서 말이 되는지
    print("\n--- tools.json ---")
    cfg = json.loads((ROOT / "config" / "tools.json").read_text(encoding="utf-8"))
    defined = {t["function"]["name"] for t in cfg["tools"]}
    check("tools.json 의 도구 = 레지스트리", defined == set(TOOLS), str(defined ^ set(TOOLS)))
    for t in cfg["tools"]:
        f = t["function"]
        check(f"  {f['name']} description 충분", len(f["description"]) > 150,
              f"{len(f['description'])}자")

    print()
    print("실패 없음" if not FAIL else "실패: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
