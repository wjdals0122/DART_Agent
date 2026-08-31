"""검색기 -> 컨텍스트 조립 -> HyperCLOVA X 를 한 번에 훑어보는 CLI.

Agent 가 아니다. function calling 도 붙이지 않는다. 확인하려는 건 하나다 —
**표가 든 청크를 그대로 넣어주면 LLM 이 숫자를 제대로 뽑아 오는가.**
검색 인자는 사람이 CLI 로 직접 준다.

  python eval/quick_qa.py --q "..." --corp "LG이노텍" --rcept-dt 20250813 --top-k 5
  python eval/quick_qa.py --q "..." --corp CJ제일제당 --fy 2023 --dry-run

--dry-run 은 LLM 을 부르지 않고 검색 결과와 프롬프트 전문만 찍는다 (크레딧 절약).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.agent import clova                       # noqa: E402
from src.agent.context import build_context       # noqa: E402

SYSTEM_PATH = ROOT / "prompts" / "system_min.txt"
BAR = "=" * 78


def build_user_message(question: str, context: str) -> str:
    return (f"[참고 자료]\n{context}\n\n"
            f"[질문]\n{question}")


def print_hits(res: dict) -> None:
    print(BAR)
    print("1. 검색 결과 top-k")
    print(BAR)
    f = res.get("filters") or {}
    active = " · ".join(f"{k}={v}" for k, v in f.items() if v not in (None, "", False))
    print(f"질의: {res.get('query')}")
    print(f"필터: {active or '없음'}")
    print(f"후보 {res.get('n_candidates', 0):,}건 → pool {res.get('n_pool', 0)}건 "
          f"(preserved 드롭 {res.get('n_preserved_dropped', 0)}건) · "
          f"status={res.get('status')}")
    if res.get("note"):
        print(f"note: {res['note']}")
    print()
    for h in res.get("results") or []:
        src = h.get("source") or {}
        d = h.get("score_detail") or {}
        print(f"[{h.get('rank')}] chunk_id={h.get('chunk_id')}  "
              f"rrf={h.get('score')}  vec={d.get('vec')}  bm25={d.get('bm25')}")
        print(f"     receipt_no={src.get('rcept_no')} ({src.get('rcept_dt')}) · "
              f"{src.get('corp_name')} {src.get('report_nm')} · "
              f"fiscal_year={src.get('fiscal_year')} · preserved={h.get('preserved')}")
        print(f"     section_path: {src.get('section')}")
        if src.get("note"):
            print(f"     note: {src['note']}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True, help="질문")
    ap.add_argument("--corp", default=None, help="회사명 (검색어가 아니라 필터다)")
    ap.add_argument("--rcept-dt", default=None, help="접수일 YYYYMMDD")
    ap.add_argument("--rcept-no", default=None, help="접수번호 14자리")
    ap.add_argument("--doc-group", default=None,
                    help="periodic / exchange / major / holding")
    ap.add_argument("--fy", type=int, default=None, help="사업연도 (report_nm 의 (YYYY.MM))")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 1·2 만 출력")
    ap.add_argument("--model", default=None,
                    help="비우면 CLOVA_MODEL 환경변수, 그것도 없으면 "
                         + clova.FALLBACK_MODEL)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--device", default="cuda", help="cuda / cpu")
    a = ap.parse_args()

    # 키 검사를 검색보다 먼저 한다. 인덱스를 30초 올려놓고 키가 없어 죽으면 낭비다.
    if not a.dry_run:
        key, err = clova.get_api_key()
        if err:
            print(f"[중단] {err}", file=sys.stderr)
            return 2
        print(f"[key] {clova.mask_key(key)} · [model] {clova.resolve_model(a.model)}")

    if not SYSTEM_PATH.exists():
        print(f"[중단] 시스템 프롬프트가 없다: {SYSTEM_PATH}", file=sys.stderr)
        return 2
    system = SYSTEM_PATH.read_text(encoding="utf-8").strip()

    from src.search.hybrid import Retriever      # 로드가 무거워 여기서 임포트

    rt = Retriever(device=a.device)

    t0 = time.time()
    res = rt.search(a.q, corp_name=a.corp, rcept_dt=a.rcept_dt, rcept_no=a.rcept_no,
                    doc_group=a.doc_group, fiscal_year=a.fy, top_k=a.top_k)
    search_s = time.time() - t0
    print_hits(res)

    if not (res.get("results")):
        print(f"[검색 {search_s:.2f}초] 결과가 없어 LLM 을 부르지 않는다.")
        return 1

    try:
        context = build_context(res)
    except ValueError as e:
        print(f"[중단] 컨텍스트 조립 실패 — {e}", file=sys.stderr)
        return 3

    user = build_user_message(a.q, context)
    print(BAR)
    print("2. LLM 에 실제로 들어가는 프롬프트 전문")
    print(BAR)
    print("----- system -----")
    print(system)
    print("\n----- user -----")
    print(user)
    print(f"\n(system {len(system):,}자 · user {len(user):,}자)")
    print()

    if a.dry_run:
        print(f"[검색 {search_s:.2f}초] --dry-run 이라 LLM 은 부르지 않았다.")
        return 0

    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    t1 = time.time()
    out = clova.chat(messages, max_tokens=a.max_tokens, temperature=a.temperature,
                     model=a.model)
    llm_s = time.time() - t1

    print(BAR)
    print("3. LLM 응답 전문")
    print(BAR)
    if out["ok"]:
        print(out["text"])
        print(f"\n(model={out.get('model')} · finishReason={out.get('finish_reason')} "
              f"· usage={out.get('usage')})")
    else:
        print(f"[실패] status={out.get('status')} · {out.get('error')}")

    print()
    print(BAR)
    print("4. 소요 시간")
    print(BAR)
    print(f"검색 {search_s:.2f}초 · LLM {llm_s:.2f}초 · 합계 {search_s + llm_s:.2f}초")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
