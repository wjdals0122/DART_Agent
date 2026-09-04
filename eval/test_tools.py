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

    # ki(기수) → 회계연도 환산 (step39 · 실측: 제50기를 fiscal_year=50 으로 검색)
    print("\n--- ki(기수) 환산 ---")
    from src.agent.context import fiscal_year_of_ki
    y, note = fiscal_year_of_ki("OCI홀딩스", 50)
    check("  OCI홀딩스 제50기 → 연도 환산", isinstance(y, int) and 2023 <= y <= 2026,
          f"{y} · {note}")
    check("  성공 note 문구", "회계연도로 환산했다" in note, note)
    y2, note2 = fiscal_year_of_ki("OCI홀딩스", 999)
    check("  표에 없는 기수 → None + 환산 불가", y2 is None and "환산 불가" in note2, note2)
    y3, note3 = fiscal_year_of_ki("없는회사명", 10)
    check("  미상 회사 → None + 환산 불가", y3 is None and "환산 불가" in note3, note3)
    y4, note4 = fiscal_year_of_ki("삼성전자", "abc")
    check("  정수 아님 → None + 예외 없음", y4 is None, note4)

    hybrid.get_searcher = lambda: spy
    try:
        out = TOOLS["search_disclosure"](query="매출액", corp_name="OCI홀딩스", ki=50)
        check("  ki → fiscal_year 로 환산돼 전달", spy.called.get("fiscal_year") == y,
              str(spy.called.get("fiscal_year")))
        check("  note 에 환산 결과", "회계연도로 환산했다" in (out.get("note") or ""),
              out.get("note"))
        out2 = TOOLS["search_disclosure"](query="매출액", corp_name="OCI홀딩스",
                                          ki=999, fiscal_year=2024)
        check("  ki 가 fiscal_year 보다 우선(환산 불가면 연도 필터 없음)",
              spy.called.get("fiscal_year") is None, str(spy.called.get("fiscal_year")))
        check("  환산 불가 note", "환산 불가" in (out2.get("note") or ""), out2.get("note"))
    finally:
        hybrid.get_searcher = real_get_searcher
    lst = TOOLS["list_disclosures"](corp_name="OCI홀딩스", ki=50)
    check("  list_disclosures 도 ki 환산", "회계연도로 환산했다" in (lst.get("note") or ""),
          (lst.get("note") or "")[:60])
    check("  list 결과가 그 연도 정기공시", lst["status"] == "ok" and lst["total"] >= 1,
          f"total={lst.get('total')}")

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

    # 자리표시자 접수번호 — 프롬프트 예시를 그대로 넣은 경우
    #
    # e2e 2번에서 모델이 list_disclosures(series_of="20230310XXXXXX") 를 불렀다.
    # 질문의 날짜에 예시의 XXXXXX 를 이어 붙인 것이다. 옛 안내("형식이 틀렸다")로는
    # 모델이 자기가 예시를 베꼈다는 걸 못 알아챘다.
    print("\n--- 자리표시자 접수번호 ---")
    from src.tools.listing import looks_placeholder  # noqa: E402

    for v in ("20230310XXXXXX", "20260619XXXXXX", "00000000000000",
              "11111111111111", "xxxxxxxxxxxxxx"):
        check(f"  자리표시자로 잡는다 — {v}", looks_placeholder(v))
    for v in ("20240319000684", "20240930000001", "20250520000006"):
        check(f"  진짜 접수번호는 안 잡는다 — {v}", not looks_placeholder(v))

    # 코퍼스 전량에 오탐이 없는지. 진짜 접수번호에 0 이 여섯 개 연속인 것이 있다.
    man = ROOT / "corpus" / "manifest.jsonl"
    if man.is_file():
        real = [str(json.loads(l)["rcept_no"])
                for l in man.read_text(encoding="utf-8").splitlines() if l.strip()]
        wrong = [r for r in real if looks_placeholder(r)]
        check(f"  코퍼스 {len(real):,}건에 오탐 없음", not wrong, str(wrong[:5]))

    for tool, key in (("list_disclosures", "series_of"),
                      ("search_disclosure", "rcept_no")):
        kw = {"corp_name": "HD현대중공업", key: "20230310XXXXXX"}
        if tool == "search_disclosure":
            kw["query"] = "MGC 2척"
        out = TOOLS[tool](**kw)
        check(f"  {tool}({key}=자리표시자) status=error",
              out.get("status") == "error", str(out.get("status")))
        check(f"  {tool} 안내가 '자리표시자' 라고 말한다",
              "자리표시자" in (out.get("note") or ""), str(out.get("note"))[:80])
        check(f"  {tool} 대안을 말한다",
              "회사명과 날짜" in (out.get("note") or ""))

    # query 없이 부른 경우의 안내 — 문구가 조용히 빈약해지지 않게 못박는다
    #
    # 이 note 는 한 번 강화한 적이 있다(주석 참고: "query 가 비어 있다" 만 돌려줬을 때
    # 모델이 재시도 없이 포기했다). 그 뒤로도 회귀 3번이 query 없이 부르고
    # 재호출 대신 답변을 끝낸 사례가 있어(step27 변형 ③), 문구를 검사로 고정한다.
    print("\n--- query 누락 안내 ---")
    from src.tools import EMPTY_QUERY_NOTE  # noqa: E402

    for kw in ({}, {"query": None}, {"query": ""}, {"query": "   "},
               {"corp_name": "KB금융", "fiscal_year": 2025}):
        out = TOOLS["search_disclosure"](**kw)
        check(f"  {str(kw)[:38]} → error", out.get("status") == "error",
              str(out.get("status")))
        check(f"  {str(kw)[:38]} → 안내 문구", out.get("note") == EMPTY_QUERY_NOTE)
    check("  무엇이 빠졌는지 말한다", "query" in EMPTY_QUERY_NOTE)
    check("  다시 호출하라고 말한다", "다시 호출" in EMPTY_QUERY_NOTE)
    check("  검색어를 넣으라고 말한다", "검색어를 넣" in EMPTY_QUERY_NOTE)
    check("  예시를 준다", "예:" in EMPTY_QUERY_NOTE and "'" in EMPTY_QUERY_NOTE,
          EMPTY_QUERY_NOTE)

    # rcept_dt + fiscal_year — 어떤 문서에서는 구조적으로 0건이다
    #
    # 회귀 7번이 두 번 연속 fiscal_year=2024 + rcept_dt="20240130" 을 넣어 후보 0건을
    # 받고 그대로 "확인되지 않습니다" 로 끝냈다. 조용한 0건은 모델이 못 고친다.
    print("\n--- rcept_dt + fiscal_year 0건 안내 ---")
    from src.tools import COMBO_NOTE, _explain_empty  # noqa: E402

    def probe(res, dt, fy):
        r = dict(res)
        _explain_empty(r, dt, fy)
        return COMBO_NOTE in (r.get("note") or "")

    EMPTY = {"status": "not_found", "results": [], "n_candidates": 0,
             "note": "필터 조건에 맞는 문서가 없다"}
    check("  후보 0건 + 두 인자 → 안내가 붙는다", probe(EMPTY, "20240130", 2024))
    r = dict(EMPTY)
    _explain_empty(r, "20240130", 2024)
    check("  status 는 not_found 그대로", r["status"] == "not_found", r["status"])
    check("  기존 note 를 지우지 않는다", "필터 조건에 맞는 문서가 없다" in r["note"])

    check("  후보가 있었으면 안 붙는다",
          not probe({**EMPTY, "n_candidates": 2405,
                     "note": "융합 후 남은 결과가 없다"}, "20240130", 2024))
    check("  결과가 있으면 안 붙는다",
          not probe({"status": "ok", "results": [{"x": 1}], "n_candidates": 10,
                     "note": ""}, "20240130", 2024))
    for dt, fy in (("20240130", None), (None, 2024), (None, None)):
        check(f"  rcept_dt={dt} fiscal_year={fy} → 안 붙는다", not probe(EMPTY, dt, fy))
    for args in ((None, None, None), ({}, "x", 1), ({"note": None}, "1", 1)):
        try:
            _explain_empty(*args)
            ok = True
        except Exception:                      # noqa: BLE001
            ok = False
        check(f"  _explain_empty{str(args)[:34]} 예외 없음", ok)

    # note 에 적은 문서 유형이 실측과 맞는지 — 추측이면 안 된다
    man = ROOT / "corpus" / "manifest.jsonl"
    if man.is_file():
        import re as _re

        fy_pat = _re.compile(r"\((\d{4})[.\-/]\d{1,2}\)")
        no_fy = {}
        tot = 0
        for line in man.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            tot += 1
            if not fy_pat.search(str(d.get("report_nm") or "")):
                g = str(d.get("doc_group"))
                no_fy[g] = no_fy.get(g, 0) + 1
        check("  fiscal_year 없는 유형 = exchange·major·holding",
              set(no_fy) == {"exchange", "major", "holding"}, str(sorted(no_fy)))
        check(f"  그 합계 {sum(no_fy.values()):,} 가 note 의 3,150 과 같다",
              sum(no_fy.values()) == 3150, str(sum(no_fy.values())))
        check(f"  전체 {tot:,} 가 note 의 4,204 와 같다", tot == 4204, str(tot))

    # corp_name 이 회사가 아니라 '전체' 류 낱말인 경우 — 해결책 안내
    #
    # 실측(2026-09-04): 모델이 corp_name='전체' 로 검색해 0건을 두 번 받고 답을
    # 포기했다. note 가 "없다" 만 말하면 필터를 뺄 생각을 못 한다.
    # 오타·미수록 회사에는 붙으면 안 된다 — 그때는 회사를 특정하는 게 맞다.
    print("\n--- corp_name='전체' 류 해결책 안내 ---")
    HINT_KEY = "corp_name 을 빼고"
    generic = ["전체", "전부", "모든", "모두", "전 종목", "상장사", "기업",
               "all", "미지정"]
    for g in generic:
        r = TOOLS["resolve_company"](name=g)
        check(f"  '{g}' → not_found + 안내가 붙는다",
              r["status"] == "not_found" and HINT_KEY in r["note"],
              f"{r['status']} · {r['note'][:60]}")
    # '회사' 는 못 찾은 값이 아니다 — 신한지주 별칭("…지주회사")에 부분 일치로
    # 흡수된다(실측). not_found 가 아니므로 이 안내의 대상이 아니다.
    r = TOOLS["resolve_company"](name="회사")
    check("  '회사' 는 not_found 가 아니다(부분 일치 흡수 — 안내 대상 밖)",
          r["status"] != "not_found", f"{r['status']} · {r.get('corp_name')}")
    for bad_name in ("삼성전제", "구글코리아", "한국조선해양"):
        r = TOOLS["resolve_company"](name=bad_name)
        check(f"  오타·미수록 '{bad_name}' 에는 안 붙는다",
              r["status"] == "not_found" and HINT_KEY not in r["note"],
              f"{r['status']} · {r['note'][:60]}")
    # 기존 note 를 지우지 않고 뒤에 덧붙인다
    r = TOOLS["resolve_company"](name="전체")
    check("  기존 note('코퍼스 70개사에 없다')가 앞에 남아 있다",
          r["note"].startswith("코퍼스 70개사에 없다"), r["note"][:60])
    check("  값이 따옴표로 들어간다", "'전체'" in r["note"], r["note"][:80])
    # 조합·표기 변형도 잡는다 (normalize 가 공백·대소문자를 지운다)
    from src.search.aliases import generic_name_hint
    for combo in ("모든 회사", "전체 기업", "ALL"):
        check(f"  조합 '{combo}' 도 잡는다", bool(generic_name_hint(combo)), combo)
    check("  실제 회사명에는 빈 문자열", generic_name_hint("삼성전자") == "")
    check("  일부만 겹치면 안 붙는다 ('국내 상장사')",
          generic_name_hint("국내 상장사") == "")

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
