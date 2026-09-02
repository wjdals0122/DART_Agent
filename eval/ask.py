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
    ap.add_argument("--deadline", type=float, default=60.0)
    a = ap.parse_args()

    from src.agent import clova
    key, err = clova.get_api_key()      # 인덱스 30초 올리기 전에 키부터 본다
    if err:
        print(f"[중단] {err}", file=sys.stderr)
        return 2
    print(f"[key] {clova.mask_key(key)} · [model] {clova.resolve_model(a.model)}")

    from src.agent.loop import answer, warmup
    print("[warmup] 검색 인덱스 올리는 중…", flush=True)
    print(f"[warmup] {warmup():.1f}초 (답변 시간에 포함되지 않는다)")

    res = answer(a.question, model=a.model, max_tool_calls=a.max_tool_calls,
                 deadline=a.deadline, verbose=a.verbose)

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

    print(f"\n{BAR}\n소요 시간\n{BAR}")
    print(f"총 {res['elapsed']:.2f}초 · LLM {res['llm_seconds']:.2f}초 "
          f"· 도구 {res['tool_seconds']:.2f}초")
    if res.get("note"):
        print(f"note: {res['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
