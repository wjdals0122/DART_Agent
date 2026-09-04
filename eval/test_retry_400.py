"""HTTP 400 재시도 검증. **LLM 을 한 번도 부르지 않는다** (requests.post 를 갈아끼운다).

  python eval/test_retry_400.py

【왜】 HCX 가 `40009 Unsupported function` 을 산발적으로 낸다. 2026-09-03 하루에
세 번 봤다(e2e_v2 1건 · e2e_v4 1건 · OCI 검증 1건). **같은 질문을 그대로 다시 부르니
정상이었다.** 첫 호출에서 400 이 나면 그 문항을 통째로 잃는다 —
40문항에 1건이면 200문항에서 5건이다.

【확인하는 세 경로】
  1차 재시도에서 성공 / 2차(도구 제거) 재시도에서 성공 / 끝까지 실패해도 규격 dict

【429·5xx 경로는 건드리지 않았다】
그쪽 백오프(delay·n_retry·n_429)와 400 상태(n_400·tools_dropped)를 분리했다.
여기서 429 도 함께 시험해 그대로인지 본다.
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

import os                                                  # noqa: E402

# 400 재시도(루프 역학)만 시험한다 — 재작성 단계가 켜지면 턴 수가 밀린다
os.environ["DART_REWRITE"] = "0"

from src.agent import clova                                # noqa: E402
from src.agent import usage as usage_mod                   # noqa: E402

usage_mod.set_enabled(False)                               # 비용 로그를 더럽히지 않는다

FAIL = []
BODY_400 = '{"status":{"code":"40009","message":"Unsupported function"},"result":null}'
TOOLS = [{"type": "function",
          "function": {"name": "search_disclosure", "description": "x",
                       "parameters": {"type": "object", "properties": {}}}}]


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


class Resp:
    def __init__(self, code, body):
        self.status_code = code
        self.text = body
        self._body = body

    def json(self):
        import json
        return json.loads(self._body)


OK_BODY = ('{"status":{"code":"20000","message":"OK"},'
           '"result":{"message":{"role":"assistant","content":"답변이다"},'
           '"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":2}}}')


def run(codes, capture=None):
    """codes 순서대로 응답을 돌려주는 가짜 requests.post 로 chat() 을 부른다."""
    seq = list(codes)
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append({k: v for k, v in (json or {}).items()})
        code = seq.pop(0) if seq else 200
        return Resp(code, BODY_400 if code == 400 else OK_BODY)

    real_post, real_sleep = clova.requests.post, clova.time.sleep
    clova.requests.post = fake_post
    clova.time.sleep = lambda s: None                      # 시험은 기다리지 않는다
    try:
        r = clova.chat([{"role": "user", "content": "질문"}],
                       extra={"tools": TOOLS, "toolChoice": "auto"})
    finally:
        clova.requests.post = real_post
        clova.time.sleep = real_sleep
    if capture is not None:
        capture.extend(seen)
    return r, seen


print("=" * 74)
print("1 — 1차 재시도(같은 payload)에서 성공")
print("=" * 74)

r, seen = run([400, 200])
check("ok=True", r.get("ok") is True, str(r.get("error"))[:80])
check("호출 2회", len(seen) == 2, f"{len(seen)}회")
check("retry_400 = 1", r.get("retry_400") == 1, str(r.get("retry_400")))
check("도구를 떼지 않았다", r.get("tools_dropped") is False, str(r.get("tools_dropped")))
check("두 번째 요청에도 tools 가 그대로", "tools" in seen[1])
check("429 계측은 그대로 0", r.get("retry_429") == 0 and r.get("retries") == 0,
      f"retries={r.get('retries')} 429={r.get('retry_429')}")

print()
print("=" * 74)
print("2 — 2차(도구 제거) 재시도에서 성공")
print("=" * 74)

r, seen = run([400, 400, 200])
check("ok=True", r.get("ok") is True, str(r.get("error"))[:80])
check("호출 3회", len(seen) == 3, f"{len(seen)}회")
check("retry_400 = 2", r.get("retry_400") == 2, str(r.get("retry_400")))
check("tools_dropped = True", r.get("tools_dropped") is True)
check("1·2차에는 tools 가 있었다", "tools" in seen[0] and "tools" in seen[1])
check("3차 요청에 tools 가 없다", "tools" not in seen[2], str(sorted(seen[2])))
check("3차 요청에 toolChoice 도 없다", "toolChoice" not in seen[2])
check("messages 는 그대로 간다", "messages" in seen[2])

print()
print("=" * 74)
print("3 — 끝까지 400 이어도 규격 dict")
print("=" * 74)

r, seen = run([400, 400, 400, 400, 400])
check("ok=False", r.get("ok") is False)
check("status=400", r.get("status") == 400, str(r.get("status")))
check("error 에 40009 가 남는다", "40009" in str(r.get("error")), str(r.get("error"))[:90])
check("retry_400 이 기록된다", r.get("retry_400") >= 2, str(r.get("retry_400")))
check("tools_dropped 가 기록된다", r.get("tools_dropped") is True)
check("예외를 던지지 않았다", isinstance(r, dict))
check("무한히 부르지 않는다", len(seen) <= clova.MAX_RETRY + 1, f"{len(seen)}회")

print()
print("=" * 74)
print("4 — 400 이 아예 없으면 아무것도 달라지지 않는다")
print("=" * 74)

r, seen = run([200])
check("ok=True · 호출 1회", r.get("ok") is True and len(seen) == 1, f"{len(seen)}회")
check("retry_400 = 0", r.get("retry_400") == 0)
check("tools_dropped = False", r.get("tools_dropped") is False)

print()
print("=" * 74)
print("5 — 429/5xx 경로는 그대로다")
print("=" * 74)

r, seen = run([429, 200])
check("429 뒤 성공", r.get("ok") is True, str(r.get("error"))[:60])
check("retry_429 = 1", r.get("retry_429") == 1, str(r.get("retry_429")))
check("retries = 1", r.get("retries") == 1, str(r.get("retries")))
check("400 계측은 건드리지 않았다", r.get("retry_400") == 0 and not r.get("tools_dropped"))
check("429 재시도에는 tools 가 그대로", "tools" in seen[1])

r, seen = run([500, 200])
check("5xx 뒤 성공", r.get("ok") is True)
check("retries = 1 · retry_429 = 0", r.get("retries") == 1 and r.get("retry_429") == 0,
      f"retries={r.get('retries')} 429={r.get('retry_429')}")

print()
print("=" * 74)
print("6 — 400 이 나도 answer() 는 규격 dict 를 낸다")
print("=" * 74)

seq = {"n": 0}


def always_400(url, headers=None, json=None, timeout=None):
    seq["n"] += 1
    return Resp(400, BODY_400)


real_post, real_sleep = clova.requests.post, clova.time.sleep
clova.requests.post = always_400
clova.time.sleep = lambda s: None
try:
    from src.agent.loop import answer

    res = answer("질문", deadline=50.0)
finally:
    clova.requests.post = real_post
    clova.time.sleep = real_sleep

check("answer 가 비지 않는다", bool((res.get("answer") or "").strip()),
      (res.get("answer") or "")[:60])
check("규격 키가 전부 있다",
      all(k in res for k in ("answer", "sources", "tool_calls", "elapsed")))
check("waste.retry_400 이 누적된다", res["waste"]["retry_400"] >= 2,
      str(res["waste"]["retry_400"]))
check("waste.tools_dropped = True", res["waste"]["tools_dropped"] is True)

from src.api.server import render_trace                    # noqa: E402

trace = render_trace(res)
check("think_trace 에 '400 재시도' 가 남는다", "400 재시도" in trace,
      trace.splitlines()[-1][:110])
check("think_trace 에 '도구 없이 재시도함' 이 남는다", "도구 없이 재시도함" in trace)

print()
print("  --- think_trace 마지막 줄 ---")
print("   ", trace.splitlines()[-1])

print()
print("=" * 74)
if FAIL:
    print(f"{len(FAIL)}건 실패: " + ", ".join(FAIL))
    sys.exit(1)
print("전부 통과 · LLM 실제 호출 0회")
