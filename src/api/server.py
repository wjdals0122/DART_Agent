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

3. 시간 층위(주최측 공지 2026-09-04: "문항 당 응답 대기 시간(타임아웃)은 300초"):
     주최측 300초 > API 하드 240초(HARD_DEADLINE) > 에이전트 소프트 200초(loop.DEADLINE)
   소프트 데드라인이 지나면 loop 가 도구 없이 마지막 답변을 받아내고,
   그래도 240초를 넘기면(비정상 멈춤) 여기서 폴백 응답을 먼저 보낸다.
   타임아웃에 딱 맞추면 답변 생성 도중에 상대가 먼저 끊는다.

4. 한글 질문은 URL 인코딩돼 온다. `scope["query_string"]` 은 bytes 이므로
   percent-decoding 을 바이트 단계에서 직접 하고 UTF-8 → cp949 순으로 읽는다.
   실측: Windows 셸의 curl 이 '하이' 를 %c7%cf%c0%cc(cp949) 로 보냈다.

【언제나 순수 ASGI 로 뜬다 — 2026-09-03 변경】
예전에는 "fastapi 가 깔려 있으면 그쪽을 쓴다" 였고 "응답과 상태코드는 완전히 같다" 고
적어 두었다. **틀렸다.** FastAPI 껍데기는 쿼리 파라미터를 Starlette 이
UTF-8 고정으로 디코딩해서, cp949 로 보내는 클라이언트의 한글 질문이 통째로 깨졌다.
서버에 fastapi 가 깔려 있어 실제로 그 경로가 떠 있었고 그대로 나갔다.

fastapi 는 이 개발 PC 에 설치할 수 없다(pydantic 2.8.2 → 2.13.5 를 끌고 와
bge-m3 환경을 흔든다. CLAUDE.md). 즉 **로컬에서 시험할 수 없는 경로**였고,
시험되지 않은 경로를 기본으로 둔 것이 사고의 원인이다.
지금은 `DART_API_FASTAPI=1` 을 명시해야만 그 껍데기를 쓴다. 그 경우에도
쿼리는 아래 `_params()` 로 똑같이 읽는다.
"""
from __future__ import annotations

import json
import os
import re
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

# API 하드 데드라인. 주최측 타임아웃 300초보다 짧아야 응답이 전송될 시간이 남는다.
# 평소에는 loop 의 소프트 데드라인 200초가 먼저 끝을 내므로 여기까지 오지 않는다 —
# 이것은 loop 가 비정상적으로 멈췄을 때(네트워크 행 등)의 마지막 방어선이다.
HARD_DEADLINE = 240.0

# retrieved_context 길이 상한. 공지에 "각 필드의 길이 제한은 없습니다" 라고 했지만
# 무한정 보내는 것도 의미가 없어 우리 쪽 상한을 둔다. 넘으면 **문서 단위로** 자르고
# (문서 중간에서 끊지 않는다) 답변 근거 절에 등장하는 접수번호의 문서를 우선 남긴다.
CONTEXT_LIMIT = 40_000

# 접수번호 14자리. 앞뒤에 숫자가 더 붙으면 접수번호가 아니다(regression.py 와 같은 꼴).
_RCEPT14 = re.compile(r"(?<!\d)\d{14}(?!\d)")

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


def _doc_block(d: dict) -> str:
    """문서 하나의 본문 블록. [문서N] 번호는 조립 때 붙인다(선별 뒤 번호가 바뀐다).

    헤더는 LLM 컨텍스트와 같은 형식(context.doc_header_lines, 1-E) —
    회계연도·제N기·기간·접수일을 명시해 평가자도 같은 정보를 본다.
    """
    try:
        from src.agent.context import doc_header_lines
        line = doc_header_lines(d.get("corp_name"), d.get("report_nm"),
                                d.get("rcept_no"), d.get("rcept_dt"))
    except Exception:                          # noqa: BLE001 — 헤더 실패는 최소 형식으로
        traceback.print_exc()
        line = [f"{d.get('corp_name', '')} {d.get('report_nm', '')}"
                + (f", 접수번호 {d['rcept_no']}" if d.get("rcept_no") else "")]
    if d.get("section"):
        line.append(f"섹션: {d['section']}")
    if d.get("note"):
        line.append(f"참고: {d['note']}")
    line.append("")
    line.append((d.get("text") or "").strip())
    return "\n".join(line)


def render_context(res: dict) -> str:
    """검색이 올린 청크를 문서별로 이어 붙인다. CONTEXT_LIMIT 를 넘으면 문서 단위로 자른다.

    【자르는 규칙】
    - 문서 중간에서 끊지 않는다. 문서를 통째로 넣거나 통째로 뺀다
    - 답변(근거 절 포함)에 등장하는 접수번호의 문서를 우선 남긴다 —
      근거로 인용된 문서가 retrieved_context 에 없으면 평가자가 검증할 수 없다
    - 첫 문서 하나가 상한보다 커도 통째로 넣는다(공지상 필드 길이 제한은 없다)
    - 뺀 문서 수는 끝에 한 줄로 밝힌다. 조용히 사라지게 두지 않는다
    """
    docs = res.get("documents") or []
    if not docs:
        return "검색된 문서 없음"
    blocks = [_doc_block(d) for d in docs]
    sep = "\n---\n"

    def assemble(keep_idx: list, dropped: int) -> str:
        parts = [f"[문서{n}] {blocks[j]}"
                 for n, j in enumerate(keep_idx, 1)]
        if dropped:
            parts.append(f"(길이 제한 {CONTEXT_LIMIT:,}자를 넘어 문서 {dropped}건 생략 — "
                         "조회 내역은 think_trace 참조)")
        return sep.join(parts)

    full = assemble(list(range(len(blocks))), 0)
    if len(full) <= CONTEXT_LIMIT:
        return full

    # 답변이 가리킨 접수번호의 문서 먼저, 나머지는 원래 순서대로
    cited = set(_RCEPT14.findall(res.get("answer") or ""))
    pri = [i for i, d in enumerate(docs) if str(d.get("rcept_no") or "") in cited]
    rest = [i for i in range(len(docs)) if i not in set(pri)]
    keep: set = set()
    total = 0
    for i in pri + rest:
        cost = len(blocks[i]) + len(sep) + 10          # 번호 머리말 여유분
        if not keep or total + cost <= CONTEXT_LIMIT:  # 첫 문서는 커도 넣는다
            keep.add(i)
            total += cost
    return assemble(sorted(keep), len(docs) - len(keep))


def render_trace(res: dict) -> str:
    """도구 호출 과정을 사람이 읽게. 평가 대상이라 성의 있게 적는다."""
    calls = res.get("tool_calls") or []
    n = 1
    out = []

    # 질문 재작성 단계가 있으면 그 결과를, 없으면(옛 경로·가짜 응답) 도구 인자에서
    # 뽑은 요약을 첫 줄로 쓴다. 생략됐으면 사유를 밝힌다 — 조용히 사라지지 않는다.
    rw = res.get("rewrite")
    if isinstance(rw, dict) and rw.get("ok") and (rw.get("analysis") or {}):
        a = rw["analysis"]
        bits = []
        if a.get("rewritten"):
            bits.append(f'재작성 "{a["rewritten"]}"')
        if a.get("companies"):
            bits.append("회사 " + "·".join(a["companies"]))
        if a.get("fiscal_years"):
            bits.append("회계연도 " + "·".join(str(y) for y in a["fiscal_years"]))
        # 코드가 판정한 시점 제약(범위 밖 연도·여러 시점 질문)은 LLM 이 쓴
        # time_note 보다 **앞**에 싣는다. think_trace 는 채점 필드다 —
        # 왜 연도 필터를 빼거나 다른 해로 답했는지가 여기서 보여야 한다.
        if a.get("range_note"):
            bits.append(f"시점: {a['range_note']}")
        if a.get("time_note"):
            bits.append(f"시점 해석: {a['time_note']}")
        out.append(f"{n}. 질문 분석: " + (" · ".join(bits) or "특이사항 없음"))
        n += 1
    elif isinstance(rw, dict):
        out.append(f"{n}. 질문 분석 생략({rw.get('note') or '사유 미상'})")
        n += 1
    else:
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
    w = res.get("waste") or {}
    # 400(40009)은 산발 오류다. 몇 번 다시 불렀는지, 도구를 떼고 불렀는지를
    # 평가자가 볼 수 있게 남긴다 — 답변이 짧아진 이유가 여기 있을 수 있다
    if w.get("retry_400"):
        tail.append(f"400 재시도 {w['retry_400']}회")
    if w.get("tools_dropped"):
        tail.append("도구 없이 재시도함")
    u = res.get("usage") or {}
    tail.append(f"LLM {u.get('llm_calls', 0)}회 · {res.get('elapsed', 0):.1f}초")
    out.append(f"{n}. 비고: " + " · ".join(tail))
    return "\n".join(out)


def make_answer(question_id: str, question: str, detail: dict = None) -> dict:
    """규격 5필드. 여기서 예외가 새어 나가면 안 된다.

    `detail` 을 주면 에이전트 루프의 결과(res)를 그대로 담아 준다 — 요청 로그가
    도구 호출·후처리 내역을 남기는 데 쓴다(step61). 반환값은 언제나 5필드 그대로다.
    """
    out = {"question_id": str(question_id or ""), "question": str(question or ""),
           "retrieved_context": "", "think_trace": "", "answer": ""}
    if not (question or "").strip():
        out["think_trace"] = "1. question 파라미터가 비어 있어 질의를 수행하지 않았다"
        out["retrieved_context"] = "검색된 문서 없음"
        out["answer"] = ("질문이 전달되지 않았습니다. "
                         "question 파라미터에 질의 내용을 넣어 다시 요청해 주세요.")
        return out
    try:
        # 【하드 데드라인 — 워커 스레드 + join(HARD_DEADLINE)】
        # loop 의 소프트 데드라인(200초)이 정상 경로의 끝이다. 여기는 loop 가
        # 그마저 못 지키고 멈춰 있을 때(네트워크 행 등) 주최측 300초 타임아웃 전에
        # 폴백이라도 보내는 장치다. 시간을 넘긴 스레드는 daemon 이라 그대로 두면
        # 알아서 끝난다 — 평가는 순차 호출이라 다음 문항과 겹칠 일이 거의 없다.
        box: dict = {}

        def _work() -> None:
            try:
                from src.agent.loop import answer

                box["res"] = answer(question, qid=question_id or None)
            except BaseException as e:         # noqa: BLE001 — 본 스레드로 넘긴다
                box["err"] = e

        th = threading.Thread(target=_work, name="answer", daemon=True)
        th.start()
        th.join(HARD_DEADLINE)
        if th.is_alive():
            out["think_trace"] = (f"1. 내부 하드 데드라인 {HARD_DEADLINE:.0f}초를 넘겨 "
                                  "답변 생성을 중단하고 폴백을 보냈다")
            out["retrieved_context"] = "검색된 문서 없음"
            out["answer"] = FALLBACK
            for k in FIELDS:
                out[k] = "" if out.get(k) is None else str(out[k])
            return out
        if "err" in box:
            raise box["err"]

        res = box["res"]
        if isinstance(detail, dict) and isinstance(res, dict):
            detail["res"] = res
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


REQ_LOG = ROOT / "logs" / "requests.jsonl"


def log_request(question_id: str, question: str, status: int, sec: float,
                res: dict = None) -> None:
    """받은 질문과 답변을 **UTF-8 로** 한 줄 남긴다. 실패해도 응답을 막지 않는다.

    uvicorn 의 access 로그에는 percent-encoding 된 경로만 남아 한글을 눈으로
    확인할 수 없다. 인코딩 사고가 났을 때 "무엇을 받았는가" 를 볼 자리가 필요하다.

    【답변까지 남기는 이유 — step61】
    V4 실호출에서 답변이 문장 중간부터 시작해 나갔는데(소수점 문장 분할 사고),
    이 로그에 질문만 있어서 **원본을 못 찾고 재현에 의존해야 했다.**
    200문항 본평가 중 같은 일이 나면 되짚을 자리가 없다. 그래서 남긴다.

      answer      최종 답변 전문
      answer_raw  후처리 **전** 전문. 실제로 지운 것이 있을 때만 채워진다
                  (붙이기만 한 경우는 최종 답변에서 되짚을 수 있어 생략 — 파일 크기)
      tool_calls  name · arguments · status
      postproc    어떤 후처리가 몇 글자를 줄였나 (removed 가 음수면 붙인 것)
    """
    try:
        REQ_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": status,
               "question_id": question_id, "question": question,
               "seconds": round(sec, 2)}
        if isinstance(res, dict):
            row["answer"] = str(res.get("answer") or "")
            raw = str(res.get("answer_raw") or "")
            if raw:                            # 지운 것이 있을 때만
                row["answer_raw"] = raw
            row["tool_calls"] = [
                {"name": str(c.get("name") or ""),
                 "arguments": c.get("arguments") if isinstance(
                     c.get("arguments"), dict) else {},
                 "status": str(c.get("status") or "")}
                for c in (res.get("tool_calls") or []) if isinstance(c, dict)]
            row["postproc"] = [
                {"fn": str(p.get("fn") or ""), "removed": int(p.get("removed") or 0)}
                for p in (res.get("postproc") or []) if isinstance(p, dict)]
        with open(REQ_LOG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:                     # noqa: BLE001 — 로그가 답을 막으면 안 된다
        print(f"[api] 요청 로그 기록 실패: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def handle_answer(params: dict) -> tuple:
    qid = params.get("question_id", "")
    q = params.get("question", "")
    if not STATE["ready"]:
        # 기동 전에는 503. 주최측이 재시도하는 사이에 준비가 끝난다.
        log_request(qid, q, 503, 0.0)
        return 503, {"status": "starting", "detail": STATE["error"] or "모델 로딩 중",
                     "elapsed": round(time.time() - STATE["started_at"], 1)}
    STATE["served"] += 1
    t0 = time.time()
    detail: dict = {}
    body = make_answer(qid, q, detail)
    # 답변 전문·도구 호출·후처리 내역까지 남긴다(step61). 로깅 실패는 응답을 막지 않는다.
    log_request(qid, q, 200, time.time() - t0, detail.get("res"))
    return 200, body


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
    """fastapi 껍데기. **기본으로는 쓰지 않는다** — `DART_API_FASTAPI=1` 일 때만.

    【2026-09-03 · 치명적 버그를 여기서 냈다】
    예전에는 "fastapi 가 있으면 그쪽을 쓴다" 였고, 주석에 "응답과 상태코드는 완전히
    같다" 라고 적어 두었다. **틀렸다.** 같지 않은 곳이 하나 있었다 — 쿼리 파라미터를
    누가 디코딩하느냐다.

      순수 ASGI : _params() 가 바이트를 쥐고 UTF-8 → cp949 순으로 읽는다
      FastAPI   : Starlette 이 UTF-8 + errors="replace" 로 고정 디코딩한다

    이 PC 의 Git Bash curl 은 한글을 **cp949** 로 인코딩해 보낸다(실측:
    '하이' → %c7%cf%c0%cc). 서버에 fastapi 가 깔려 있어 그쪽이 떠 있었고,
    cp949 바이트가 UTF-8 로 읽히면서 question 이 통째로 U+FFFD 가 됐다.

      요청 question=SK하이닉스 …  →  응답 "question":"SK���̴н� …"

    같은 응답의 retrieved_context·answer 는 서버가 만든 문자열이라 멀쩡했다.
    그래서 '출력이 아니라 입력 경로' 로 좁혀졌다.

    【그래서 두 가지를 했다】
    1. 이 껍데기도 `_params()` 로 쿼리를 읽는다 — 선언형 파라미터를 쓰지 않는다
    2. **기본을 순수 ASGI 로 되돌렸다.** fastapi 는 이 개발 PC 에 설치할 수 없어
       (pydantic 2.8.2 → 2.13.5 를 끌고 와 bge-m3 환경을 흔든다) **이 경로는
       로컬에서 시험할 수가 없다.** 시험되지 않은 경로를 기본으로 두었다가
       이 버그가 났다. 쓰려면 `DART_API_FASTAPI=1` 로 **명시**해야 한다.
    """
    try:
        from fastapi import FastAPI, Request
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
    def _answer(request: Request):
        # 선언형 파라미터(question: str = "")를 쓰면 Starlette 이 UTF-8 고정으로
        # 디코딩한다. 순수 ASGI 와 같은 결과를 내려면 바이트를 직접 읽어야 한다.
        st, body = handle_answer(_params(request.scope.get("query_string") or b""))
        return JSONResponse(body, status_code=st)

    return api


# 기본은 순수 ASGI. fastapi 를 쓰려면 환경변수로 명시한다(위 주석 참고).
USE_FASTAPI = os.environ.get("DART_API_FASTAPI", "") == "1"
_fastapi = build_fastapi() if USE_FASTAPI else None
if _fastapi is not None:
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
