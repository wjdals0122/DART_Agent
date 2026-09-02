"""1-C 검증 — 하이브리드 검색기. [E] 1~9.

  python eval/test_retriever.py

구현보다 먼저 쓰고, 구현 후 다시 돌린다.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

from src.search.aliases import all_companies
from src.search.hybrid import Retriever

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}", flush=True)
    if not ok:
        FAIL.append(name)
    return ok


def main() -> int:
    rt = Retriever()
    print()

    # ---------------------------------------------------------------- E1
    # 검색 품질보다 필터가 붙었는지가 먼저다.
    r1 = rt.search("연결 기준 매출액", corp_name="삼성전자", base_year=2024, top_k=5)
    check("E1. 삼성전자·base_year=2024 후보 > 0", r1["n_candidates"] > 0,
          f"n_candidates={r1['n_candidates']:,} status={r1['status']} "
          f"결과 {len(r1['results'])}건 pool {len(r1['pool'])}건 {r1.get('elapsed_ms')}ms")

    # ---------------------------------------------------------------- E2
    # 70개사 전수 — stock_code/corp_code 혼동을 잡는 검사. 반드시 돈다.
    zero = []
    for c in all_companies():
        r = rt.search("매출액", corp_name=c["corp_name"], top_k=1)
        if r["n_candidates"] == 0:
            zero.append(c["corp_name"])
    check("E2. 70개사 전수 n_candidates > 0", not zero,
          f"검사 {len(all_companies())}개사 · 0건 회사 {len(zero)}곳 {zero}")

    # ---------------------------------------------------------------- E3
    bad = [x["source"]["corp_name"] for x in r1["results"]
           if x["source"]["corp_name"] != "삼성전자"]
    check("E3. 결과 전부 삼성전자 (타사 혼입 0건)", not bad, f"타사 {bad}")

    # ---------------------------------------------------------------- E4
    docs = {x["doc_id"] for x in r1["results"]}
    shas = [rt.embed_sha1[x["row"]] for x in r1["results"]]
    check("E4. doc_id 2종 이상 · embed_sha1 중복 0",
          len(docs) >= 2 and len(shas) == len(set(shas)),
          f"doc {len(docs)}종 / 결과 {len(shas)}건 / 고유 sha {len(set(shas))}")

    # ---------------------------------------------------------------- E5
    r5 = rt.search("영업이익", corp_name="LG이노텍", rcept_dt="20250813", top_k=10)
    got5 = sorted({x["source"]["rcept_no"] for x in r5["results"]})
    check("E5. rcept_dt 필터 — receipt_no 전부 20250813 로 시작",
          bool(got5) and all(v.startswith("20250813") for v in got5),
          f"n_candidates={r5['n_candidates']:,} receipt_no={got5}")

    # ---------------------------------------------------------------- E6
    # CJ제일제당 사업보고서(2024.12) 계열.
    # 주의: 이 문서의 접수일은 2025-03-17 이라 base_year 는 2025 다.
    #       사업연도로 좁히려면 fiscal_year 를 쓴다. 아래에 둘 다 찍는다.
    ORIG = "20250317000648"
    a = rt.search("매출액", corp_name="CJ제일제당", fiscal_year=2024, top_k=10)
    b = rt.search("감사의견 독립된 감사인", corp_name="CJ제일제당", fiscal_year=2024, top_k=10)
    ra = sorted({x["source"]["rcept_no"] for x in a["results"]})
    rb = sorted({x["source"]["rcept_no"] for x in b["results"]})
    check(f"E6a. 매출액 질의에 원본 {ORIG} 없음", ORIG not in ra,
          f"receipt_no={ra} (preserved 드롭 {a['n_preserved_dropped']}건)")
    check(f"E6b. 감사의견 질의에 원본 {ORIG} 있음", ORIG in rb,
          f"receipt_no={rb} (preserved 드롭 {b['n_preserved_dropped']}건)")
    ab = rt.search("매출액", corp_name="CJ제일제당", base_year=2024, top_k=10)
    print(f"       (참고) base_year=2024 로 걸면 후보 {ab['n_candidates']:,}행, "
          f"receipt_no={sorted({x['source']['rcept_no'] for x in ab['results']})}")

    # ---------------------------------------------------------------- E7
    c = rt.search("매출액", corp_name="CJ제일제당", fiscal_year=2024, top_k=10,
                  include_superseded=True)
    rc = sorted({x["source"]["rcept_no"] for x in c["results"]})
    check(f"E7. include_superseded=True 면 {ORIG} 가 나타남", ORIG in rc,
          f"후보 {c['n_candidates']:,}행 receipt_no={rc}")

    # ---------------------------------------------------------------- E8
    r = rt.search("매출", corp_name="삼성전자", base_year=2099)
    check("E8a. base_year=2099 → not_found", r["status"] == "not_found", r["note"])
    r = rt.search("매출", corp_name="없는회사")
    check("E8b. 없는회사 → not_found", r["status"] == "not_found", r["note"])
    r = rt.search("매출", corp_name="SK")
    check("E8c. SK → ambiguous + candidates",
          r["status"] == "ambiguous" and len(r.get("candidates", [])) >= 2,
          ", ".join(c_["corp_name"] for c_ in r.get("candidates", [])))

    # ---------------------------------------------------------------- E9
    for label, kw in (("회사 지정", {"corp_name": "삼성전자", "base_year": 2024}),
                      ("광역(필터 없음)", {})):
        rt.search("연결 기준 매출액", top_k=5, **kw)          # 워밍업
        ts = [rt.search("연결 기준 매출액", top_k=5, **kw)["elapsed_ms"] for _ in range(5)]
        check(f"E9. {label} 지연 ≤ 300ms", max(ts) <= 300,
              f"평균 {np.mean(ts):.0f}ms · 최대 {max(ts):.0f}ms")

    # ---------------------------------------------------------------- 참고 출력
    print("\n=== [G] E1 질의 top-5 ===")
    for x in r1["results"]:
        s = x["source"]
        d = x["score_detail"]
        print(f"\n{x['rank']}. {x['chunk_id']}  score={x['score']:.5f} "
              f"vec={d['vec']:.4f} bm25={d['bm25']:.4f}")
        print(f"   {s['corp_name']} · {s['report_nm']} · 접수 {s['rcept_dt']}({s['rcept_no']})")
        print(f"   {s['section'][:150]}")
        print(f"   {x['text'][:150].replace(chr(10), ' / ')}")

    print()
    if FAIL:
        print(f"실패 {len(FAIL)}건: {FAIL}")
        return 1
    print("전 항목 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
