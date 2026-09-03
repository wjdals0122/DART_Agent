"""API 서버 계약 검증. **LLM 을 한 번도 부르지 않는다.**

  python eval/test_api.py

ASGI 앱을 프로세스 안에서 직접 두드린다 — 서버를 띄우지 않아도 상태코드와
응답 모양을 그대로 확인할 수 있다. 실제 HTTP·실제 LLM 왕복은 `eval/api_smoke.py`.

【무엇을 지키는가】
1. 준비 전 `/answer`·`/health` 는 503, 준비 후 200
2. `answer()` 가 무엇을 던지든 **200 과 규격 5필드**
3. question 누락도 500 이 아니라 200 + 안내
4. 한글이 URL 인코딩돼 와도 그대로 복원된다
5. 모든 필드가 문자열이다 (명세)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from src.agent import loop as loop_mod  # noqa: E402
from src.api import server as S  # noqa: E402

BAR = "=" * 74
FAIL: list = []
FIELDS = ("question_id", "question", "retrieved_context", "think_trace", "answer")


def check(name, cond, detail=""):
    if not cond:
        FAIL.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(path: str, **params) -> tuple:
    """ASGI 앱을 직접 부른다. (status, dict) 를 돌려준다."""
    qs = urlencode(params, encoding="utf-8").encode("ascii")
    scope = {"type": "http", "method": "GET", "path": path, "query_string": qs,
             "headers": []}
    got: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            got["status"] = msg["status"]
            got["headers"] = dict(msg.get("headers") or [])
        elif msg["type"] == "http.response.body":
            got["body"] = msg["body"]

    asyncio.run(S.app(scope, receive, send))
    return got["status"], json.loads(got["body"].decode("utf-8")), got


def spec_ok(body: dict) -> bool:
    return (set(body) == set(FIELDS)
            and all(isinstance(body[k], str) for k in FIELDS))


def main() -> int:
    print(f"{BAR}\n1 — 준비 전에는 503\n{BAR}")
    S.STATE.update(ready=False, error=None)
    st, body, _ = call("/answer", question_id="Q-000", question="아무거나")
    check("/answer 503", st == 503, str(st))
    check("본문에 이유", body.get("status") == "starting", str(body))
    st, body, _ = call("/health")
    check("/health 503", st == 503, str(st))
    check("status=starting", body.get("status") == "starting", str(body))

    print(f"\n{BAR}\n2 — 준비되면 200 (LLM 은 가짜)\n{BAR}")
    S.STATE.update(ready=True, warm_seconds=34.6)
    st, body, _ = call("/health")
    check("/health 200", st == 200, str(st))
    check("warm_seconds 를 알려준다", body.get("warm_seconds") == 34.6, str(body))

    real = loop_mod.answer
    doc = {"chunk_id": "x:0001", "corp_name": "SK하이닉스",
           "report_nm": "사업보고서 (2024.12)", "rcept_no": "20250311001275",
           "rcept_dt": "20250311",
           "section": "[SK하이닉스] III. 재무에 관한 사항 > 2. 연결재무제표 > 2-2. 연결 손익계산서",
           "note": "", "text": "매출액 66,192,960백만원"}

    def fake_ok(question, **kw):
        return {"answer": "2024 사업연도 매출액은 66,192,960백만원이다.\n\n근거\n- [SK하이닉스 …]",
                "sources": ["[SK하이닉스 사업보고서 (2024.12), 2025-03-11, 접수번호 20250311001275]"],
                "tool_calls": [{"n": 1, "name": "search_disclosure",
                                "arguments": {"corp_name": "SK하이닉스",
                                              "fiscal_year": 2024,
                                              "query": "연결 손익계산서 매출액"},
                                "status": "ok", "summary": "5건 · 후보 2,720",
                                "note": ""}],
                "documents": [doc], "elapsed": 12.3, "note": "",
                "usage": {"llm_calls": 2}, "unsourced_claim": False}

    loop_mod.answer = fake_ok
    st, body, raw = call("/answer", question_id="Q-001",
                         question="SK하이닉스 2024년 연결 매출액이 얼마야?")
    loop_mod.answer = real
    check("200", st == 200, str(st))
    check("규격 5필드 · 전부 문자열", spec_ok(body), str(sorted(body)))
    check("한글 질문이 그대로 복원", body["question"] == "SK하이닉스 2024년 연결 매출액이 얼마야?",
          body["question"])
    check("question_id 반영", body["question_id"] == "Q-001")
    check("retrieved_context 에 문서 머리말", "[문서1]" in body["retrieved_context"])
    check("retrieved_context 에 접수번호", "20250311001275" in body["retrieved_context"])
    check("retrieved_context 에 섹션", "섹션:" in body["retrieved_context"])
    check("retrieved_context 에 본문", "66,192,960" in body["retrieved_context"])
    check("think_trace 가 번호 매긴 절차", body["think_trace"].startswith("1. 질문 분석"))
    check("think_trace 에 도구 인자", 'corp_name="SK하이닉스"' in body["think_trace"])
    check("Content-Type utf-8",
          b"utf-8" in raw["headers"].get(b"content-type", b""),
          str(raw["headers"].get(b"content-type")))
    print("\n  --- think_trace ---")
    for ln in body["think_trace"].splitlines():
        print("   ", ln)

    print(f"\n{BAR}\n3 — answer() 가 터져도 200 + 규격 JSON\n{BAR}")
    for name, boom in (("RuntimeError", RuntimeError("검색기 폭발")),
                       ("MemoryError", MemoryError()),
                       ("SystemExit", SystemExit("치명적"))):
        def fake_boom(question, _e=boom, **kw):
            raise _e

        loop_mod.answer = fake_boom
        st, body, _ = call("/answer", question_id="Q-002", question="질문")
        loop_mod.answer = real
        check(f"{name} → 200", st == 200, str(st))
        check(f"{name} → 규격 5필드", spec_ok(body), str(sorted(body)))
        check(f"{name} → answer 가 비지 않는다", body["answer"].strip() != "",
              body["answer"][:40])
        check(f"{name} → think_trace 에 사유", "오류" in body["think_trace"],
              body["think_trace"][:60])

    print(f"\n{BAR}\n4 — question 누락\n{BAR}")
    st, body, _ = call("/answer", question_id="Q-003")
    check("200", st == 200, str(st))
    check("규격 5필드", spec_ok(body))
    check("answer 에 안내", "question" in body["answer"], body["answer"][:60])
    st, body, _ = call("/answer")
    check("파라미터가 아예 없어도 200", st == 200, str(st))
    check("question_id 는 빈 문자열", body["question_id"] == "")

    print(f"\n{BAR}\n5 — 도구를 안 부른 경우의 think_trace\n{BAR}")

    def fake_notool(question, **kw):
        return {"answer": "공시에서 확인되지 않음", "sources": [], "tool_calls": [],
                "documents": [], "elapsed": 5.2,
                "note": "회사가 특정되지 않아 되물었다",
                "usage": {"llm_calls": 3}, "unsourced_claim": False}

    loop_mod.answer = fake_notool
    st, body, _ = call("/answer", question_id="Q-004", question="이 회사 매출은?")
    loop_mod.answer = real
    check("200", st == 200)
    check("도구 미호출 사실을 적는다", "도구를 호출하지 않았다" in body["think_trace"],
          body["think_trace"][:80])
    check("이유도 적는다", "특정되지 않아" in body["think_trace"])
    check("문서 없음 표기", body["retrieved_context"] == "검색된 문서 없음",
          body["retrieved_context"][:40])

    print(f"\n{BAR}\n6 — 질의 문자열 디코딩\n{BAR}")
    from urllib.parse import quote

    from src.api.server import _params

    want = "SK하이닉스 2024년 연결 매출액이 얼마야?"
    for enc in ("utf-8", "cp949"):
        qs = ("question_id=Q-1&question=" + quote(want, encoding=enc)).encode("ascii")
        check(f"{enc} 인코딩 질의를 그대로 복원", _params(qs)["question"] == want,
              _params(qs)["question"][:14])
    check("+ 는 공백으로",
          _params(b"question=%EC%97%B0%EA%B2%B0+%EB%A7%A4%EC%B6%9C")["question"]
          == "연결 매출")
    check("빈 값도 키는 남는다",
          _params(b"question_id=&question=") == {"question_id": "", "question": ""})

    print(f"\n{BAR}\n7 — 그 밖\n{BAR}")
    st, body, _ = call("/nope")
    check("모르는 경로는 404", st == 404, str(st))
    check("내부 데드라인 50초", loop_mod.DEADLINE == 50.0, str(loop_mod.DEADLINE))

    print(f"\n{BAR}")
    if FAIL:
        print(f"실패 {len(FAIL)}건: {FAIL}")
        return 1
    print("전부 통과 · LLM 실제 호출 0회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
