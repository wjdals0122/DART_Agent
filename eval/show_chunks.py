"""질문 하나에 **실제로 어떤 청크가 나오는지** 눈으로 본다.

HIT/MISS 집계는 eval/probe_benchmark.py 가 한다. 여기는 본문을 그대로 찍는다.
하이브리드 검색기(BM25+dense RRF)를 그대로 태우므로 청크마다 bm25/dense 점수가 같이 나온다.

  python eval/show_chunks.py "연구개발비용 총계" --company 삼성전자 --fy 2025
  python eval/show_chunks.py --case 삼성전자 --scenario search_closed   # 골든셋 문항 + 정답 대조
  python eval/show_chunks.py                                          # 대화형 (모델 1회 로드)

모델 로드에 20~60초가 걸린다. 여러 질문을 볼 거면 대화형을 쓸 것.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    # 파이프로 한글을 넣으면 cp949 로 읽혀 깨진다. 콘솔 입력에는 영향이 없다.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

QDIR = ROOT / "Question" / "질문_(수정해서 다시 할 것)"
NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
SCENARIOS = ("search_closed", "search_open", "multi_closed", "multi_open",
             "complex_closed", "complex_open")


def load_case(company: str, scenario: str) -> dict | None:
    """골든셋에서 회사·시나리오로 문항 하나를 집는다. 없으면 None."""
    for p in sorted(glob.glob(str(QDIR / "benchmark_*AR*.json"))):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        if d["company"] != company:
            continue
        sc = d["scenarios"].get(scenario)
        if not sc:
            return None
        return {"company": d["company"],
                "fy": int(re.search(r"_(\d{4})AR", p).group(1)),
                "query": sc["query"], "gt": sc["ground_truth"],
                "gold_rcept": sorted(set(re.findall(r"\b\d{14}\b", str(d["doc_info"])))),
                "answer_nums": NUM.findall(sc["ground_truth"])}
    return None


def companies() -> list[str]:
    out = []
    for p in sorted(glob.glob(str(QDIR / "benchmark_*AR*.json"))):
        out.append(json.loads(Path(p).read_text(encoding="utf-8"))["company"])
    return sorted(set(out))


def show(hs, query, *, company=None, fy=None, group=None, k=5, chars=600,
         case=None) -> None:
    r = hs.search(query, corp_name=company, fiscal_year=fy, doc_group=group, top_k=k)
    f = f"회사={company or '-'} 사업연도={fy or '-'} 문서군={group or '-'}"
    print(f"\n{'=' * 100}\nQ  {query}\n   [{f}]")
    if case:
        print(f"정답  {case['gt'][:300]}")
        print(f"정답 문서  {', '.join(case['gold_rcept'])}")
    if r["status"] != "ok":
        print(f"\n  ** {r['status']} — {r.get('note', '')}")
        if r.get("candidates"):
            print(f"     후보: {r['candidates']}")
        return
    print(f"   후보 {r['n_candidates']:,}행 -> pool {r['n_pool']}건 "
          f"(정정 전 첨부 {r['n_preserved_dropped']}건 버림) · {r['elapsed_ms']}ms")

    nums = [n.replace(",", "") for n in (case or {}).get("answer_nums", [])]
    gold = set((case or {}).get("gold_rcept", []))
    for x in r["results"]:
        s = x["source"]
        body = x.get("text", "")
        marks = []
        if gold:
            marks.append("문서O" if s["rcept_no"] in gold else "문서X")
        if nums:
            found = [n for n in nums if n in body.replace(",", "")]
            marks.append(f"정답숫자 {len(found)}/{len(nums)}" if found else "정답숫자 없음")
        tag = ("  [" + " · ".join(marks) + "]") if marks else ""
        print(f"\n--- {x['rank']}. score {x['score']:.5f}  "
              f"bm25 {x['score_detail']['bm25']:.2f}  vec {x['score_detail']['vec']:.3f}{tag}")
        print(f"    {s['corp_name']} · {s['report_nm']} · 접수 {s['rcept_dt']} ({s['rcept_no']})")
        print(f"    {s['section']}")
        if s.get("note"):
            print(f"    ※ {s['note']}")
        shown = body if chars <= 0 else body[:chars]
        for line in shown.splitlines():
            print(f"      {line}")
        if chars > 0 and len(body) > chars:
            print(f"      … (총 {len(body):,}자, 전체는 --chars 0)")


HELP = """지시어
  /company 삼성전자   회사 필터        /company        해제
  /fy 2025           사업연도 필터     /fy             해제
  /group periodic    문서군 필터       /group          해제
  /k 8               상위 몇 건        /chars 0        본문 전체
  /case 삼성전자 search_closed        골든셋 문항 불러서 정답과 대조
  /companies         골든셋에 있는 회사 목록        /q  종료
그 밖의 입력은 질문으로 본다."""


def repl(hs, st: dict) -> None:
    print(HELP)
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        if line in ("/q", "/quit", "/exit"):
            return
        if line.startswith("/"):
            cmd, _, rest = line[1:].partition(" ")
            rest = rest.strip()
            if cmd == "company":
                st["company"] = rest or None
            elif cmd == "fy":
                st["fy"] = int(rest) if rest else None
            elif cmd == "group":
                st["group"] = rest or None
            elif cmd == "k":
                st["k"] = int(rest)
            elif cmd == "chars":
                st["chars"] = int(rest)
            elif cmd == "companies":
                print("  " + ", ".join(companies()))
                continue
            elif cmd == "case":
                parts = rest.split()
                if len(parts) != 2 or parts[1] not in SCENARIOS:
                    print("  /case <회사> <" + "|".join(SCENARIOS) + ">")
                    continue
                c = load_case(parts[0], parts[1])
                if c is None:
                    print("  그 회사·시나리오 문항이 골든셋에 없다. /companies 로 확인할 것")
                    continue
                show(hs, c["query"], company=c["company"], fy=c["fy"],
                     k=st["k"], chars=st["chars"], case=c)
                continue
            else:
                print(HELP)
                continue
            print(f"  회사={st['company'] or '-'} 사업연도={st['fy'] or '-'} "
                  f"문서군={st['group'] or '-'} k={st['k']} chars={st['chars']}")
            continue
        show(hs, line, company=st["company"], fy=st["fy"], group=st["group"],
             k=st["k"], chars=st["chars"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="질문. 없으면 대화형")
    ap.add_argument("--company", help="회사명 (별칭 사전으로 해석한다)")
    ap.add_argument("--fy", type=int, help="사업연도. 접수연도가 아니다")
    ap.add_argument("--group", help="문서군 (periodic / major / exchange ...)")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--chars", type=int, default=600, help="본문 표시 글자수. 0이면 전체")
    ap.add_argument("--case", help="골든셋 회사명 — 그 회사 문항을 질문 대신 쓴다")
    ap.add_argument("--scenario", default="search_closed", choices=SCENARIOS)
    ap.add_argument("--cpu", action="store_true", help="GPU 없이 (느리다)")
    args = ap.parse_args()

    case = None
    if args.case:
        case = load_case(args.case, args.scenario)
        if case is None:
            print(f"'{args.case}' 의 {args.scenario} 문항이 골든셋에 없다.\n"
                  f"  회사 목록: {', '.join(companies())}")
            return 2

    from src.search.hybrid import Retriever
    hs = Retriever(device="cpu" if args.cpu else "cuda")

    if case:
        show(hs, case["query"], company=case["company"], fy=case["fy"],
             k=args.k, chars=args.chars, case=case)
        return 0
    if args.query:
        show(hs, args.query, company=args.company, fy=args.fy, group=args.group,
             k=args.k, chars=args.chars)
        return 0
    repl(hs, {"company": args.company, "fy": args.fy, "group": args.group,
              "k": args.k, "chars": args.chars})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
