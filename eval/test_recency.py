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

    print(f"\n{BAR}\n7 — 접수번호·연도 방어 (e2e 1차에서 모델이 번호를 지어냈다)\n{BAR}")
    r = TOOLS["list_disclosures"](corp_name="두산로보틱스", series_of="20260619000667")
    print(f"   note={r['note']}")
    check("남의 회사 접수번호는 error", r["status"] == "error" and "KB금융" in r["note"])
    r = TOOLS["list_disclosures"](corp_name="OCI홀딩스", fiscal_year=2017)
    print(f"   note={r['note']}")
    check("코퍼스 범위 밖 연도는 not_found + 범위 안내",
          r["status"] == "not_found" and "범위 밖" in r["note"])
    r = TOOLS["list_disclosures"](corp_name="HD현대중공업", series_of="20230310000001")
    check("없는 접수번호는 error + 목록 조회 안내",
          r["status"] == "error" and "추측하지 말고" in r["note"])
    r = TOOLS["list_disclosures"](corp_name="HD현대일렉트릭", series_of="2024013000001")
    check("14자리가 아니면 형식 오류를 밝힌다",
          r["status"] == "error" and "형식이 틀렸다" in r["note"])
    r = TOOLS["search_disclosure"](query="매출", corp_name="삼성전자",
                                   rcept_no="20240101999999")
    check("search_disclosure 의 rcept_no 도 같은 검사를 받는다",
          r["status"] == "error" and "코퍼스에 없다" in r["note"])
    r = TOOLS["list_disclosures"](corp_name="KB금융", series_of="20260619000667")
    check("자기 회사 계열은 그대로 동작한다(회귀)",
          r["status"] == "ok" and r["total"] == 3)

    print(f"\n{BAR}\n7-b — doc_group · 목록→본문 안내\n{BAR}")
    r = TOOLS["list_disclosures"](corp_name="두산퓨얼셀", doc_group="exchange",
                                  fiscal_year=2024)
    print(f"   note={r['note']}")
    check("사업연도 없는 doc_group + fiscal_year 는 error",
          r["status"] == "error" and "사업연도가 없다" in r["note"])
    check("대안(date_from/date_to)을 알려준다", "date_from" in r["note"])
    r = TOOLS["list_disclosures"](corp_name="미래에셋증권", doc_group="exchange")
    print(f"   note={r['note']}")
    check("그 종류가 0건이면 보유 현황을 알려준다",
          r["status"] == "not_found" and "보유 현황" in r["note"]
          and "periodic 19" in r["note"])
    r = TOOLS["list_disclosures"](corp_name="두산퓨얼셀", doc_group="exchange",
                                  date_from="20250101", date_to="20251231", limit=3)
    check("기간으로 좁히면 exchange 도 나온다", r["status"] == "ok" and r["total"] > 0,
          f"total={r['total']}")
    check("성공 응답에 '내용을 보려면 search_disclosure' 안내가 붙는다",
          "search_disclosure(rcept_no=" in r["note"])
    r = TOOLS["list_disclosures"](corp_name="LG이노텍", doc_group="periodic",
                                  fiscal_year=2025, limit=3)
    check("periodic + fiscal_year 는 그대로 동작한다(회귀)",
          r["status"] == "ok" and r["total"] == 5, f"total={r['total']}")

    print(f"\n{BAR}\n8 — 프롬프트에 베낄 수 있는 실제 접수번호가 없다\n{BAR}")
    import re as _re

    pat = _re.compile(r"\d{14}")
    for name in ("system.txt", "system_min.txt", "planner.txt"):
        f = ROOT / "prompts" / name
        hits = pat.findall(f.read_text(encoding="utf-8")) if f.is_file() else []
        check(f"{name} 에 14자리 접수번호 없음", not hits, str(hits))
    tj = (ROOT / "config" / "tools.json").read_text(encoding="utf-8")
    check("tools.json 에 14자리 접수번호 없음", not pat.findall(tj))

    print(f"\n{BAR}")
    if _FAILED:
        print(f"실패 {len(_FAILED)}건: {_FAILED}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
