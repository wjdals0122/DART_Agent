"""Agent 대화 루프. 질문 하나를 받아 도구를 부르고 답을 만든다.

  from src.agent.loop import answer
  answer("SK하이닉스 2024년 연결 매출액이 얼마야?")

【이 파일의 계약 — 어떤 경우에도 지킨다】
반환은 언제나 아래 모양이다. LLM 이 죽든, 도구가 죽든, 시간이 다 되든 마찬가지다.

  {"answer": str, "sources": [...], "tool_calls": [...], "elapsed": float}

`answer` 는 비지 않는다. 최악의 경우에도 "공시에서 확인되지 않음" 이 들어간다.
평가 중 예외가 나면 그 문항은 0점이라 여기서 전부 막는다.

【HyperCLOVA X 도구 왕복 — 실측한 것】
  요청  extra={"tools": [...], "toolChoice": "auto"}   · maxTokens >= 1024 필수
  응답  result.message.toolCalls[].{id, type, function:{name, arguments}}
        finishReason == "tool_calls"
  되돌려주기
        {"role":"assistant","content":"","toolCalls":[받은 toolCall 그대로]}
        {"role":"tool","toolCallId":"call_...","name":"함수명","content":"결과 문자열"}

**arguments 는 JSON 문자열이 아니라 객체로 온다.** OpenAI 와 다르다.
json.loads() 를 걸면 터진다. 그래도 문자열로 올 경우를 대비해 isinstance 로 가른다.

【무한루프·폭주 막기】
- 도구 호출 누적 6회. 넘으면 실행하지 않고 "한도 초과" 를 도구 결과로 돌려준다
- 전체 60초. 넘으면 tools 없이 한 번 더 불러 **강제로 답을 받아낸다**
- 같은 (도구, 인자) 재호출은 캐시를 주고 "이미 조회함" 을 붙인다.
  이게 없으면 못 찾은 질문에서 같은 검색을 끝없이 반복한다
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"
TOOLS_JSON = ROOT / "config" / "tools.json"

MAX_TOOL_CALLS = 6
DEADLINE = 60.0
MAX_TOKENS = 2048          # 도구 호출은 1024 이상이어야 한다(문서 명시)
TEMPERATURE = 0.1
FALLBACK = "공시에서 확인되지 않음"

# 도구 결과를 다 넣은 직후 딱 한 번 끼워 넣는 형식 리마인더.
#
# 【왜 필요한가】
# 시스템 프롬프트가 4,000자인데 도구 결과가 그 뒤로 12,000자쯤 쌓인다. 답을 쓸 때쯤이면
# 형식 규칙이 멀어져서, 실측으로 (1) 사칙연산을 calculate 없이 암산하고
# (2) 근거 절을 빠뜨리거나 대괄호를 떼먹었다. 가까이에서 한 번 더 말해 준다.
#
# 【왜 이 자리인가】
# "모델이 답변만 내놓으려는 턴" 은 불러 보기 전엔 알 수 없다. 그걸 알아내려면
# 한 번 부르고 버린 뒤 다시 부르는 셈이라 LLM 호출이 두 배가 된다.
# 도구 결과가 들어온 직후면 다음 턴이 십중팔구 답변 턴이고, 아니어도 손해가 없다
# (모델이 도구를 더 부르면 루프는 그냥 계속 돈다).
REMINDER = "\n".join([
    "위 자료만으로 답하라.",
    "사칙연산이 필요하면 암산하지 말고 calculate 도구를 부른다.",
])

# 도구를 한 번도 안 부르고 바로 답하려는 경우에만 한 번 더 던지는 검토 지시.
# 거절 답변에 '공개되지 않을 가능성이 높습니다', '관련 기관에 문의하세요' 같은
# 공시 근거 없는 일반 지식이 붙는 걸 막는다. 시스템 프롬프트에 같은 규칙을 넣어
# 봤지만 지켜지지 않았다(실측 2/2). 답을 본 뒤에 지적해야 먹힌다.
REVIEW = (
    "위 답변을 검토하라. 공시에서 확인되지 않는다고 밝힌 뒤에는 그 이유를 추측해 "
    "설명하지 마라. '공개되지 않을 가능성이 높습니다', '관련 기관에 문의하세요', "
    "'내부 회의록에서 확인할 수 있습니다' 같은 문장은 공시 근거가 없는 일반 지식이므로 "
    "삭제하라. 확인되지 않는 항목은 한 문장으로 밝히고 끝낸다. "
    "질문의 다른 항목 중 공시에서 확인되는 것이 있으면 그것만 이어서 답하라. "
    "고쳐 쓴 답변 **본문만** 출력하라. '죄송합니다', '말씀하신 대로', "
    "'수정된 답변은 다음과 같습니다', '다른 질문이 있으시면' 같은 말머리·맺음말을 "
    "붙이지 마라. 검토했다는 사실 자체를 언급하지 마라."
)

# 답변 끝의 '근거' 목록을 찾아내는 규칙. 줄 머리가 '근거' 이고 그 아래가
# '-' 나 '*' 로 시작하는 목록인 구간만 잡는다. 산문형 '근거: …' 는 건드리지 않는다.
_GEUN = re.compile(r"\n[ \t]*(?:\*\*)?근거(?:\*\*)?[ \t]*:?[ \t]*\n"
                   r"(?:[ \t]*[-*][^\n]*\n?)+\s*$")
_RCEPT = re.compile(r"\d{14}")



def _read(name: str) -> str:
    p = PROMPTS / name
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


def build_system() -> str:
    """planner(계획 규칙) 를 앞에, system(답변 규칙) 을 뒤에."""
    planner, system = _read("planner.txt"), _read("system.txt")
    if not system:
        system = _read("system_min.txt")
    head = "당신은 DART 전자공시를 근거로 답하는 분석 어시스턴트다."
    body = system[len(head):].lstrip() if system.startswith(head) else system
    return f"{head}\n\n{planner}\n\n{body}".strip()


def load_tools() -> list:
    try:
        return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))["tools"]
    except (OSError, ValueError, KeyError) as e:
        print(f"[loop] {TOOLS_JSON} 를 읽지 못했다 — 도구 없이 진행한다 "
              f"({type(e).__name__}: {e})", file=sys.stderr)
        return []


def warmup() -> float:
    """검색 인덱스를 미리 올린다. 걸린 초를 돌려준다.

    첫 검색은 인덱스·임베딩 모델을 올리느라 35초쯤 걸린다. 그건 답변 시간이 아니라
    기동 시간이다. 데드라인 안에 그게 들어가면 첫 질문만 억울하게 잘린다.
    부르는 쪽에서 answer() 전에 한 번 불러 준다.
    """
    t = time.time()
    try:
        from src.search.hybrid import get_searcher
        get_searcher()
    except Exception as e:                     # noqa: BLE001 — 예열 실패해도 답은 해야 한다
        print(f"[loop] 예열 실패({type(e).__name__}: {e}). 첫 검색이 느릴 것이다",
              file=sys.stderr)
    return round(time.time() - t, 2)


# ---------------------------------------------------------------- 도구 결과 -> 문자열


def _args_of(call: dict) -> tuple:
    """(인자 dict, 오류메시지). arguments 는 보통 객체로 오지만 문자열도 대비한다."""
    fn = (call or {}).get("function") or {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw, None
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except ValueError as e:
            return {}, f"인자 파싱 실패: {e}"
        return (v, None) if isinstance(v, dict) else ({}, f"인자가 객체가 아니다: {type(v).__name__}")
    return {}, f"인자 형식을 모르겠다: {type(raw).__name__}"


def _render(name: str, out: dict) -> str:
    """도구 결과를 LLM 이 읽을 문자열로. 검색 결과는 build_context 로 조립한다."""
    head = f'status={out.get("status")}'
    note = out.get("note") or ""
    if note:
        head += f" · {note}"

    if name == "search_disclosure" and out.get("results"):
        try:
            from src.agent.context import build_context
            body = build_context(out["results"])
        except Exception as e:                 # noqa: BLE001 — 조립이 실패해도 답은 해야 한다
            traceback.print_exc(file=sys.stderr)
            body = f"(컨텍스트 조립 실패: {type(e).__name__}: {e})"
        return f"{head}\n\n{body}"

    slim = {k: v for k, v in out.items() if k not in ("results", "note", "status", "pool")}
    if out.get("results"):
        slim["results"] = out["results"][:30]
    return f"{head}\n{json.dumps(slim, ensure_ascii=False)[:6000]}"


def _summary(name: str, out: dict) -> str:
    """사람이 한 줄로 볼 요약."""
    if name == "calculate":
        return f"value={out.get('value')}"
    if name == "list_disclosures":
        return f"total={out.get('total')} · {out.get('by_correction')}"
    n = len(out.get("results") or [])
    extra = f" · 후보 {out['n_candidates']:,}" if out.get("n_candidates") else ""
    return f"{n}건{extra}"


def strip_sources_section(text: str) -> str:
    """모델이 쓴 '근거' 목록을 떼어낸다. 형식이 흔들리므로 우리가 다시 붙인다."""
    prev = None
    out = text or ""
    while prev != out:                     # 여러 번 붙였을 수도 있다
        prev = out
        out = _GEUN.sub("\n", out).rstrip()
    return out


def used_sources(text: str, sources: list) -> list:
    """답변이 실제로 인용한 출처만 고른다.

    접수번호(14자리)가 답변 본문에 있으면 '썼다' 로 본다. 하나도 안 맞으면
    판단을 포기하고 전부 붙인다 — 근거가 없는 것보다 많은 게 낫다.
    """
    seen = _RCEPT.findall(text or "")
    used = [s for s in sources if any(n in s for n in seen)]
    return used or list(sources)


def drop_baseless_geun(text: str) -> str:
    """출처가 하나도 없는데 '근거:' 라고 주장하는 문단을 잘라낸다.

    거절 답변에서 모델이 '근거: 상법 제391조에 따라…', '근거: 기업 내부 의사결정
    과정의 보안을 위해…' 처럼 **라벨만 근거이고 내용은 일반 지식**인 문단을 붙인다.
    프롬프트로 세 번 막아 봤지만 표현만 바꿔 가며 계속 나왔다(실측).
    도구가 돌려준 출처가 하나도 없으면 '근거' 라고 부를 것 자체가 없으므로 여기서 끊는다.
    """
    paras = re.split(r"\n\s*\n", text or "")
    kept = [p for p in paras
            if not re.match(r"^\s*(?:\*\*)?근거(?:\*\*)?\s*[:：]", p)]
    return "\n\n".join(kept).strip() or (text or "").strip()


def attach_sources(text: str, sources: list) -> tuple:
    """근거 절을 **파이썬이** 만들어 붙인다. (본문, 붙인 출처 목록).

    LLM 에게 맡겼더니 출처가 둘 이상일 때 대괄호를 떼먹었다(실측 2/2).
    형식이 흔들리면 채점에서 샌다. 문자열은 fmt_source() 가 만든 것을 그대로 쓴다.
    """
    body = strip_sources_section(text)
    if not sources:
        # 붙일 출처가 없다 = 도구가 아무것도 못 가져왔다. 그런 답에 '근거' 는 없다
        return drop_baseless_geun(body), []
    picked, out = used_sources(body, sources), []
    for s in picked:                       # 순서를 지키며 중복 제거
        if s not in out:
            out.append(s)
    lines = "\n".join(f"- {s}" for s in out)
    return f"{body}\n\n근거\n{lines}", out


# ---------------------------------------------------------------- 본체


def answer(question, model=None, max_tool_calls=MAX_TOOL_CALLS,
           deadline=DEADLINE, verbose=False) -> dict:
    """질문 하나에 답한다. 어떤 경우에도 규격 dict 를 돌려준다."""
    t0 = time.time()
    out = {"answer": "", "sources": [], "tool_calls": [], "elapsed": 0.0,
           "llm_seconds": 0.0, "tool_seconds": 0.0, "messages": [], "note": ""}
    try:
        out = _run(question, out, t0, model, max_tool_calls, deadline, verbose)
    except Exception as e:                     # noqa: BLE001 — 마지막 방어선
        traceback.print_exc(file=sys.stderr)
        out["note"] = f"루프 실패: {type(e).__name__}: {e}"
    if not (out.get("answer") or "").strip():
        out["answer"] = FALLBACK + (f" (사유: {out['note']})" if out.get("note") else "")
    # 근거 절은 모델이 아니라 여기서 만든다. 형식이 흔들리면 안 된다
    try:
        out["answer"], out["sources"] = attach_sources(out["answer"], out.get("sources") or [])
    except Exception as e:                     # noqa: BLE001 — 붙이다 죽으면 안 된다
        traceback.print_exc(file=sys.stderr)
        out["note"] = (out.get("note") or "") + f" · 근거 절 조립 실패: {e}"
    out["elapsed"] = round(time.time() - t0, 2)
    return out


def _run(question, out, t0, model, max_calls, deadline, verbose) -> dict:
    from src.agent import clova
    from src.tools import TOOLS

    tools = load_tools()
    msgs = [{"role": "system", "content": build_system()},
            {"role": "user", "content": str(question)}]
    cache: dict = {}
    n_calls = 0
    reminded = False           # 리마인더는 딱 한 번만 넣는다
    reviewed = False           # 도구 없이 답하는 경로의 검토도 딱 한 번만

    while True:
        over_time = (time.time() - t0) > deadline
        extra = None if (over_time or not tools) else {"tools": tools, "toolChoice": "auto"}
        if over_time and not out["note"]:
            out["note"] = f"{deadline:.0f}초를 넘겨 도구 없이 마지막 답변을 받았다"

        t1 = time.time()
        res = clova.chat(msgs, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
                         model=model, extra=extra)
        out["llm_seconds"] += time.time() - t1
        if not res.get("ok"):
            out["note"] = f"LLM 호출 실패: {res.get('error')}"
            break

        calls = res.get("tool_calls") or []
        if not calls:
            text = (res.get("text") or "").strip()
            # 도구를 한 번도 안 부르고 바로 답하려는 경우에만 검토를 한 번 시킨다.
            # 이 경로는 리마인더가 닿지 않아 거절 답변에 추측이 붙는다(실측).
            if extra is not None and not reviewed and not out["tool_calls"]:
                reviewed = True
                msgs.append({"role": "assistant", "content": text})
                msgs.append({"role": "user", "content": REVIEW})
                continue
            out["answer"] = text
            break
        if extra is None:
            out["answer"] = (res.get("text") or "").strip()
            break

        # assistant 가 무엇을 불렀는지 그대로 되돌려 놓아야 tool 메시지가 이어진다
        msgs.append({"role": "assistant", "content": res.get("text") or "",
                     "toolCalls": calls})

        for call in calls:
            name = ((call.get("function") or {}).get("name")) or ""
            args, err = _args_of(call)
            n_calls += 1

            if err:
                tool_out = {"status": "error", "note": err,
                            "results": [], "sources": []}
            elif n_calls > max_calls:
                tool_out = {"status": "error", "results": [], "sources": [],
                            "note": "호출 한도 초과. 지금까지의 근거로 답변할 것"}
            elif name not in TOOLS:
                tool_out = {"status": "error", "results": [], "sources": [],
                            "note": f"그런 도구가 없다: {name}. "
                                    f"쓸 수 있는 것: {', '.join(TOOLS)}"}
            else:
                key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True,
                                        default=str))
                if key in cache:
                    # 캐시 '결과' 를 다시 주면 안 된다. 같은 문서를 또 보여주면 모델이
                    # "아직 못 찾았다" 로 읽고 같은 호출을 반복한다(실측: 6회 중 2회 낭비).
                    # 결과 없이 error 로 끊어야 다음 단계로 넘어간다.
                    tool_out = {"status": "error", "results": [], "sources": [],
                                "note": "이미 같은 인자로 조회했다. 그 결과는 위 대화에 "
                                        "그대로 있다. 다시 부르지 말고, 다른 검색어·다른 "
                                        "시점으로 바꾸거나 지금까지의 근거로 답하라"}
                else:
                    t2 = time.time()
                    tool_out = TOOLS[name](**args)
                    out["tool_seconds"] += time.time() - t2
                    cache[key] = tool_out

            for s in (tool_out.get("sources") or []):
                if s not in out["sources"]:
                    out["sources"].append(s)
            out["tool_calls"].append({
                "n": n_calls, "name": name, "arguments": args,
                "status": tool_out.get("status"),
                "summary": _summary(name, tool_out),
                "note": tool_out.get("note") or ""})

            msgs.append({"role": "tool", "toolCallId": call.get("id") or "",
                         "name": name, "content": _render(name, tool_out)})

        if not reminded:
            # 도구 호출 횟수에는 넣지 않는다. 이건 메시지일 뿐이다
            msgs.append({"role": "user", "content": REMINDER})
            reminded = True

        if n_calls >= max_calls:
            # 다음 턴에는 도구를 주지 않는다 — 있는 근거로 답하게 한다
            tools = []
            if not out["note"]:
                out["note"] = f"도구 호출 {max_calls}회 한도에 도달해 답변 단계로 넘어갔다"

    if verbose:
        out["messages"] = msgs
    return out
