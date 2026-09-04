# -*- coding: utf-8 -*-
"""e2e_v6 채점용 대조표 + 기계 집계. LLM 0회.

  python eval/report_v6.py

만드는 것
  reports/e2e_v6_scoring.md  — v5 와 같은 형식. 정오 판정은 사람이 한다.
  reports/e2e_v6_auto.md     — 기계 집계. v5 대비 + step39~42 계측을 추가한다.

동료 벤치의 정답 필드는 `ground_truth` 하나다(해설·반례가 그 안에 섞여 있고
그대로 옮긴다). report_v5.py 와 같은 규칙이며, 집계 항목만 늘렸다.
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

V6 = ROOT / "eval" / "e2e_runs" / "e2e_v6.jsonl"
V5 = ROOT / "eval" / "e2e_runs" / "e2e_v5.jsonl"
SCORING = ROOT / "reports" / "e2e_v6_scoring.md"
AUTO = ROOT / "reports" / "e2e_v6_auto.md"

_RCEPT = re.compile(r"(?<!\d)\d{14}(?!\d)")
_GEUN_HEAD = re.compile(r"^\s*(?:\*\*)?근거(?:\*\*)?\s*[:：]?\s*$"
                        r"|^\s*(?:\*\*)?근거(?:\*\*)?\s*[:：]", re.M)
_REFUSAL = ("확인되지 않", "확인할 수 없", "찾을 수 없", "답변할 수 없",
            "제공할 수 없", "알 수 없")
# 검문 종류 → 사람이 읽을 이름 (loop.waste["audit"] 값)
AUDIT_NAMES = {
    "calc": "검문1 비율·배수인데 calculate 미사용",
    "no_search": "검문2 조회 없이 거절",
    "all_bad": "검문4 도구가 전부 빗나감",
    "announced": "검문5 부르겠다 말만 함",
    "compare": "검문6 크기 비교인데 calculate 미사용",
    "subq": "검문7 하위 질문보다 검색이 적음",
    "unsourced_num": "검문8 답변 수치의 출처 없음",
}


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
    if a.get("ki"):
        bits.append("기수: " + "·".join(f"제{k}기" for k in a["ki"]))
    if a.get("time_note"):
        bits.append("시점: " + a["time_note"])
    return " / ".join(bits) or "(빈 분석)"


def used_ki(r: dict) -> bool:
    return any(isinstance(c.get("arguments"), dict) and c["arguments"].get("ki")
               for c in (r.get("tool_calls") or []))


def used_progress(r: dict) -> bool:
    return any(c.get("name") == "get_progress_table"
               for c in (r.get("tool_calls") or []))


def has_askback(r: dict) -> bool:
    a = r.get("answer") or ""
    return "확인 가능한 관련 정보" in a or ("드릴까요" in a and "알려주시면" in a)


def scoring_md(rows: list) -> str:
    L = ["# e2e_v6 채점용 대조표", "",
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
        w = r.get("waste") or {}
        marks = []
        if w.get("audit"):
            marks.append(f"검문 {AUDIT_NAMES.get(w['audit'], w['audit'])}")
        if w.get("audit_reverted"):
            marks.append("검문 후 되돌림")
        if w.get("placeholder_stripped"):
            marks.append("자리표시자 제거")
        if marks:
            L.append("")
            L.append("【계측】 " + " · ".join(marks))
        if r.get("error"):
            L.append(f"- ⚠ 문항 오류: {r['error']}")
        L.append("")
        L.append("【정오】 (   )   【유형】 (   )   【메모】 (   )")
        L.append("")
        L.append("---")
        L.append("")
    return "\n".join(L)


def auto_md(rows: list, prev: list) -> str:
    n = len(rows)
    pv = {r["qid"]: r for r in prev}

    def ids(xs):
        return " ".join(str(rows.index(x) + 1) for x in xs) if xs else "—"

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
    rw_fail = [r for r in rows if not (r.get("rewrite") or {}).get("ok")]
    errs = [r for r in rows if r.get("error")]
    reverted = [r for r in rows if (r.get("waste") or {}).get("audit_reverted")]
    ph = [r for r in rows if (r.get("waste") or {}).get("placeholder_stripped")]
    ki = [r for r in rows if used_ki(r)]
    prog = [r for r in rows if used_progress(r)]
    ask = [r for r in rows if has_askback(r)]
    changed = [i for i, r in enumerate(rows, 1)
               if r["qid"] in pv and norm(r.get("answer")) != norm(pv[r["qid"]].get("answer"))]
    same = [i for i, r in enumerate(rows, 1)
            if r["qid"] in pv and norm(r.get("answer")) == norm(pv[r["qid"]].get("answer"))]

    audits: dict[str, list] = {}
    for r in rows:
        a = (r.get("waste") or {}).get("audit") or ""
        if a:
            audits.setdefault(a, []).append(r["qid"])

    won = sum((r.get("cost") or 0) for r in rows)
    sec = sum((r.get("elapsed") or 0) for r in rows)
    llm = sum((r.get("usage") or {}).get("llm_calls", 0) for r in rows)

    L = ["# e2e_v6 자동 판정 (기계적으로 센 것만)", "",
         f"문항 {n}건 · 총 {won:,.0f}원 · {sec / 60:.1f}분 · LLM {llm}회", "",
         "| 항목 | v6 | v5 | 문항 번호 |", "|---|---|---|---|",
         f"| 근거 절 있음 | {len(has_geun)}/{n} | 38/40 | |",
         f"| 접수번호 있음 | {len(has_rcept)}/{n} | 38/40 | |",
         f"| 조회 범위 줄('조회:') | {len(has_scope)}/{n} | 0/40 | {ids(has_scope)} |",
         f"| 도구 0회 | {len(no_tool)} | 0 | {ids(no_tool)} |",
         f"| 60초 초과 | {len(over60)} | 2 | {ids(over60)} |",
         f"| 200초 초과 | {len(over200)} | 0 | {ids(over200)} |",
         f"| '확인되지 않음' 계열 | {len(refuse)} | 11 | {ids(refuse)} |",
         f"| 서두 군더더기 잔존 | {len(leadin)} | 0 | {ids(leadin)} |",
         f"| 근거 중복 | {len(dup)} | 0 | {ids(dup)} |",
         f"| 재작성 실패·생략 | {len(rw_fail)} | 3 | {ids(rw_fail)} |",
         f"| 문항 오류(error) | {len(errs)} | 0 | {ids(errs)} |", "",
         "## step39~42 신규 계측", "",
         "| 항목 | 수 | 문항 번호 |", "|---|---|---|",
         f"| 검문 후 되돌림(1-B) | {len(reverted)} | {ids(reverted)} |",
         f"| 자리표시자 제거(1-B c) | {len(ph)} | {ids(ph)} |",
         f"| ki 환산 발동(1-A) | {len(ki)} | {ids(ki)} |",
         f"| get_progress_table 호출(2-C) | {len(prog)} | {ids(prog)} |",
         f"| 역질문 붙음(3-F) | {len(ask)} | {ids(ask)} |", "",
         "## 검문 발동 내역 (1~8)", ""]
    if audits:
        L.append("| 검문 | 수 | qid |")
        L.append("|---|---|---|")
        for k in ("calc", "no_search", "all_bad", "announced", "compare",
                  "subq", "unsourced_num"):
            if k in audits:
                L.append(f"| {AUDIT_NAMES[k]} | {len(audits[k])} | "
                         f"{', '.join(audits[k])} |")
        for k, v in audits.items():          # 목록 밖의 새 종류가 생기면 여기에
            if k not in AUDIT_NAMES:
                L.append(f"| {k} | {len(v)} | {', '.join(v)} |")
        L.append(f"| **합계** | **{sum(len(v) for v in audits.values())}** | |")
    else:
        L.append("발동 0건")
    L += ["", "## v5 대비", "",
          f"- 답변이 **바뀐** 문항: {len(changed)}/{n} — "
          + (" ".join(str(i) for i in changed) if changed else "없음"),
          f"- 답변이 **같은** 문항: {len(same)}/{n} — "
          + (" ".join(str(i) for i in same) if same else "없음"), ""]
    return "\n".join(L)


def main() -> int:
    if not V6.is_file():
        print(f"[중단] {V6} 가 없다", file=sys.stderr)
        return 2
    rows = load(V6)
    prev = load(V5) if V5.is_file() else []
    SCORING.parent.mkdir(parents=True, exist_ok=True)
    SCORING.write_text(scoring_md(rows), encoding="utf-8", newline="\n")
    auto = auto_md(rows, prev)
    AUTO.write_text(auto, encoding="utf-8", newline="\n")
    print(f"[대조표] {SCORING} — {len(rows)}문항")
    print(f"[자동표] {AUTO}")
    print()
    print(auto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
