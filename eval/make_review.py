"""e2e 실행 결과를 사람이 판정할 표로 만든다. **채점은 하지 않는다. LLM 도 안 부른다.**

  python eval/make_review.py --run e2e_v4 --prev e2e_v2

【왜 채점하지 않는가】
자동 채점을 한 번 넣었다가 폐기했다. 벤치 정답 문자열에 "※ 오답 유형: … 를 답하면
오답" 형태의 **반례 숫자**가 잔뜩 들어 있어, 답변과 정답의 숫자 일치율을 재면
반례가 분모에 섞여 점수가 오염된다(40문항에서 일치율 0.5 초과가 1건뿐이었다).
그래서 이 파일은 **맞고 틀림을 한 글자도 적지 않는다.** 사람이 채울 칸만 만든다.

【기계적으로 셀 수 있는 것만 따로 센다】
근거 절 유무 · 접수번호 포함 · 도구 0회 · 42초 초과 · "확인되지 않음" 계열 ·
직전 실행 대비 답변이 바뀐 문항 · 오류로 실패한 문항. 전부 문자열 검사라 다툼이 없다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

RUNDIR = ROOT / "eval" / "e2e_runs"
REPORTS = ROOT / "reports"

MAX_SECONDS = 42.0
_RCEPT = re.compile(r"(?<!\d)\d{14}(?!\d)")
_GEUN = re.compile(r"\n[ \t]*(?:\*\*)?근거(?:\*\*)?[ \t]*:?[ \t]*\n")
_REFUSE = ("확인되지 않", "확인할 수 없", "확인 불가", "찾을 수 없")
_WS = re.compile(r"\s+")


def load(name: str) -> list:
    p = RUNDIR / f"{name}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def cell(s: str) -> str:
    """표 칸에 넣을 수 있게. 줄바꿈은 <br>, 파이프는 이스케이프."""
    return _WS.sub(" ", (s or "").replace("|", "\\|")).strip()


def two_lines(text: str, width: int = 110) -> str:
    """답변 요약 2줄 — 앞부분을 그대로 자른다. 요약을 지어내지 않는다."""
    flat = _WS.sub(" ", (text or "").split("\n근거\n")[0]).strip()
    if not flat:
        return "(빈 답변)"
    a, b = flat[:width], flat[width:width * 2]
    return cell(a) + ("<br>" + cell(b) + ("…" if len(flat) > width * 2 else "") if b else "")


def tools_of(r: dict) -> str:
    calls = r.get("tool_calls") or []
    if not calls:
        return "**0회**"
    seq = []
    for c in calls:
        nm = {"search_disclosure": "search", "list_disclosures": "list",
              "calculate": "calc", "resolve_company": "resolve"}.get(c.get("name"),
                                                                    str(c.get("name")))
        seq.append(nm + ("" if c.get("status") == "ok" else f"({c.get('status')})"))
    return f"{len(calls)}회: " + " → ".join(seq)


def docs_of(r: dict) -> str:
    srcs = r.get("sources") or []
    if not srcs:
        return "없음"
    out = []
    for s in srcs[:4]:
        inner = str(s).strip().lstrip("[").rstrip("]")
        head = inner.split(",")[0].strip()
        m = _RCEPT.search(inner)
        out.append(f"{head}{' ' + m.group(0) if m else ''}")
    return cell("<br>".join(out) + (f"<br>… 외 {len(srcs) - 4}건" if len(srcs) > 4 else ""))


def body_of(r: dict) -> str:
    """근거 절을 뗀 답변 본문. 답변 변화를 볼 때 근거 형식 변경에 휘둘리지 않게."""
    return _WS.sub(" ", _GEUN.split(r.get("answer") or "")[0]).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="이번 실행 이름 (e2e_v4)")
    ap.add_argument("--prev", default=None, help="비교할 직전 실행 이름 (e2e_v2)")
    # 기본 경로에 사람이 이미 판정을 채워 넣은 파일이 있을 수 있다(e2e_v2_review.md).
    # 덮어쓰기 전에 확인을 받으려면 --out 으로 다른 이름을 준다.
    ap.add_argument("--out", default=None, help="출력 경로 (기본 reports/<run>_review.md)")
    a = ap.parse_args()

    rows = load(a.run)
    if not rows:
        print(f"[중단] {RUNDIR / (a.run + '.jsonl')} 이 없거나 비었다", file=sys.stderr)
        return 2
    prev = {r["qid"]: r for r in load(a.prev)} if a.prev else {}
    if a.prev and not prev:
        print(f"[주의] 직전 실행 {a.prev} 을 찾지 못했다 — 변화 열은 '비교 없음' 이 된다",
              file=sys.stderr)

    rows = sorted(rows, key=lambda x: (x.get("qtype", ""), x.get("qid", "")))

    # ------------------------------------------------- 기계로 셀 수 있는 것
    stat = {"근거절": [], "접수번호": [], "도구0회": [], "42초초과": [],
            "확인되지않음": [], "답변변화": [], "비교없음": [], "실패": []}
    for i, r in enumerate(rows, 1):
        ans = r.get("answer") or ""
        tag = f"{i}({r['qid']})"
        if _GEUN.search(ans):
            stat["근거절"].append(tag)
        if _RCEPT.search(ans):
            stat["접수번호"].append(tag)
        if not (r.get("tool_calls") or []):
            stat["도구0회"].append(tag)
        if float(r.get("elapsed") or 0) > MAX_SECONDS:
            stat["42초초과"].append(tag)
        if any(w in ans for w in _REFUSE):
            stat["확인되지않음"].append(tag)
        if r.get("error"):
            stat["실패"].append(tag)
        p = prev.get(r["qid"])
        if p is None:
            stat["비교없음"].append(tag)
        elif body_of(p) != body_of(r):
            stat["답변변화"].append(tag)

    n = len(rows)
    cost = sum(float(r.get("cost") or (r.get("usage") or {}).get("est_won") or 0)
               for r in rows)
    sec = sum(float(r.get("elapsed") or 0) for r in rows)
    llm = sum(int((r.get("usage") or {}).get("llm_calls") or 0) for r in rows)

    L = [f"# e2e 사람 판정 — {a.run}", "",
         f"문항 **{n}건** · 총 소요 {sec / 60:.1f}분 · LLM {llm}회 · **{cost:,.0f}원**",
         "",
         "> **이 파일에는 정오 판정이 한 글자도 없다.** 자동 채점은 폐기했다 — 벤치 정답의",
         "> `※ 오답 유형:` 에 반례 숫자가 섞여 있어 기계가 정답으로 오인한다.",
         "> 아래 표의 채점란은 **사람이** 채운다.", ""]

    # ------------------------------------------------- 자동 판정 표
    L += ["## 기계로 센 것 (다툼 없는 것만)", "",
          "| 항목 | 건수 | 문항 |", "|---|---|---|"]
    for k, label in (("근거절", "근거 절이 있다"), ("접수번호", "답변에 접수번호 14자리가 있다"),
                     ("도구0회", "도구 호출 0회"), ("42초초과", "42초 초과"),
                     ("확인되지않음", "'확인되지 않음' 계열"),
                     ("답변변화", f"직전 실행({a.prev or '없음'}) 대비 답변이 바뀜"),
                     ("비교없음", "직전 실행에 없던 문항"),
                     ("실패", "오류로 실패")):
        v = stat[k]
        shown = ", ".join(x.split("(")[0] for x in v) if v else "—"
        L.append(f"| {label} | {len(v)}/{n} | {shown} |")
    L += ["", "번호는 아래 표의 행 번호다. `qid` 는 각 행에 있다.", ""]

    if stat["실패"]:
        L += ["### 실패한 문항의 오류", ""]
        for r in rows:
            if r.get("error"):
                L.append(f"- `{r['qid']}` — {cell(r['error'])}")
        L.append("")

    # ------------------------------------------------- 본 표
    L += ["## 문항별", "",
          "| # | 질문 | 답변 요약(2줄) | 인용 문서 | 도구 호출 | 소요 | 답변 변화 |",
          "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        p = prev.get(r["qid"])
        chg = "비교 없음" if p is None else ("**있음**" if body_of(p) != body_of(r) else "없음")
        # 질문은 표에서 잘라 읽기 좋게 한다. 전문은 아래 채점란에 그대로 있다.
        q = cell(r.get("question"))
        q = q if len(q) <= 130 else q[:130] + "…"
        L.append(f"| {i} | {q} | {two_lines(r.get('answer'))} | "
                 f"{docs_of(r)} | {tools_of(r)} | {float(r.get('elapsed') or 0):.0f}초 | "
                 f"{chg} |")
    L.append("")

    # ------------------------------------------------- 채점란
    L += ["## 채점란", "",
          "각 문항 아래 괄호를 채운다. **정오 판정은 사람만 한다.**", ""]
    for i, r in enumerate(rows, 1):
        L += [f"### {i}. `{r['qid']}` · {r.get('qtype', '')}", "",
              f"질문 — {cell(r.get('question'))}", "",
              "정오( )　실패유형( )　메모( )", ""]

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = Path(a.out) if a.out else (REPORTS / f"{a.run}_review.md")
    out.write_text("\n".join(L), encoding="utf-8", newline="\n")

    print(f"문항 {n}건 · {cost:,.0f}원 · {sec / 60:.1f}분 · LLM {llm}회")
    for k in stat:
        print(f"  {k:12s} {len(stat[k]):2d}건")
    print(f"[review] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
