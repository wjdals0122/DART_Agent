"""HyperCLOVA X Chat Completions v3 최소 클라이언트.

【인증 방식 — 공식 문서에서 확인함: (a) Bearer 방식】
  https://api.ncloud-docs.com/docs/en/clovastudio-chatcompletionsv3
  Authorization: Bearer {CLOVA Studio API Key}
  X-NCP-CLOVASTUDIO-REQUEST-ID: {Request ID}
  Content-Type: application/json
구방식(X-NCP-CLOVASTUDIO-API-KEY + X-NCP-APIGW-API-KEY)은 v3 문서에 나오지 않는다.
.env 의 키가 `nv-` 로 시작하는 것도 (a) 방식과 일치한다.

혹시 (b)로 바꿔야 하면 `build_headers()` 하나만 고치면 된다. 다른 곳에서는 헤더를 만들지 않는다.

【보안】
- 키는 환경변수 CLOVA_API_KEY 로만 읽는다. 이 파일 어디에도 값이 없다.
- 헤더를 통째로 출력하지 않는다. 로그가 필요하면 `safe_headers()` 를 써라.
- 키가 없거나 빈 문자열이면 호출하지 않고 곧바로 실패를 반환한다.
  (빈 키로 부르면 401 이 오고, 그걸 API 장애로 오진하기 쉽다)

【모델 이름】
코드에 박지 않는다. `resolve_model()` 이 인자 > 환경변수 `CLOVA_MODEL` > `FALLBACK_MODEL`
순으로 고른다. `.env` 에 `CLOVA_MODEL=HCX-007` 을 넣으면 코드를 안 고치고 바꿀 수 있다.
반환 dict 의 `model` 은 **실제로 호출한 이름**이다.

  python -m src.agent.clova            # 키 존재만 확인 (마스킹 출력)
  python -m src.agent.clova --ping     # 실제 호출 1건
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

# 아래 4개는 문서에서 확인한 값이다.
DEFAULT_HOST = "https://clovastudio.stream.ntruss.com"
PATH_FMT = "/v3/chat-completions/{model}"
FALLBACK_MODEL = "HCX-005"   # 128k 컨텍스트. 문서 예시 모델
OK_CODE = "20000"      # 문서상 성공 status.code

# 추론형 모델은 maxTokens 를 거부하고 maxCompletionTokens 를 요구한다. 실측:
#   HCX-007 + maxTokens        -> 400 40001 "Invalid parameter: maxTokens"
#   HCX-007 + maxCompletionTokens -> 200
# 문서에도 두 필드는 함께 쓸 수 없다고 되어 있다.
REASONING_MODELS = {"HCX-007"}

# 모델·호스트는 **모듈을 읽을 때가 아니라 부를 때** 환경에서 꺼낸다.
# import 시점에 os.environ 을 읽으면 .env 가 아직 로드되기 전이라
# .env 에 적어 둔 CLOVA_MODEL 이 조용히 무시된다. 실제로 그랬다.

TIMEOUT = 60
MAX_RETRY = 2          # 429/5xx 에 대해 지수 백오프로 2회까지
BACKOFF0 = 2.0

_env_loaded = False


def _load_env() -> None:
    global _env_loaded
    if not _env_loaded:
        # .env 내용 자체는 출력하지 않는다
        load_dotenv(ROOT / ".env", override=False)
        _env_loaded = True


def resolve_model(model=None) -> str:
    """쓸 모델 이름 하나. 인자 > 환경변수 CLOVA_MODEL > FALLBACK_MODEL 순."""
    if model:
        return str(model).strip()
    _load_env()
    return (os.environ.get("CLOVA_MODEL") or "").strip() or FALLBACK_MODEL


def resolve_host() -> str:
    _load_env()
    return (os.environ.get("CLOVA_API_HOST") or "").strip() or DEFAULT_HOST


def get_api_key():
    """(key, error) 를 돌려준다. 키 값은 절대 로그로 나가지 않는다."""
    _load_env()
    raw = os.environ.get("CLOVA_API_KEY")
    if raw is None:
        return None, ("CLOVA_API_KEY 가 환경에 없다. "
                      "리포 루트 .env 에 CLOVA_API_KEY=... 를 넣어라")
    # Windows 에서 만든 .env 는 끝에 공백·따옴표가 붙는 일이 있다
    key = raw.strip().strip('"').strip("'").strip()
    if not key:
        return None, ("CLOVA_API_KEY 가 빈 문자열이다. 빈 키로는 호출하지 않는다 "
                      "(빈 키는 401 로 돌아와 API 장애로 오진하기 쉽다)")
    return key, None


def mask_key(key) -> str:
    """디버깅용 표시. 앞 3자와 길이만 보여준다."""
    if not key:
        return "(없음)"
    return f"{key[:3]}{'*' * 8}(len={len(key)})"


def build_headers(key: str, request_id=None, stream: bool = False) -> dict:
    """헤더를 만드는 유일한 지점. 인증 방식이 (b)로 바뀌면 여기만 고친다."""
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": request_id or uuid.uuid4().hex,
    }


def safe_headers(headers: dict) -> dict:
    """로그용. Authorization 의 키를 마스킹한 사본을 만든다."""
    out = {}
    for k, v in headers.items():
        if k.lower() == "authorization":
            tok = v.split(" ", 1)[1] if " " in v else v
            out[k] = f"Bearer {mask_key(tok)}"
        else:
            out[k] = v
    return out


def _norm_messages(messages) -> list:
    # v3 는 content 로 문자열과 배열을 모두 받는다. 들어온 형태를 그대로 넘긴다.
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _extract_text(raw: dict):
    """문서상 경로: result.message.content (문자열 또는 [{type:text,text:...}] 배열)."""
    msg = ((raw or {}).get("result") or {}).get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [p.get("text", "") for p in c
                 if isinstance(p, dict) and p.get("type") == "text"]
        return "".join(parts) if parts else None
    return None


def chat(messages, max_tokens: int = 1024, temperature: float = 0.1,
         model=None, request_id=None, extra=None) -> dict:
    """성공 {"ok":True,"text","raw","elapsed"} / 실패 {"ok":False,"error","status"}.

    어떤 입력에도 예외를 던지지 않는다. temperature 0.1 은 같은 질문에 같은 답이
    나와야 비교가 가능해서다.

    `extra` 는 요청 본문에 그대로 합쳐진다. 도구 호출(`tools`, `toolChoice`)이나
    추론 강도(`thinking`) 처럼 이 함수가 모르는 필드를 넘길 때 쓴다.
    도구 호출이 오면 `tool_calls` 키에 result.message.toolCalls 를 담아 돌려준다.
    """
    key, err = get_api_key()
    if err:
        return {"ok": False, "error": err, "status": None, "elapsed": 0.0}

    model = resolve_model(model)
    url = resolve_host() + PATH_FMT.format(model=model)
    tok_field = "maxCompletionTokens" if model in REASONING_MODELS else "maxTokens"
    try:
        body = {"messages": _norm_messages(messages),
                tok_field: int(max_tokens),
                "temperature": float(temperature)}
        if extra:
            body.update(dict(extra))
    except (KeyError, TypeError, ValueError) as e:
        return {"ok": False, "error": f"messages 형식 오류: {type(e).__name__}: {e}",
                "status": None, "elapsed": 0.0}

    delay = BACKOFF0
    for attempt in range(MAX_RETRY + 1):
        t0 = time.time()
        try:
            r = requests.post(url, headers=build_headers(key, request_id),
                              json=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            fail = {"ok": False, "status": None,
                    "elapsed": round(time.time() - t0, 2),
                    "error": f"요청 실패: {type(e).__name__}: {e}"}
            if attempt < MAX_RETRY:
                time.sleep(delay)
                delay *= 2
                continue
            return fail
        elapsed = round(time.time() - t0, 2)

        if r.status_code == 429 or r.status_code >= 500:
            fail = {"ok": False, "status": r.status_code, "elapsed": elapsed,
                    "error": f"HTTP {r.status_code} "
                             f"(시도 {attempt + 1}/{MAX_RETRY + 1}): {r.text[:500]}"}
            if attempt < MAX_RETRY:
                time.sleep(delay)
                delay *= 2
                continue
            return fail

        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "elapsed": elapsed,
                    "error": f"HTTP {r.status_code}: {r.text[:500]}"}

        try:
            raw = r.json()
        except ValueError:
            return {"ok": False, "status": r.status_code, "elapsed": elapsed,
                    "error": f"응답이 JSON 이 아니다: {r.text[:500]}"}

        code = str(((raw or {}).get("status") or {}).get("code", ""))
        if code and code != OK_CODE:
            msg = ((raw or {}).get("status") or {}).get("message", "")
            return {"ok": False, "status": r.status_code, "elapsed": elapsed,
                    "raw": raw, "error": f"CLOVA status {code}: {msg}"}

        result = (raw or {}).get("result") or {}
        tool_calls = ((result.get("message") or {}).get("toolCalls")) or []
        text = _extract_text(raw)
        # 도구 호출 응답은 content 가 빈 문자열이다. 그건 실패가 아니다.
        if text is None and not tool_calls:
            return {"ok": False, "status": r.status_code, "elapsed": elapsed,
                    "raw": raw, "error": "응답에서 result.message.content 를 찾지 못했다"}

        return {"ok": True, "text": text or "", "raw": raw, "elapsed": elapsed,
                "status": r.status_code, "model": model,
                "tool_calls": tool_calls,
                "finish_reason": result.get("finishReason"),
                "usage": result.get("usage")}

    return {"ok": False, "error": "재시도 소진", "status": None, "elapsed": 0.0}


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--ping", action="store_true", help="실제 호출 1건")
    ap.add_argument("--model", default=None,
                    help="비우면 CLOVA_MODEL 환경변수, 그것도 없으면 " + FALLBACK_MODEL)
    a = ap.parse_args()

    key, err = get_api_key()
    print(f"[key] 없음 — {err}" if err else f"[key] {mask_key(key)}")
    if err:
        return 2
    if not a.ping:
        return 0
    print(f"[model] {resolve_model(a.model)}  [host] {resolve_host()}")
    print("[headers]", safe_headers(build_headers(key)))
    res = chat([{"role": "user", "content": "안녕. 한 문장으로 답해라."}],
               max_tokens=64, model=a.model)
    print(f"[ok] {res['elapsed']}초 · {res['text']!r}" if res["ok"] else res)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
