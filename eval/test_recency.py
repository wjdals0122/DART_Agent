"""'최근' 질문에서 나온 문제 4건의 회귀 테스트. LLM 은 한 번도 부르지 않는다.

  python eval/test_recency.py

【무엇을 지키는 테스트인가】
1. list_disclosures 가 **최신순**으로 준다
   — 오름차순으로 자르던 시절 LG이노텍 periodic 17건 · limit=10 에서 2025.05 이후
     7건이 통째로 잘려 나갔고, 모델은 남은 것 중 가장 최신인 2024 사업보고서를
     "최근" 으로 골랐다
2. series_of 결과는 자른 뒤 시간순으로 되돌린다 (정정 이력은 시간순이 읽기 좋다)
3. rcept_no 를 넣으면 그 문서 청크만 나온다
   — fiscal_year 만으로는 같은 사업연도의 분기·반기가 섞인다
4. report_type(annual/half/quarter) 이 동작하고, **안 넣으면 종전과 같다**
5. query 누락은 error + 재시도 안내 (기본 질의어를 지어내지 않는다)
6. 회귀: 삼성전자 손익계산서가 여전히 1위
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

from src.tools import TOOLS

BAR = "=" * 78
_FAILED: list = []


def check(name: str, cond, detail: str = "") -> None:
    if not cond:
        _FAILED.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def sec(h) -> str:
    return h["source"]["section"].split(" > ", 1)[-1][:60]


def main() -> int:
    print(f"{BAR}\n1 — list_disclosures 최신순 (LG이노텍 periodic limit=10)\n{BAR}")
    r = TOOLS["list_disclosures"](corp_name="LG이노텍", doc_group="periodic", limit=10)
    for x in r["results"]:
        print(f"   {x['rcept_no']}  {x['rcept_dt']}  {x['report_nm']}"
              f"  latest_in_series={x['is_latest_in_series']}")
    print(f"   status={r['status']} total={r['total']}\n   note={r['note']}")
    rc = [x["rcept_no"] for x in r["results"]]
    check("최신 분기보고서 20260514001513 이 잘리지 않았다", "20260514001513" in rc)
    check("최신 사업보고서 20260312001270 이 잘리지 않았다", "20260312001270" in rc)
    check("접수일 내림차순", rc == sorted(rc, reverse=True))
    check("total 은 전량 기준", r["total"] == 17, f"total={r['total']}")

    print(f"\n{BAR}\n2 — series_of 정정 이력 (KB금융 20260619000667)\n{BAR}")
    r = TOOLS["list_disclosures"](corp_name="KB금융", series_of="20260619000667")
    for x in r["results"]:
        print(f"   {x['rcept_no']}  {x['rcept_dt']}  {x['report_nm']}"
              f"  correction={x['is_correction']}  latest_in_series={x['is_latest_in_series']}")
    print(f"   status={r['status']} total={r['total']}\n   note={r['note']}")
    rc = [x["rcept_no"] for x in r["results"]]
    check("계열 3건 전부", r["total"] == 3 and len(rc) == 3, f"total={r['total']}")
    check("최종본 20260619000667 포함", "20260619000667" in rc)
    check("시간순(오름차순)으로 되돌렸다", rc == sorted(rc))

    print(f"\n{BAR}\n3 — rcept_no 로 문서 특정\n{BAR}")
    r = TOOLS["search_disclosure"](query="사업의 개요 주요 제품 광학솔루션",
                                   corp_name="LG이노텍", rcept_no="20260312001270")
    for h in r["results"]:
        print(f"   {h['rank']}. {h['source']['rcept_no']} {h['source']['report_nm']} | {sec(h)}")
    print(f"   status={r['status']} 후보={r.get('n_candidates')}")
    got = {h["source"]["rcept_no"] for h in r["results"]}
    check("그 문서 청크만 나온다", got == {"20260312001270"}, str(got))

    print(f"\n{BAR}\n4 — report_type\n{BAR}")
    q = "사업의 개요 주요 제품 광학솔루션 기판소재 전장부품"
    r = TOOLS["search_disclosure"](query=q, corp_name="LG이노텍",
                                   fiscal_year=2025, report_type="annual")
    for h in r["results"]:
        print(f"   {h['rank']}. {h['source']['rcept_no']} {h['source']['report_nm']} | {sec(h)}")
    nm = {h["source"]["report_nm"] for h in r["results"]}
    check("annual → 사업보고서만", bool(nm) and all("사업보고서" in x for x in nm), str(nm))

    r0 = TOOLS["search_disclosure"](query=q, corp_name="LG이노텍", fiscal_year=2025)
    nm0 = [h["source"]["report_nm"] for h in r0["results"]]
    print(f"   (report_type 없음) {nm0}")
    check("안 넣으면 종전과 같다(분기·반기가 나온다)",
          any("분기" in x or "반기" in x for x in nm0))

    rk = TOOLS["search_disclosure"](query="매출액", corp_name="LG이노텍",
                                    report_type="사업보고서")
    nmk = {h["source"]["report_nm"] for h in rk["results"]}
    check("한국어 별칭도 받는다", bool(nmk) and all("사업보고서" in x for x in nmk), str(nmk))

    rb = TOOLS["search_disclosure"](query="매출액", corp_name="LG이노텍",
                                    report_type="연간보고서")
    print(f"   (모르는 값) status={rb['status']}\n   note={rb['note']}")
    check("모르는 값을 조용히 넘기지 않는다", "연간보고서" in (rb["note"] or ""))

    print(f"\n{BAR}\n5 — query 누락\n{BAR}")
    r = TOOLS["search_disclosure"](corp_name="LG이노텍")
    print(f"   status={r['status']}\n   note={r['note']}")
    check("status=error", r["status"] == "error")
    check("재시도 안내가 들어 있다", "다시 호출" in r["note"] and "예:" in r["note"])

    print(f"\n{BAR}\n6 — 회귀 (삼성전자 fiscal_year=2024 연결 손익계산서 매출액)\n{BAR}")
    r = TOOLS["search_disclosure"](query="연결 손익계산서 매출액",
                                   corp_name="삼성전자", fiscal_year=2024)
    for h in r["results"]:
        print(f"   {h['rank']}. {h['source']['rcept_no']} {h['source']['report_nm']} | {sec(h)}")
    rcs = [h["source"]["rcept_no"] for h in r["results"]]
    check("20250311001085 이 상위에 있다", "20250311001085" in rcs,
          f"{rcs.index('20250311001085') + 1}위" if "20250311001085" in rcs else str(rcs))

    print(f"\n{BAR}")
    if _FAILED:
        print(f"실패 {len(_FAILED)}건: {_FAILED}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
