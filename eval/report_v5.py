# -*- coding: utf-8 -*-
"""e2e_v5 채점용 대조표 + 기계 집계. LLM 0회.

  python eval/report_v5.py

만드는 것
  reports/e2e_v5_scoring.md  — 문항별 대조표. 정오 판정은 사람이 한다(자동 채점 폐기).
  reports/e2e_v5_auto.md     — 기계적으로 셀 수 있는 것만 모은 표.

동료 벤치의 정답 필드는 `ground_truth` 하나다(240문항 전부 query+ground_truth,
2026-09-04 확인). 해설·반례("…를 답하면 오답")가 그 문자열 안에 섞여 있는데
**그대로 옮긴다** — 요약하면 채점자가 반례를 못 본다.
"""
from __future__ import annotations

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

V5 = ROOT / "eval" / "e2e_runs" / "e2e_v5.jsonl"
V4 = ROOT / "eval" / "e2e_runs" / "e2e_v4.jsonl"
SCORING = ROOT / "reports" / "e2e_v5_scoring.md"
AUTO = ROOT / "reports" / "e2e_v5_auto.md"

_RCEPT = re.compile(r"(?<!\d)\d{14}(?!\d)")
_GEUN_HEAD = re.compile(r"^\s*(?:\*\*)?근거(?:\*\*)?\s*[:：]?\s*$|^\s*(?:\*\*)?근거(?:\*\*)?\s*[:：]", re.M)
_REFUSAL = ("확인되지 않", "확인할 수 없", "찾을 수 없", "답변할 수 없",
            "제공할 수 없", "알 수 없")


def load(path: Path) -> list:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def leadin_residual(ans: str) -> bool:
    """서두 군더더기가 최종 답변에 남았나 — loop.strip_leadin 과 같은 판정."""
    from src.agent.loop import LEADIN_TAIL, LEADIN_WORDS
    body = (ans or "").strip()
    if not body:
        return False
    first = body.split("\n")[0].strip()
    if not any(w in first[:60] for w in LEADIN_WORDS):
        return False
    naked = first.rstrip().rstrip(".。:：!？? ").rstrip()
    return first.rstrip().endswith((":", "：")) or naked.endswith(LEADIN_TAIL)


def geun_dup(ans: str) -> bool:
    """'근거' 머리가 두 번 이상 나오나(중복 근거 절·산문형 잔존)."""
    return len(_GEUN_HEAD.findall(ans or "")) >= 2


def fmt_rewrite(r: dict) -> str:
    rw = r.get("rewrite") or {}
    if not rw.get("ok"):
        return f"(생략/실패 — {rw.get('note') or '기록 없음'})"
    a = rw.get("analysis") or {}
    bits = []
    if a.get("rewritten"):
        bits.append(a["rewritten"])
    if a.get("companies"):
        bits.append("회사: " + "·".join(a["companies"]))
    if a.get("fiscal_years"):
        bits.append("회계연도: " + "·".join(str(y) for y in a["fiscal_years"]))
    if a.get("time_note"):
        bits.append("시점: " + a["time_note"])
    return " / ".join(bits) or "(빈 분석)"


def scoring_md(rows: list) -> str:
    L = ["# e2e_v5 채점용 대조표", "",
         "정오·유형·메모는 **사람이** 채운다. 자동 채점은 폐기했다(분모 오염).",
         "【동료 정답】은 벤치 `ground_truth` 필드 원문 그대로다 — '…를 답하면 오답'",
         "같은 반례가 섞여 있으니 숫자만 훑지 말 것.", ""]
    for i, r in enumerate(rows, 1):
        L.append(f"## [{i}] {r['qid']}")
        L.append("")
        L.append(f"【질문】 {r['question']}")
        L.append("")
        L.append(f"【재작성】 {fmt_rewrite(r)}")
        L.append("")
        L.append("【우리 답변】")
        L.append("")
        L.append("```")
        L.append((r.get("answer") or "(빈 답변)").strip())
        L.append("```")
        L.append("")
        srcs = r.get("sources") or []
        L.append("【우리 근거】" + ("" if srcs else " (없음)"))
        for s in srcs:
            L.append(f"- {s}")
        L.append("")
        L.append("【동료 정답】")
        L.append("")
        L.append("```")
        L.append((r.get("ground_truth") or "(정답 기록 없음)").strip())
        L.append("```")
        L.append("")
        L.append("【도구 호출】" + ("" if r.get("tool_calls") else " (0회)"))
        for c in (r.get("tool_calls") or []):
            args = json.dumps(c.get("arguments") or {}, ensure_ascii=False, default=str)
            L.append(f"- {c.get('n')}. {c.get('name')}({args}) → {c.get('status')}")
        if r.get("error"):
            L.append(f"- ⚠ 문항 오류: {r['error']}")
        L.append("")
        L.append("【정오】 (   )   【유형】 (   )   【메모】 (   )")
        L.append("")
        L.append("---")
        L.append("")
    return "\n".join(L)


def auto_md(rows: list, v4rows: list) -> str:
    n = len(rows)
    v4 = {r["qid"]: r for r in v4rows}

    has_geun = [r for r in rows if "근거" in (r.get("answer") or "")]
    has_rcept = [r for r in rows if _RCEPT.search(r.get("answer") or "")]
    has_scope = [r for r in rows if "조회:" in (r.get("answer") or "")]
    no_tool = [r for r in rows if not r.get("n_tool_calls")]
    over60 = [r for r in rows if (r.get("elapsed") or 0) > 60]
    over200 = [r for r in rows if (r.get("elapsed") or 0) > 200]
    refuse = [r for r in rows
              if any(w in (r.get("answer") or "") for w in _REFUSAL)]
    leadin = [r for r in rows if leadin_residual(r.get("answer") or "")]
    dup = [r for r in rows if geun_dup(r.get("answer") or "")]
    audits: dict[str, list] = {}
    for r in rows:
        a = (r.get("waste") or {}).get("audit") or ""
        if a:
            audits.setdefault(a, []).append(r["qid"])
    rw_fail = [r for r in rows if not (r.get("rewrite") or {}).get("ok")]
    errs = [r for r in rows if r.get("error")]
    changed = [i for i, r in enumerate(rows, 1)
               if r["qid"] in v4 and norm(r.get("answer")) != norm(v4[r["qid"]].get("answer"))]
    not_in_v4 = [r["qid"] for r in rows if r["qid"] not in v4]

    def ids(xs):
        return " ".join(str(rows.index(x) + 1) for x in xs) if xs else "—"

    won = sum((r.get("cost") or 0) for r in rows)
    sec = sum((r.get("elapsed") or 0) for r in rows)
    llm = sum((r.get("usage") or {}).get("llm_calls", 0) for r in rows)

    L = ["# e2e_v5 자동 판정 (기계적으로 센 것만)", "",
         f"문항 {n}건 · 총 {won:,.0f}원 · {sec / 60:.1f}분 · LLM {llm}회", "",
         "| 항목 | 수 | 문항 번호 |", "|---|---|---|",
         f"| 근거 절 있음 | {len(has_geun)}/{n} | |",
         f"| 접수번호 있음 | {len(has_rcept)}/{n} | |",
         f"| 조회 범위 줄('조회:') | {len(has_scope)}/{n} | {ids(has_scope)} |",
         f"| 도구 0회 | {len(no_tool)} | {ids(no_tool)} |",
         f"| 60초 초과 | {len(over60)} | {ids(over60)} |",
         f"| 200초 초과 | {len(over200)} | {ids(over200)} |",
         f"| '확인되지 않음' 계열 포함 | {len(refuse)} | {ids(refuse)} |",
         f"| 서두 군더더기 잔존 | {len(leadin)} (v4: 15) | {ids(leadin)} |",
         f"| 근거 중복 | {len(dup)} (v4: 3) | {ids(dup)} |",
         f"| 재작성 실패·생략 | {len(rw_fail)} | {ids(rw_fail)} |",
         f"| 문항 오류(error) | {len(errs)} | {ids(errs)} |", ""]
    L.append("## 검문 발동 (waste.audit)")
    L.append("")
    if audits:
        L.append("| 종류 | 수 | qid |")
        L.append("|---|---|---|")
        for k, v in sorted(audits.items()):
            L.append(f"| {k} | {len(v)} | {', '.join(v)} |")
    else:
        L.append("발동 0건")
    L.append("")
    L.append("## v4 대비")
    L.append("")
    L.append(f"- 답변이 바뀐 문항: **{len(changed)}/{n}** — "
             + (" ".join(str(i) for i in changed) if changed else "없음"))
    if not_in_v4:
        L.append(f"- v4 에 없는 qid: {not_in_v4}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    if not V5.is_file():
        print(f"[중단] {V5} 가 없다", file=sys.stderr)
        return 2
    rows = load(V5)
    v4rows = load(V4) if V4.is_file() else []
    SCORING.parent.mkdir(parents=True, exist_ok=True)
    SCORING.write_text(scoring_md(rows), encoding="utf-8", newline="\n")
    auto = auto_md(rows, v4rows)
    AUTO.write_text(auto, encoding="utf-8", newline="\n")
    print(f"[대조표] {SCORING} — {len(rows)}문항")
    print(f"[자동표] {AUTO}")
    print()
    print(auto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
