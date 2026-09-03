"""평가용 API 서버.

  GET /answer?question_id=Q-001&question=...   → 200 + 규격 JSON
  GET /health                                  → 준비 전 503 / 준비 후 200

  python -m uvicorn src.api.server:app --host 0.0.0.0 --port 80
  또는  bash serve.sh

【지키는 계약 — 여기서 어기면 점수가 통째로 날아간다】

1. 모델·인덱스는 기동할 때 한 번만 올린다(35초). 요청마다 올리면 첫 문항부터 끊긴다.
   올리는 동안 `/answer` 는 503 을 준다. 주최측은 5xx 에 2회 재시도하므로
   그 사이에 준비가 끝나면 정상 서비스된다.

2. **준비된 뒤에는 무슨 일이 있어도 200 과 규격 JSON을 돌려준다.**
   5xx 를 뱉으면 재시도 2회를 낭비하고 결국 미응답 처리된다.
   answer 는 최악의 경우에도 "공시에서 확인되지 않음" 으로 채운다.

3. 내부 데드라인 50초(`loop.DEADLINE`). 주최측 60초보다 짧아야 답을 만들어
   보낼 시간이 남는다. 60초로 두면 답변 생성 도중에 상대가 먼저 끊는다.

4. 한글 질문은 URL 인코딩돼 온다. `scope["query_string"]` 은 bytes 이므로
   percent-decoding 을 바이트 단계에서 직접 하고 UTF-8 → cp949 순으로 읽는다.
   실측: Windows 셸의 curl 이 '하이' 를 %c7%cf%c0%cc(cp949) 로 보냈다.

【FastAPI 가 있으면 FastAPI, 없으면 순수 ASGI】
이 PC 에는 fastapi 가 없고, 넣으려면 pydantic 2.8.2 → 2.13.5 와
typing_extensions 를 올려야 한다. torch/FlagEmbedding 에 bge-m3 벡터가 묶여 있어
마감 직전에 건드릴 수 없다(CLAUDE.md). uvicorn 만으로 도는 ASGI 경로를 기본으로 두고,
fastapi 가 있는 환경에서는 그쪽을 쓴다. **응답과 상태코드는 둘이 완전히 같다** —
판단 로직은 handle_* 한 곳에만 있고 아래는 얇은 껍데기다.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import unquote_to_bytes

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

FALLBACK = "공시에서 확인되지 않음"
FIELDS = ("question_id", "question", "retrieved_context", "think_trace", "answer")

# 기동 상태. 스레드 하나가 채우고 요청 쪽에서는 읽기만 한다.
STATE = {
    "ready": False,
    "started_at": time.time(),
    "warm_seconds": None,
    "error": None,
    "served": 0,
}


# ---------------------------------------------------------------- 기동


def _warm() -> None:
    """인덱스와 임베딩 모델을 올린다. 실패해도 서버는 산다 — /health 가 이유를 말한다."""
    t0 = time.time()
    try:
        from src.agent.loop import warmup

        sec = warmup()
        STATE["warm_seconds"] = round(sec, 1)
        STATE["ready"] = True
        print(f"[api] 준비 완료 {sec:.1f}초", flush=True)
    except BaseException as e:                 # noqa: BLE001 — SystemExit 도 잡는다
        STATE["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        print(f"[api] 기동 실패 — {STATE['error']}", file=sys.stderr, flush=True)
    finally:
        STATE["warm_seconds"] = STATE["warm_seconds"] or round(time.time() - t0, 1)


def start_warmup() -> None:
    print(f"[api] 인덱스·모델을 올리는 중… (약 35초)", flush=True)
    threading.Thread(target=_warm, name="warmup", daemon=True).start()


# ---------------------------------------------------------------- 응답 만들기


def render_context(res: dict) -> str:
    """검색이 올린 청크를 문서별로 이어 붙인다."""
    docs = res.get("documents") or []
    if not docs:
        return "검색된 문서 없음"
    parts = []
    for i, d in enumerate(docs, 1):
        head = f"[문서{i}] {d.get('corp_name', '')} {d.get('report_nm', '')}"
        if d.get("rcept_no"):
            head += f", 접수번호 {d['rcept_no']}"
        line = [head]
        if d.get("section"):
            line.append(f"섹션: {d['section']}")
        if d.get("note"):
            line.append(f"참고: {d['note']}")
        line.append("")
        line.append((d.get("text") or "").strip())
        parts.append("\n".join(line))
    return "\n---\n".join(parts)


def render_trace(res: dict) -> str:
    """도구 호출 과정을 사람이 읽게. 평가 대상이라 성의 있게 적는다."""
    calls = res.get("tool_calls") or []
    n = 1
    out = []

    corp = next((c["arguments"].get("corp_name") for c in calls
                 if isinstance(c.get("arguments"), dict)
                 and c["arguments"].get("corp_name")), None)
    year = next((c["arguments"].get("fiscal_year") for c in calls
                 if isinstance(c.get("arguments"), dict)
                 and c["arguments"].get("fiscal_year")), None)
    bits = [x for x in (corp, f"{year} 사업연도" if year else None) if x]
    out.append(f"{n}. 질문 분석: " + (", ".join(bits) if bits
                                    else "회사·시점을 특정하지 못했다"))
    n += 1

    if not calls:
        why = res.get("note") or "도구를 부르지 않고 답할 수 있다고 판단했다"
        out.append(f"{n}. 도구를 호출하지 않았다 — {why}")
        n += 1
    for c in calls:
        args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
        shown = ", ".join(f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}"
                          for k, v in args.items() if v not in (None, "", []))
        line = f"{n}. {c.get('name')}({shown}) → {c.get('status')}"
        if c.get("summary"):
            line += f", {c['summary']}"
        if c.get("status") != "ok" and c.get("note"):
            line += f"\n   사유: {str(c['note']).splitlines()[0][:160]}"
        out.append(line)
        n += 1

    docs = res.get("documents") or []
    if docs:
        secs = []
        for d in docs[:5]:
            s = (d.get("section") or "").split(" > ")
            secs.append(" > ".join(s[-2:]) if len(s) > 1 else (s[0] if s else ""))
        out.append(f"{n}. 근거 확인: 청크 {len(docs)}개 — " + " / ".join(x for x in secs if x))
        n += 1
    if res.get("sources"):
        out.append(f"{n}. 답변 작성: 출처 {len(res['sources'])}건을 근거 절에 붙였다")
    else:
        out.append(f"{n}. 답변 작성: 근거를 확보하지 못해 확인 불가로 답했다")
    n += 1

    tail = []
    if res.get("note"):
        tail.append(res["note"])
    if res.get("unsourced_claim"):
        tail.append("근거 없이 단정한 부분이 있어 답변 앞에 표시를 붙였다")
    u = res.get("usage") or {}
    tail.append(f"LLM {u.get('llm_calls', 0)}회 · {res.get('elapsed', 0):.1f}초")
    out.append(f"{n}. 비고: " + " · ".join(tail))
    return "\n".join(out)


def make_answer(question_id: str, question: str) -> dict:
    """규격 5필드. 여기서 예외가 새어 나가면 안 된다."""
    out = {"question_id": str(question_id or ""), "question": str(question or ""),
           "retrieved_context": "", "think_trace": "", "answer": ""}
    if not (question or "").strip():
        out["think_trace"] = "1. question 파라미터가 비어 있어 질의를 수행하지 않았다"
        out["retrieved_context"] = "검색된 문서 없음"
        out["answer"] = ("질문이 전달되지 않았습니다. "
                         "question 파라미터에 질의 내용을 넣어 다시 요청해 주세요.")
        return out
    try:
        from src.agent.loop import answer

        res = answer(question, qid=question_id or None)
        out["retrieved_context"] = render_context(res)
        out["think_trace"] = render_trace(res)
        out["answer"] = (res.get("answer") or "").strip() or FALLBACK
    except BaseException as e:                 # noqa: BLE001 — 마지막 방어선
        traceback.print_exc()
        out["think_trace"] = f"1. 처리 중 오류가 발생했다 — {type(e).__name__}: {e}"
        out["retrieved_context"] = out["retrieved_context"] or "검색된 문서 없음"
        out["answer"] = FALLBACK
    for k in FIELDS:                            # 모든 필드는 문자열이어야 한다
        out[k] = "" if out.get(k) is None else str(out[k])
    return out


# ---------------------------------------------------------------- 라우팅 (한 곳)


def handle_health() -> tuple:
    if STATE["error"]:
        return 503, {"status": "error", "detail": STATE["error"]}
    if not STATE["ready"]:
        return 503, {"status": "starting",
                     "elapsed": round(time.time() - STATE["started_at"], 1)}
    return 200, {"status": "ok", "warm_seconds": STATE["warm_seconds"],
                 "served": STATE["served"],
                 "uptime": round(time.time() - STATE["started_at"], 1)}


def handle_answer(params: dict) -> tuple:
    if not STATE["ready"]:
        # 기동 전에는 503. 주최측이 재시도하는 사이에 준비가 끝난다.
        return 503, {"status": "starting", "detail": STATE["error"] or "모델 로딩 중",
                     "elapsed": round(time.time() - STATE["started_at"], 1)}
    STATE["served"] += 1
    return 200, make_answer(params.get("question_id", ""), params.get("question", ""))


def route(path: str, params: dict) -> tuple:
    if path in ("/health", "/healthz", "/health/"):
        return handle_health()
    if path in ("/answer", "/answer/"):
        return handle_answer(params)
    if path in ("/", ""):
        return 200, {"service": "공시 Agent 평가 API",
                     "endpoints": ["/answer?question_id=&question=", "/health"]}
    return 404, {"status": "not_found", "path": path}


# ---------------------------------------------------------------- ASGI


def _decode(b: bytes) -> str:
    """UTF-8 이 정석이지만, 한국어 클라이언트가 cp949 로 인코딩해 보내는 일이 있다.

    실측: Windows 셸의 curl 이 '하이' 를 %c7%cf%c0%cc(cp949) 로 보냈다.
    UTF-8 로만 읽으면 U+FFFD 로 깨진다. UTF-8 을 먼저 시도하고 안 되면 cp949 를 본다.
    올바른 UTF-8 은 첫 시도에서 통과하므로 이 폴백이 정상 요청을 건드리지 않는다.
    """
    for enc in ("utf-8", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _params(qs: bytes) -> dict:
    """query_string 은 bytes 다. percent-decoding 을 바이트 단계에서 직접 한다.

    `parse_qs(str)` 를 쓰면 인코딩 판단을 못 하고 UTF-8 로 고정된다.
    한글 질의가 걸린 문제라 여기서는 바이트를 손에 쥐고 결정한다.
    """
    out: dict = {}
    for pair in qs.split(b"&"):
        if not pair:
            continue
        k, _, v = pair.partition(b"=")
        key = _decode(unquote_to_bytes(k.replace(b"+", b" ")))
        if key and key not in out:
            out[key] = _decode(unquote_to_bytes(v.replace(b"+", b" ")))
    return out


async def app(scope, receive, send) -> None:
    if scope["type"] == "lifespan":
        while True:
            m = await receive()
            if m["type"] == "lifespan.startup":
                start_warmup()
                await send({"type": "lifespan.startup.complete"})
            elif m["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return
    if scope["type"] != "http":
        return

    try:
        status, body = route(scope.get("path", "/"),
                             _params(scope.get("query_string", b"")))
    except BaseException as e:                 # noqa: BLE001 — 여기서도 5xx 를 막는다
        traceback.print_exc()
        status, body = 200, {"question_id": "", "question": "",
                             "retrieved_context": "검색된 문서 없음",
                             "think_trace": f"1. 라우팅 오류 — {type(e).__name__}: {e}",
                             "answer": FALLBACK}

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(payload)).encode())]})
    await send({"type": "http.response.body", "body": payload})


# ---------------------------------------------------------------- FastAPI 껍데기


def build_fastapi():
    """fastapi 가 있으면 같은 handle_* 를 감싸 준다. 없으면 None."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError:
        return None

    api = FastAPI(title="공시 Agent 평가 API", docs_url=None, redoc_url=None)

    @api.on_event("startup")
    def _on_start():
        start_warmup()

    @api.get("/health")
    def _health():
        st, body = handle_health()
        return JSONResponse(body, status_code=st)

    @api.get("/answer")
    def _answer(question_id: str = "", question: str = ""):
        st, body = handle_answer({"question_id": question_id, "question": question})
        return JSONResponse(body, status_code=st)

    return api


_fastapi = build_fastapi()
if _fastapi is not None:                       # 있으면 그쪽이 app 이 된다
    app = _fastapi                             # noqa: F811


def main() -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="평가용 API 서버")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "80")))
    a = ap.parse_args()

    import uvicorn

    print(f"[api] {'FastAPI' if _fastapi is not None else '순수 ASGI'} 로 뜬다 "
          f"· {a.host}:{a.port}", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info", access_log=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
