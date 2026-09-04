"""Agent 에게 질문 하나를 던져 본다.

  python eval/ask.py "SK하이닉스 2024년 연결 매출액이 얼마야?"
  python eval/ask.py "..." --verbose      # LLM 에 들어간 프롬프트 전문까지
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BAR = "=" * 78


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent 한 문항 실행")
    ap.add_argument("question")
    ap.add_argument("--verbose", action="store_true", help="프롬프트 전문도 출력")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-tool-calls", type=int, default=6)
    # 기본은 loop.DEADLINE(200초). 값을 두 군데서 관리하지 않는다
    ap.add_argument("--deadline", type=float, default=None)
    ap.add_argument("--qid", default=None,
                    help="토큰 로그에 남길 문항 이름. 없으면 문항 단위 집계에서 빠진다")
    ap.add_argument("--max-cost", type=float, default=None,
                    help="추정 비용 상한(원). 넘으면 그 자리에서 멈춘다")
    a = ap.parse_args()

    from src.agent import clova
    from src.agent import usage as U

    # 모든 LLM 호출을 eval/token_log.jsonl 한 곳에 모은다(eval/token_log.py --summary)
    U.set_context(script="ask", qid=a.qid)
    if a.max_cost:
        U.use_budget(U.Budget(limit_won=a.max_cost))

    key, err = clova.get_api_key()      # 인덱스 30초 올리기 전에 키부터 본다
    if err:
        print(f"[중단] {err}", file=sys.stderr)
        return 2
    print(f"[key] {clova.mask_key(key)} · [model] {clova.resolve_model(a.model)}")

    from src.agent.loop import DEADLINE, answer, warmup
    if a.deadline is None:
        a.deadline = DEADLINE
    print("[warmup] 검색 인덱스 올리는 중…", flush=True)
    print(f"[warmup] {warmup():.1f}초 (답변 시간에 포함되지 않는다)")

    res = answer(a.question, model=a.model, max_tool_calls=a.max_tool_calls,
                 deadline=a.deadline, verbose=a.verbose, qid=a.qid)

    if a.verbose and res.get("messages"):
        print(f"\n{BAR}\nLLM 에 들어간 프롬프트 전문\n{BAR}")
        for m in res["messages"]:
            print(f"\n----- {m['role']}"
                  + (f" · {m.get('name')}" if m.get("name") else "") + " -----")
            if m.get("toolCalls"):
                for c in m["toolCalls"]:
                    f = c.get("function") or {}
                    print(f"  [toolCall] {f.get('name')}({f.get('arguments')})")
            if m.get("content"):
                print(m["content"])

    print(f"\n{BAR}\n질문\n{BAR}\n{a.question}")

    print(f"\n{BAR}\n도구 호출 {len(res['tool_calls'])}회\n{BAR}")
    if not res["tool_calls"]:
        print("(없음)")
    for c in res["tool_calls"]:
        print(f"[{c['n']}] {c['name']}({c['arguments']})")
        print(f"    → status={c['status']} · {c['summary']}"
              + (f" · {c['note'][:90]}" if c["note"] else ""))

    print(f"\n{BAR}\n답변\n{BAR}\n{res['answer']}")

    if res["sources"]:
        print(f"\n{BAR}\n도구가 돌려준 출처 {len(res['sources'])}건\n{BAR}")
        for s in res["sources"]:
            print(f"  {s}")

    u, w = res.get("usage") or {}, res.get("waste") or {}
    print(f"\n{BAR}\n토큰·비용 (추정)\n{BAR}")
    pt, ct = u.get("prompt", 0), u.get("completion", 0)
    print(f"입력 {pt:,} + 출력 {ct:,} = {pt + ct:,} 토큰 · LLM {u.get('llm_calls', 0)}회")
    print(f"입력 {U.fmt_won(pt * U.WON_PER_PROMPT_TOKEN)} + "
          f"출력 {U.fmt_won(ct * U.WON_PER_COMPLETION_TOKEN)} = "
          f"**{U.fmt_won(U.won(pt, ct))}** 추정")
    print(U.price_line())
    if u.get("unknown_calls"):
        print(f"토큰을 알 수 없는 호출 {u['unknown_calls']}건 — 위 수치는 하한이다")
    waste = [f"재호출 차단 {w.get('repeat_blocked', 0)}",
             f"error 뒤 재시도 {w.get('retry_after_bad', 0)}",
             f"429 재시도 {w.get('retry_429', 0)}"]
    if w.get("hit_call_limit"):
        waste.append("도구 6회 한도 도달")
    print("낭비 신호: " + " · ".join(waste))

    print(f"\n{BAR}\n소요 시간\n{BAR}")
    print(f"총 {res['elapsed']:.2f}초 · LLM {res['llm_seconds']:.2f}초 "
          f"· 도구 {res['tool_seconds']:.2f}초")
    if res.get("note"):
        print(f"note: {res['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
