"""Agent 루프가 어떤 경우에도 규격 dict 를 돌려주는지 확인한다. LLM 을 부르지 않는다.

  python eval/test_loop.py

`clova.chat` 과 도구를 가짜로 갈아끼워 실패 상황을 만든다.
평가 중 예외가 나면 그 문항은 0점이므로, 여기서 안 터지는 것이 전부다.
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

from src.agent import clova, loop  # noqa: E402
from src.agent.loop import answer  # noqa: E402

FAIL = []
SPEC = ("answer", "sources", "tool_calls", "elapsed")


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def spec_ok(r):
    return (isinstance(r, dict) and all(k in r for k in SPEC)
            and isinstance(r["answer"], str) and r["answer"].strip()
            and isinstance(r["sources"], list) and isinstance(r["tool_calls"], list)
            and isinstance(r["elapsed"], float))


def fake_call(i, name="search_disclosure", args=None):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": args if args is not None
                         else {"query": "매출액", "corp_name": "삼성전자"}}}


def main() -> int:
    real_chat, real_tools = clova.chat, dict(__import__("src.tools", fromlist=["TOOLS"]).TOOLS)

    # ---------- 4. clova.chat 이 예외를 던진다
    print("\n--- 4. LLM 이 예외를 던지는 경우 ---")
    def boom(*a, **kw):
        raise RuntimeError("네트워크가 끊겼다")
    clova.chat = boom
    r = answer("삼성전자 매출액은?")
    check("규격 dict 반환", spec_ok(r), f"answer={r['answer'][:50]!r}")
    check("실패 사유가 note 에 남는다", "RuntimeError" in (r.get("note") or ""), r.get("note"))
    clova.chat = real_chat

    # ---------- 5. 도구가 전부 not_found
    print("\n--- 5. 도구가 전부 not_found 를 돌려주는 경우 ---")
    import src.tools as T
    calls = {"n": 0}

    def chat_two_step(messages, **kw):
        """도구를 한 번 부르고, 도구 없이 부르면 최종 답을 준다."""
        if kw.get("extra"):
            calls["n"] += 1
            return {"ok": True, "text": "",
                    "tool_calls": [fake_call(calls["n"],
                                             args={"query": f"검색어{calls['n']}"})],
                    "finish_reason": "tool_calls", "usage": {}}
        return {"ok": True, "text": "공시에서 확인되지 않음", "tool_calls": [],
                "finish_reason": "stop", "usage": {}}

    T.TOOLS = {k: (lambda **kw: {"status": "not_found", "results": [], "sources": [],
                                 "note": "조건에 맞는 문서가 없다"})
               for k in real_tools}
    loop.__dict__.setdefault("_", None)
    clova.chat = chat_two_step
    r = answer("있지도 않은 회사의 매출액은?", max_tool_calls=3)
    check("규격 dict 반환", spec_ok(r), f"도구 {len(r['tool_calls'])}회")
    check("not_found 가 기록된다",
          all(c["status"] == "not_found" for c in r["tool_calls"]) and r["tool_calls"],
          str([c["status"] for c in r["tool_calls"]]))
    T.TOOLS = real_tools

    # ---------- 6. 도구를 7회 부르려 하면 6회에서 끊긴다
    print("\n--- 6. 도구 호출 7회 강제 ---")
    seq = {"n": 0}

    def chat_always_tool(messages, **kw):
        if kw.get("extra"):
            seq["n"] += 1
            return {"ok": True, "text": "",
                    "tool_calls": [fake_call(seq["n"], args={"query": f"q{seq['n']}"})],
                    "finish_reason": "tool_calls", "usage": {}}
        return {"ok": True, "text": "지금까지의 근거로 답한다: 확인되지 않음",
                "tool_calls": [], "finish_reason": "stop", "usage": {}}

    hits = {"n": 0}

    def counting_tool(**kw):
        hits["n"] += 1
        return {"status": "ok", "results": [], "sources": [], "note": f"{hits['n']}번째"}

    T.TOOLS = {k: counting_tool for k in real_tools}
    clova.chat = chat_always_tool
    r = answer("무한히 도구를 부르는 질문", max_tool_calls=6)
    check("규격 dict 반환", spec_ok(r), f"answer={r['answer'][:40]!r}")
    check("도구 호출 6회에서 끊긴다", len(r["tool_calls"]) == 6, str(len(r["tool_calls"])))
    check("실제 도구 실행도 6회 이하", hits["n"] <= 6, f"{hits['n']}회 실행")
    check("한도 도달이 note 에 남는다", "한도" in (r.get("note") or ""), r.get("note"))
    T.TOOLS = real_tools
    clova.chat = real_chat

    # ---------- 덤: 같은 인자 재호출은 캐시
    print("\n--- 덤. 같은 인자 재호출 차단 ---")
    seq2 = {"n": 0}

    def chat_same_tool(messages, **kw):
        if kw.get("extra"):
            seq2["n"] += 1
            return {"ok": True, "text": "",
                    "tool_calls": [fake_call(seq2["n"], args={"query": "같은질문"})],
                    "finish_reason": "tool_calls", "usage": {}}
        return {"ok": True, "text": "끝", "tool_calls": [], "finish_reason": "stop",
                "usage": {}}

    ran = {"n": 0}

    def once_tool(**kw):
        ran["n"] += 1
        return {"status": "ok", "results": [], "sources": [], "note": "처음"}

    T.TOOLS = {k: once_tool for k in real_tools}
    clova.chat = chat_same_tool
    r = answer("같은 걸 계속 묻는다", max_tool_calls=4)
    check("도구는 한 번만 실제로 돈다", ran["n"] == 1, f"{ran['n']}회 실행")
    check("나머지는 재호출로 끊긴다",
          sum(1 for c in r["tool_calls"]
              if c["status"] == "error" and "이미 같은 인자" in c["note"]) >= 1,
          str([(c["status"], c["note"][:16]) for c in r["tool_calls"]]))
    T.TOOLS = real_tools
    clova.chat = real_chat

    # ---------- 덤2: 모르는 도구 이름
    print("\n--- 덤2. LLM 이 없는 도구를 부르는 경우 ---")
    def chat_bad_name(messages, **kw):
        if kw.get("extra"):
            return {"ok": True, "text": "",
                    "tool_calls": [fake_call(1, name="get_stock_price")],
                    "finish_reason": "tool_calls", "usage": {}}
        return {"ok": True, "text": "확인되지 않음", "tool_calls": [],
                "finish_reason": "stop", "usage": {}}
    clova.chat = chat_bad_name
    r = answer("주가 알려줘", max_tool_calls=2)
    check("모르는 도구는 error 로 흡수", spec_ok(r)
          and any(c["status"] == "error" for c in r["tool_calls"]),
          str([(c["name"], c["status"]) for c in r["tool_calls"]]))
    clova.chat = real_chat

    print()
    print("실패 없음" if not FAIL else "실패: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
