"""회귀 확인 — 정답이 확정된 문항만 돌려 답변에 그 문자열이 있는지 본다.

  python eval/regression.py --baseline     # 기준선을 만든다(처음 한 번)
  python eval/regression.py                # 기준선과 비교하며 돌린다
  python eval/regression.py --quick        # 1·3·5번만 (수정 중 빠른 확인)

【채점을 정교하게 만들지 않는다】
포함 여부만 본다. 숫자 하나가 답변 문자열 안에 있는가, 없는가. 그뿐이다.
자동 채점은 이 프로젝트에서 한 번 폐기했다(벤치 정답에 "…를 답하면 오답" 형태의
반례 숫자가 섞여 분모가 오염된다). 그래서 **정답이 사람 손으로 확정된 6문항만**
넣고, 그 문항의 핵심 숫자 하나만 본다. 여기서 PASS 이던 것이 FAIL 로 바뀌면
그 수정은 되돌린다 — 그게 이 파일의 유일한 쓸모다.

콤마 형태와 콤마 없는 형태를 둘 다 인정한다("66,192,960" / "66192960").
모델이 콤마를 빼고 쓰는 일이 있는데 그건 회귀가 아니다. 그 외의 관용은 없다.

【공통 검사】
전 문항에 네 가지를 함께 본다. 계약이 깨지면 답이 맞아도 평가에서 잃는다.
  - 답변에 '근거' 절이 있는가
  - 근거에 접수번호 14자리(8자리 접수일 + 6자리 일련번호)가 있는가
  - 도구 호출이 6회 이하인가          (loop.MAX_TOOL_CALLS)
  - 소요시간이 42초 이하인가          (주최측 60초 - 여유 18초)

6번(거절 문항)은 근거를 달 것이 없어 접수번호 검사에서 떨어질 수 있다.
그건 기준선에 FAIL 로 적히고 그대로 둔다. **기준선은 '지금 상태' 이지 '목표' 가 아니다.**
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

REPORTS = ROOT / "reports"
BASELINE_JSON = REPORTS / "regression_baseline.json"
BASELINE_MD = REPORTS / "regression_baseline.md"
LAST_JSON = REPORTS / "regression_last.json"
LAST_MD = REPORTS / "regression_last.md"

# 429 는 이 프로젝트에서 가장 흔한 실패다. 연속 호출 사이 20초(run_e2e 와 같은 값).
DEFAULT_SLEEP = 20.0
MAX_TOOL_CALLS = 6
MAX_SECONDS = 42.0

# 접수번호 14자리. 앞뒤에 숫자가 더 붙으면 접수번호가 아니다.
_RCEPT = re.compile(r"(?<!\d)\d{14}(?!\d)")
# 거절 표현. 하나라도 있으면 "확인되지 않음" 계열로 본다.
_REFUSE = ("확인되지 않", "확인할 수 없", "확인 불가", "찾을 수 없", "없습니다", "없음")
# 적자 표현. 숫자가 음수임을 드러내는 표기 전부.
_LOSS = ("손실", "적자", "마이너스", "-7,730,313", "(7,730,313)", "△7,730,313", "△")


def has_num(text: str, num: str) -> bool:
    """콤마 있는 형태와 없는 형태를 둘 다 인정한다. 그 외 관용 없음."""
    return num in text or num.replace(",", "") in (text or "").replace(",", "")


# 답변 끝의 '근거' 절 본문만 떼어낸다(loop._GEUN 과 같은 모양).
_GEUN_SEC = re.compile(r"\n[ \t]*(?:\*\*)?근거(?:\*\*)?[ \t]*:?[ \t]*\n(.*)$", re.S)
# 조회 범위로 인정하는 표현. 접수번호가 있으면 그것이 곧 조회한 문서다.
_SCOPE = ("0건", "없음", "해당 조건", "조회")


def scope_in_geun(text: str) -> bool:
    """근거 절에 '무엇을 조회했는지' 가 적혀 있는가.

    접수번호 14자리가 있으면 조회한 문서가 적힌 것이다. 아무것도 못 찾은 경우에는
    "해당 조건의 공시 0건" 같은 문장이 있어야 한다.
    """
    m = _GEUN_SEC.search(text or "")
    if not m:
        return False
    body = m.group(1)
    return bool(_RCEPT.search(body)) or any(w in body for w in _SCOPE)


# ---------------------------------------------------------------- 문항
#
# `check(answer, row) -> (bool, 실패사유)`. 정답이 사람 손으로 확정된 것만 넣는다.


def c1(a, r):
    return has_num(a, "66,192,960"), "66,192,960 없음"


def c2(a, r):
    return has_num(a, "300,870,903"), "300,870,903 없음"


def c3(a, r):
    if not has_num(a, "57,846"):
        return False, "57,846 없음"
    if has_num(a, "57,824"):
        return False, "57,824(오답값)가 섞였다"
    return True, ""


def c4(a, r):
    if not has_num(a, "7,730,313"):
        return False, "7,730,313 없음"
    if not any(w in a for w in _LOSS):
        return False, "적자 표현 없음 — 흑자로 읽었을 수 있다"
    return True, ""


def c5(a, r):
    """두 값 + 배수. 도구 횟수는 보지 않는다.

    기준선에서는 "도구 3회 이상" 을 봤는데, 실제로는 2회(회사별 검색 1회씩)로
    두 값을 정확히 가져왔다. 기대치가 과했던 것이라 뺐다. 대신 **비교 결과**를 본다 —
    차액 234,677,943백만원을 "234.73% 더 많다" 로 쓴 오류가 실제로 났다. 4.55배가 맞다.
    """
    miss = [n for n in ("300,870,903", "66,192,960") if not has_num(a, n)]
    if miss:
        return False, f"{'·'.join(miss)} 없음"
    if not (has_num(a, "4.55") or has_num(a, "4.5")):
        return False, "배수 4.5/4.55 가 없다 — 비교 결과가 틀렸을 수 있다"
    return True, ""


def c6(a, r):
    """이사회 반대·기권 사례를 이름까지 짚어야 한다. 거절하면 오답.

    【기대값을 2026-09-03 에 바꿨다】
    처음에는 "확인되지 않음 계열" 을 정답으로 잡았다. **틀렸다.**
    정기공시(사업보고서)의 「VI. 이사회 등 회사의 기관에 관한 사항」에 이사회
    안건별 찬반·기권이 실제로 기재돼 있다. 실측으로 김준성 사외이사의 반대,
    허은녕 사외이사의 기권이 답변에 나왔다. 거절은 오답이다.
    """
    if any(w in a for w in ("확인되지 않", "확인할 수 없")):
        return False, "거절했다 — 정기공시에 이사회 찬반·기권이 기재돼 있다"
    if not any(w in a for w in ("반대", "기권")):
        return False, "'반대'·'기권' 표현 없음"
    names = [n for n in ("김준성", "허은녕") if n in a]
    if not names:
        return False, "이사 이름(김준성·허은녕)이 하나도 없음"
    if r["n_tool_calls"] < 1:
        return False, "도구 0회 — 조회하지 않았다"
    return True, ""


def c7(a, r):
    """정정공시의 확정치를 답했는가. 정정 전 값이 섞이면 실패."""
    if not has_num(a, "644"):
        return False, "644 없음"
    if has_num(a, "583"):
        return False, "583(정정 전 값)이 섞였다"
    return True, ""


CASES = [
    {"n": 1, "name": "SK하이닉스 2024 연결 매출액", "quick": True,
     "q": "SK하이닉스 2024년 연결 매출액이 얼마야?",
     "want": "66,192,960", "check": c1},
    {"n": 2, "name": "삼성전자 2024 연결 매출액", "quick": False,
     "q": "삼성전자 2024년 연결 매출액이 얼마야?",
     "want": "300,870,903", "check": c2},
    {"n": 3, "name": "KB금융 2025 자기자본", "quick": True,
     "q": "KB금융 2025년 자기자본이 얼마야?",
     "want": "57,846 포함 · 57,824 불포함", "check": c3},
    {"n": 4, "name": "SK하이닉스 2023 영업손익", "quick": False,
     "q": "SK하이닉스 2023년 연결 영업손익이 얼마야?",
     "want": "7,730,313 + 적자 표현", "check": c4},
    {"n": 5, "name": "삼성전자·SK하이닉스 2024 매출 비교", "quick": True,
     "q": "삼성전자와 SK하이닉스의 2024년 연결 매출액을 비교해줘.",
     "want": "두 값 모두 + 배수 4.5/4.55", "check": c5},
    {"n": 6, "name": "삼성전자 이사회 반대 이사", "quick": False,
     "q": "삼성전자 이사회에서 반대한 이사가 있었나?",
     "want": "반대/기권 + 이사 이름(김준성·허은녕) + 거절 아님 + 도구 1회 이상",
     "check": c6},
    {"n": 7, "name": "HD현대일렉트릭 고압차단기 계약 확정금액", "quick": False,
     "q": "HD현대일렉트릭이 2024년 1월 30일 체결한 고압차단기 공급계약의 "
          "최종 계약금액은?",
     "want": "644 포함 · 583 불포함", "check": c7},
]


# ---------------------------------------------------------------- 공통 검사


def common_checks(a: str, r: dict) -> list:
    """(이름, 통과여부, 사유) 목록. 전 문항 공통."""
    out = []
    out.append(("근거절", "근거" in a, "'근거' 절 없음"))
    out.append(("접수번호", bool(_RCEPT.search(a)), "접수번호 14자리 없음"))
    out.append(("도구≤6", r["n_tool_calls"] <= MAX_TOOL_CALLS,
                f"도구 {r['n_tool_calls']}회"))
    out.append(("≤42초", r["elapsed"] <= MAX_SECONDS, f"{r['elapsed']:.1f}초"))
    return out


# ---------------------------------------------------------------- 실행


def run(cases: list, sleep: float, model, deadline: float) -> list:
    from src.agent import usage as U
    from src.agent.loop import answer, warmup

    print("[warmup] 검색 인덱스 올리는 중…", flush=True)
    print(f"[warmup] {warmup():.1f}초 (답변 시간에 포함되지 않는다)", flush=True)

    rows = []
    for i, c in enumerate(cases, 1):
        U.set_context(script="regression", qid=f"reg{c['n']}")
        t0 = time.time()
        res = answer(c["q"], model=model, deadline=deadline,
                     max_tool_calls=MAX_TOOL_CALLS, qid=f"reg{c['n']}")
        a = res.get("answer") or ""
        row = {
            "n": c["n"], "name": c["name"], "question": c["q"], "want": c["want"],
            "answer": a,
            "sources": res.get("sources") or [],
            "n_tool_calls": len(res.get("tool_calls") or []),
            "tool_names": [t.get("name") for t in (res.get("tool_calls") or [])],
            "elapsed": round(time.time() - t0, 2),
            "usage": res.get("usage") or {},
            "note": res.get("note") or "",
        }
        ok, why = c["check"](a, row)
        row["content_ok"] = bool(ok)
        row["content_why"] = "" if ok else why
        row["common"] = [{"name": n, "ok": bool(o), "why": "" if o else w}
                         for n, o, w in common_checks(a, row)]
        bad = ([row["content_why"]] if not ok else []) + \
              [c2_["why"] for c2_ in row["common"] if not c2_["ok"]]
        row["pass"] = not bad
        row["why"] = " · ".join(bad)
        rows.append(row)

        u = row["usage"]
        print(f"[{i}/{len(cases)}] {c['n']}. {c['name']}  "
              f"{'PASS' if row['pass'] else 'FAIL'}  "
              f"도구 {row['n_tool_calls']}회 · {row['elapsed']:.0f}초 · "
              f"{U.fmt_won(u.get('est_won'))}"
              + (f"  ← {row['why']}" if not row["pass"] else ""), flush=True)

        if i < len(cases) and sleep > 0:
            time.sleep(sleep)
    return rows


# ---------------------------------------------------------------- 표


def table(rows: list, base: dict | None) -> str:
    L = []
    head = ["#", "문항", "내용", "근거절", "접수번호", "도구≤6", "≤42초", "판정"]
    if base:
        head.append("기준선 대비")
    head.append("사유")
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "|".join(["---"] * len(head)) + "|")

    for r in rows:
        cm = {c["name"]: c["ok"] for c in r["common"]}
        mark = lambda b: "O" if b else "X"          # noqa: E731
        cells = [str(r["n"]), r["name"], mark(r["content_ok"]),
                 mark(cm["근거절"]), mark(cm["접수번호"]),
                 mark(cm["도구≤6"]), mark(cm["≤42초"]),
                 "**PASS**" if r["pass"] else "**FAIL**"]
        if base:
            was = base.get(str(r["n"]))
            if was is None:
                cells.append("(기준선에 없음)")
            elif was == r["pass"]:
                cells.append("같음")
            elif was and not r["pass"]:
                cells.append("**회귀 — 되돌릴 것**")
            else:
                cells.append("개선")
        cells.append(r["why"] or "—")
        L.append("| " + " | ".join(cells) + " |")
    return "\n".join(L)


def report(rows: list, base: dict | None, quick: bool) -> str:
    from src.agent import usage as U

    n_pass = sum(1 for r in rows if r["pass"])
    sec = sum(r["elapsed"] for r in rows)
    llm = sum((r["usage"] or {}).get("llm_calls", 0) for r in rows)
    won = sum((r["usage"] or {}).get("est_won", 0.0) or 0.0 for r in rows)
    pin = sum((r["usage"] or {}).get("prompt", 0) for r in rows)
    pout = sum((r["usage"] or {}).get("completion", 0) for r in rows)

    L = ["# 회귀 확인" + (" (--quick)" if quick else ""), ""]
    L.append(f"실행 {time.strftime('%Y-%m-%dT%H:%M:%S')} · 문항 {len(rows)}건 · "
             f"**{n_pass}/{len(rows)} PASS**")
    L.append("")
    L.append(table(rows, base))
    L.append("")
    L.append(f"- 총 소요시간 **{sec:.1f}초** (문항 답변 시간 합. 워밍업·대기 제외)")
    L.append(f"- 총 LLM 호출 **{llm}회** · 입력 {pin:,} + 출력 {pout:,} 토큰 · "
             f"{U.fmt_won(won)}")

    regress = [r["n"] for r in rows if base and base.get(str(r["n"])) and not r["pass"]]
    if regress:
        L.append("")
        L.append(f"**회귀 {len(regress)}건: {regress} — 이 수정은 되돌린다.**")

    L.append("")
    L.append("## 답변 원문")
    for r in rows:
        L.append("")
        L.append(f"### {r['n']}. {r['name']} — {'PASS' if r['pass'] else 'FAIL'}")
        L.append("")
        L.append(f"질문: {r['question']}")
        L.append("")
        L.append(f"기대: `{r['want']}`")
        L.append("")
        L.append(f"도구 {r['n_tool_calls']}회 {r['tool_names']} · {r['elapsed']:.1f}초")
        L.append("")
        L.append("```")
        L.append((r["answer"] or "(빈 답변)").strip())
        L.append("```")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="1·3·5번만")
    ap.add_argument("--baseline", action="store_true",
                    help="결과를 기준선으로 저장한다")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                    help="문항 사이 대기(초). 429 방지")
    ap.add_argument("--model", default=None)
    ap.add_argument("--deadline", type=float, default=50.0)
    a = ap.parse_args()

    cases = [c for c in CASES if c["quick"]] if a.quick else CASES
    print(f"[회귀] {len(cases)}문항" + (" (--quick: 1·3·5)" if a.quick else ""))

    base = None
    if BASELINE_JSON.exists() and not a.baseline:
        b = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
        base = {str(r["n"]): r["pass"] for r in b["rows"]}
        print(f"[회귀] 기준선 {BASELINE_JSON.name} 와 비교한다 "
              f"({sum(base.values())}/{len(base)} PASS)")
    elif a.baseline:
        print("[회귀] 이번 결과를 **기준선**으로 저장한다")
    else:
        print("[회귀] 기준선이 없다. --baseline 으로 먼저 만들어라")

    rows = run(cases, a.sleep, a.model, a.deadline)

    md = report(rows, base, a.quick)
    REPORTS.mkdir(parents=True, exist_ok=True)
    payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "quick": a.quick,
               "rows": rows}
    if a.baseline:
        BASELINE_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        BASELINE_MD.write_text(md, encoding="utf-8")
        out = BASELINE_MD
    else:
        LAST_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        LAST_MD.write_text(md, encoding="utf-8")
        out = LAST_MD

    print()
    print(table(rows, base))
    print()
    n_pass = sum(1 for r in rows if r["pass"])
    sec = sum(r["elapsed"] for r in rows)
    llm = sum((r["usage"] or {}).get("llm_calls", 0) for r in rows)
    print(f"{n_pass}/{len(rows)} PASS · 총 {sec:.1f}초 · LLM {llm}회")
    print(f"[회귀] {out}")

    regress = [r["n"] for r in rows if base and base.get(str(r["n"])) and not r["pass"]]
    if regress:
        print(f"[회귀] **회귀 {len(regress)}건: {regress}**")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
