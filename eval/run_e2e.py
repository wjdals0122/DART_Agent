"""e2e 평가 — 벤치 문항을 Agent 에 끝에서 끝까지 태우고 토큰·비용을 기록한다.

  # 1) 예상치만 본다 (LLM 안 부름)
  python eval/run_e2e.py --n 40
  # 2) 실제 실행
  python eval/run_e2e.py --n 40 --yes
  # 3) 상한을 걸고
  python eval/run_e2e.py --n 40 --yes --max-cost 3000
  # 4) 중단된 지점부터 이어서
  python eval/run_e2e.py --n 40 --yes --resume

【안전장치】
- `--yes` 없이는 **예상 비용만 출력하고 끝난다.** 실수로 전량을 돌리는 사고를 막는다
- `--max-cost`(기본 5,000원)를 넘으면 즉시 멈춘다. 문항 경계가 아니라
  **LLM 호출 직전**에 확인하므로 문항 중간에도 선다(`src/agent/loop.py`)
- 결과는 문항마다 곧바로 `eval/e2e_runs/<run>.jsonl` 에 append 한다.
  중간에 죽어도 거기까지는 남고 `--resume` 이 이어서 간다
- 호출 하나하나는 `eval/token_log.jsonl` 에도 남는다(`eval/token_log.py --summary`)

【채점은 여기서 하지 않는다】
자동 채점 방식이 아직 정해지지 않았다. 이 스크립트는 **답변과 정답을 나란히 저장**하고,
기계적으로 셀 수 있는 것 하나만 힌트로 남긴다 — 정답에 나오는 숫자가 답변에 몇 개
들어 있는지(`numbers_hit`). **이것은 채점이 아니다.** 서술형은 숫자가 없거나 무의미하다.
`verdict` 는 비워 둔다. 사람이나 심판 모델이 나중에 채운다.

【문항 원본】
`Question/질문_(수정해서 다시 할 것)/benchmark_*.json`
  {company, doc_info, scenarios: {유형: {query, ground_truth}}}
`benchmark_master_templates_24.json` 은 실제 문항이 아니라 템플릿이라 제외한다.
내용이 완전히 같은 파일 쌍(`...(1).json`)은 하나만 쓴다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from src.agent import usage as U  # noqa: E402

QDIR = ROOT / "Question" / "질문_(수정해서 다시 할 것)"
RUNDIR = ROOT / "eval" / "e2e_runs"
REPORTS = ROOT / "reports"
TEMPLATE_FILE = "benchmark_master_templates_24.json"
SCRIPT = "run_e2e"
BAR = "=" * 78

# 429 는 이 프로젝트에서 가장 흔한 실패다. 연속 호출 사이 20~30초가 안전하다(실측).
DEFAULT_SLEEP = 20.0
DEFAULT_MAX_COST = 5000.0

_NUM = re.compile(r"\d[\d,]{2,}(?:\.\d+)?")


# ---------------------------------------------------------------- 문항 적재


def load_items() -> tuple:
    """(문항 목록, 경고 목록). 파일이 없거나 모양이 다르면 지어내지 않고 알린다."""
    warn = []
    if not QDIR.is_dir():
        return [], [f"문항 폴더가 없다: {QDIR}"]

    seen_hash: dict = {}
    items = []
    for f in sorted(QDIR.glob("benchmark_*.json")):
        if f.name == TEMPLATE_FILE:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            warn.append(f"{f.name} 을 읽지 못했다: {type(e).__name__}: {e}")
            continue
        if not isinstance(d, dict) or "scenarios" not in d:
            warn.append(f"{f.name} 은 모양이 다르다(scenarios 없음) — 건너뛴다")
            continue

        h = hashlib.md5(json.dumps(d, ensure_ascii=False,
                                   sort_keys=True).encode("utf-8")).hexdigest()
        if h in seen_hash:
            warn.append(f"{f.name} 은 {seen_hash[h]} 와 내용이 같다 — 하나만 쓴다")
            continue
        seen_hash[h] = f.name

        corp = str(d.get("company") or "").strip()
        if not corp:
            warn.append(f"{f.name} 에 company 가 없다 — 건너뛴다")
            continue

        sc = d.get("scenarios")
        if not isinstance(sc, dict):
            warn.append(f"{f.name} 의 scenarios 가 dict 가 아니다 — 건너뛴다")
            continue
        for qtype, body in sc.items():
            if not isinstance(body, dict):
                warn.append(f"{f.name}:{qtype} 모양이 다르다 — 건너뛴다")
                continue
            q = (body.get("query") or "").strip()
            gt = (body.get("ground_truth") or "").strip()
            if not q:
                warn.append(f"{f.name}:{qtype} 에 query 가 없다 — 건너뛴다")
                continue
            items.append({"qid": f"{corp}__{qtype}", "corp": corp, "qtype": qtype,
                          "question": q, "ground_truth": gt,
                          "doc_info": d.get("doc_info") or "", "src_file": f.name})
    return items, warn


def sample(items: list, n, types=None, corps=None, seed=20260903) -> list:
    """유형이 고루 섞이게 뽑는다. 유형별로 돌아가며 한 건씩 가져간다."""
    import random

    pool = list(items)
    if types:
        want = set(types)
        pool = [x for x in pool if x["qtype"] in want]
    if corps:
        want = set(corps)
        pool = [x for x in pool if x["corp"] in want]
    if not n or n >= len(pool):
        return pool

    rnd = random.Random(seed)
    by: dict = defaultdict(list)
    for x in pool:
        by[x["qtype"]].append(x)
    for v in by.values():
        rnd.shuffle(v)

    out, keys = [], sorted(by)
    while len(out) < n:
        added = False
        for k in keys:
            if by[k] and len(out) < n:
                out.append(by[k].pop())
                added = True
        if not added:
            break
    return out


# ---------------------------------------------------------------- 힌트(채점 아님)


def numbers_hit(answer: str, ground_truth: str) -> dict:
    """정답에 나오는 숫자가 답변에 몇 개 있는지. **채점이 아니라 힌트다.**

    3자리 이상 숫자만 본다(연도·한 자리 수는 우연히 맞는다).
    서술형은 숫자가 없으므로 total=0 이 되고, 그건 '틀렸다' 가 아니라 '못 잰다' 이다.
    """
    def norm(s):
        return {m.group(0).replace(",", "") for m in _NUM.finditer(s or "")}

    want = norm(ground_truth)
    got = norm(answer)
    hit = want & got
    return {"total": len(want), "hit": len(hit),
            "ratio": round(len(hit) / len(want), 3) if want else None}


# ---------------------------------------------------------------- 실행


def preflight(n_items: int, max_cost: float, avg_tokens) -> dict:
    """예상 토큰·비용. 실측 평균이 있으면 그것을, 없으면 기본값을 쓴다."""
    measured = avg_tokens is not None
    per = avg_tokens if measured else U.DEFAULT_TOKENS_PER_ITEM
    total = int(per * n_items)
    return {"n": n_items, "per": per, "total": total, "won": U.won(total),
            "measured": measured, "max_cost": max_cost}


def print_preflight(pf: dict) -> None:
    src = ("token_log.jsonl 실측 평균" if pf["measured"]
           else f"기본 가정 {U.DEFAULT_TOKENS_PER_ITEM:,}토큰 — 실측 이력이 아직 없다")
    print(f"{BAR}\n실행 전 예상치\n{BAR}")
    print(f"  {pf['n']}문항 × 평균 {pf['per']:,.0f} 토큰 예상 "
          f"= {pf['total']:,} 토큰 ≈ {U.fmt_won(pf['won'])}")
    print(f"  근거: {src}")
    print(f"  상한: {U.fmt_won(pf['max_cost'])}")
    if pf["won"] > pf["max_cost"]:
        print(f"  ※ 예상치가 상한을 넘는다. 상한에 걸려 도중에 멈출 것이다 "
              f"(--max-cost 로 올리거나 --n 을 줄여라)")
    print(f"\n  이 숫자는 추정이다 — {U.TOKENS_PER_UNIT:,} 토큰 = "
          f"{U.fmt_won(U.WON_PER_UNIT)} 환산(확인된 요금표 아님)")


def done_qids(path: Path) -> set:
    out = set()
    if not path.is_file():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.add(json.loads(line).get("qid"))
            except ValueError:
                continue
    return {q for q in out if q}


def run(items: list, out_path: Path, budget, sleep: float, model, deadline: float,
        max_tool_calls: int) -> list:
    from src.agent.loop import answer, warmup

    print(f"[warmup] 검색 인덱스 올리는 중…", flush=True)
    print(f"[warmup] {warmup():.1f}초 (답변 시간에 포함되지 않는다)")

    rows = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for i, it in enumerate(items, 1):
        if budget.exceeded():
            print(f"\n[중단] 비용 상한 초과 — {budget.line()}")
            print(f"[중단] {i - 1}/{len(items)} 문항까지 했다. "
                  f"--resume 으로 이어서 갈 수 있다")
            break

        U.set_context(script=SCRIPT, qid=it["qid"])
        t0 = time.time()
        res = answer(it["question"], model=model, deadline=deadline,
                     max_tool_calls=max_tool_calls, qid=it["qid"])
        row = {
            "qid": it["qid"], "corp": it["corp"], "qtype": it["qtype"],
            "question": it["question"], "ground_truth": it["ground_truth"],
            "src_file": it["src_file"],
            "answer": res.get("answer") or "",
            "sources": res.get("sources") or [],
            "tool_calls": [{k: c.get(k) for k in ("n", "name", "status", "summary")}
                           for c in (res.get("tool_calls") or [])],
            "n_tool_calls": len(res.get("tool_calls") or []),
            "usage": res.get("usage") or {},
            "waste": res.get("waste") or {},
            "stopped_by_budget": bool(res.get("stopped_by_budget")),
            "note": res.get("note") or "",
            "elapsed": round(time.time() - t0, 2),
            "numbers_hit": numbers_hit(res.get("answer") or "", it["ground_truth"]),
            "verdict": None,        # 사람/심판이 채운다. 여기서 채점하지 않는다
        }
        rows.append(row)
        with open(out_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        u = row["usage"]
        print(f"[{i}/{len(items)}] {it['qid']}  "
              f"도구 {row['n_tool_calls']}회 · {u.get('total', 0):,} 토큰 · "
              f"{row['elapsed']:.0f}초 · 누적 {U.fmt_won(budget.spent_won)}"
              + ("  ← 상한으로 중단됨" if row["stopped_by_budget"] else ""))

        if row["stopped_by_budget"]:
            print(f"\n[중단] 비용 상한 초과 — {budget.line()}")
            break
        if i < len(items) and sleep > 0:
            time.sleep(sleep)
    return rows


# ---------------------------------------------------------------- 리포트


def report(rows: list, budget, run_name: str, pf: dict) -> str:
    L = [f"# e2e 평가 — {run_name}", ""]
    L.append(f"실행 {time.strftime('%Y-%m-%dT%H:%M:%S')} · 문항 {len(rows)}건")
    L.append("")

    tok = [r["usage"].get("total", 0) for r in rows]
    total_tok = sum(tok)
    unknown = sum(r["usage"].get("unknown_calls", 0) for r in rows)
    llm_calls = sum(r["usage"].get("llm_calls", 0) for r in rows)

    L += ["## 토큰·비용", "",
          f"- 총 {total_tok:,} 토큰 · **{U.fmt_won(U.won(total_tok))} 추정**",
          f"- LLM 호출 {llm_calls}회 · 문항당 평균 "
          f"{(total_tok / len(rows)):,.0f} 토큰" if rows else "- (문항 없음)",
          f"- 상한 {U.fmt_won(budget.limit_won)} · 남은 예산 "
          f"{U.fmt_won(budget.remaining_won())}" if budget.limit_won else "- 상한 없음",
          f"- 실행 전 예상 {pf['total']:,} 토큰 → 실제 {total_tok:,} 토큰 "
          f"({(total_tok / pf['total'] * 100):.0f}%)" if pf["total"] else "",
          ""]
    if unknown:
        L += [f"- ⚠ 토큰을 알 수 없는 호출 {unknown}건(응답에 usage 없음). "
              f"0 으로 세지 않았으므로 위 합계는 **하한**이다", ""]
    L += [f"환산 기준 {U.TOKENS_PER_UNIT:,} 토큰 = {U.fmt_won(U.WON_PER_UNIT)}. "
          f"**확인된 요금표가 아니라 사람이 정한 기준이다.**", ""]

    # 토큰 상위 5
    L += ["## 토큰을 많이 쓴 문항 상위 5", "",
          "| 문항 | 유형 | 토큰 | 추정 | 도구 | LLM |", "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: -x["usage"].get("total", 0))[:5]:
        t = r["usage"].get("total", 0)
        L.append(f"| {r['qid']} | {r['qtype']} | {t:,} | "
                 f"{U.fmt_won(U.won(t))} | {r['n_tool_calls']} | "
                 f"{r['usage'].get('llm_calls', 0)} |")
    L.append("")

    # 낭비 신호
    w = Counter()
    for r in rows:
        ww = r.get("waste") or {}
        w["repeat_blocked"] += ww.get("repeat_blocked", 0)
        w["retry_after_bad"] += ww.get("retry_after_bad", 0)
        w["hit_call_limit"] += 1 if ww.get("hit_call_limit") else 0
        w["retry_429"] += ww.get("retry_429", 0)
        w["http_retry"] += ww.get("http_retry", 0)
        w["retry_wait_s"] += ww.get("retry_wait_s", 0.0)
        w["resent"] += ww.get("resent_prompt_tokens_est", 0)
    L += ["## 낭비 신호", "",
          "| 항목 | 값 | 무슨 뜻인가 |", "|---|---|---|",
          f"| 재호출 차단 | {w['repeat_blocked']} | 같은 인자로 또 불렀다. "
          f"많으면 모델이 검색어를 안 바꾸는 것 |",
          f"| error/not_found 뒤 재시도 | {w['retry_after_bad']} | 스스로 고쳐 다시 불렀다. "
          f"많으면 첫 시도의 검색어·필터가 나쁘다 |",
          f"| 도구 6회 한도 도달 문항 | {w['hit_call_limit']}/{len(rows)} | "
          f"많으면 계획이 헤맨다 |",
          f"| 429 재시도 | {w['retry_429']} | 대기 {w['retry_wait_s']:,.0f}초. "
          f"요청이 거절된 것이라 낭비 토큰은 **측정 불가** |",
          f"| 전체 HTTP 재시도 | {w['http_retry']} | 429 + 5xx + 네트워크 |",
          f"| 다시 보낸 프롬프트 토큰(추정) | {w['resent']:,} | 재시도가 같은 본문을 "
          f"다시 보냈다. 서버가 과금했는지는 알 수 없다 |",
          ""]

    # 유형별
    by: dict = defaultdict(list)
    for r in rows:
        by[r["qtype"]].append(r)
    L += ["## 유형별", "",
          "| 유형 | 문항 | 평균 토큰 | 평균 도구 | 평균 초 | 근거 0건 |",
          "|---|---|---|---|---|---|"]
    for k in sorted(by):
        g = by[k]
        L.append(f"| {k} | {len(g)} | "
                 f"{sum(x['usage'].get('total', 0) for x in g) / len(g):,.0f} | "
                 f"{sum(x['n_tool_calls'] for x in g) / len(g):.1f} | "
                 f"{sum(x['elapsed'] for x in g) / len(g):.0f} | "
                 f"{sum(1 for x in g if not x['sources'])} |")
    L.append("")

    L += ["## 채점", "",
          "`verdict` 는 비어 있다. 이 스크립트는 채점하지 않는다 — 자동 채점 방식이",
          "아직 정해지지 않았다. `numbers_hit` 은 정답의 숫자가 답변에 몇 개 들어 있는지를",
          "센 것으로 **닫힌 문항의 힌트일 뿐 점수가 아니다**(서술형은 숫자가 없다).", "",
          "| 문항 | 유형 | 숫자 일치 | 근거 | 비고 |", "|---|---|---|---|---|"]
    for r in rows:
        nh = r["numbers_hit"]
        ratio = "-" if nh["ratio"] is None else f"{nh['hit']}/{nh['total']}"
        L.append(f"| {r['qid']} | {r['qtype']} | {ratio} | {len(r['sources'])} | "
                 f"{(r['note'] or '')[:40]} |")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e 평가 (토큰·비용 상한 포함)")
    ap.add_argument("--n", type=int, default=40, help="문항 수 (0=전량)")
    ap.add_argument("--types", nargs="*", default=None,
                    help="유형 필터 (search_closed search_open multi_closed …)")
    ap.add_argument("--corps", nargs="*", default=None, help="회사 필터")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--yes", action="store_true",
                    help="없으면 예상 비용만 출력하고 끝낸다")
    ap.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST,
                    help=f"추정 비용 상한(원). 기본 {DEFAULT_MAX_COST:.0f}")
    ap.add_argument("--resume", action="store_true",
                    help="같은 이름의 run 에서 이미 끝낸 문항을 건너뛴다")
    ap.add_argument("--run", default=None, help="run 이름 (기본: 날짜시각)")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                    help=f"문항 사이 대기 초. 429 때문에 기본 {DEFAULT_SLEEP:.0f}")
    ap.add_argument("--model", default=None)
    ap.add_argument("--deadline", type=float, default=60.0)
    ap.add_argument("--max-tool-calls", type=int, default=6)
    a = ap.parse_args()

    items, warn = load_items()
    for m in warn:
        print(f"[문항] {m}")
    if not items:
        print("[중단] 문항을 하나도 읽지 못했다", file=sys.stderr)
        return 2
    print(f"[문항] 총 {len(items)}건 · 회사 {len({x['corp'] for x in items})} · "
          f"유형 {dict(Counter(x['qtype'] for x in items))}")

    picked = sample(items, a.n, a.types, a.corps, a.seed)

    run_name = a.run or ("e2e_" + time.strftime("%Y%m%d_%H%M%S"))
    out_path = RUNDIR / f"{run_name}.jsonl"
    if a.resume:
        if not a.run:
            print("[중단] --resume 은 --run 으로 이어갈 run 이름을 줘야 한다",
                  file=sys.stderr)
            return 2
        already = done_qids(out_path)
        before = len(picked)
        picked = [x for x in picked if x["qid"] not in already]
        print(f"[resume] {out_path.name} 에 이미 {len(already)}건. "
              f"{before} → {len(picked)}건만 돈다")

    print(f"[표본] {len(picked)}건 · {dict(Counter(x['qtype'] for x in picked))}")
    if not picked:
        print("[끝] 돌 문항이 없다")
        return 0

    pf = preflight(len(picked), a.max_cost, U.avg_tokens_per_item(script=SCRIPT))
    print_preflight(pf)

    if not a.yes:
        print(f"\n{BAR}")
        print("실제 호출은 하지 않았다. 진행하려면 --yes 를 붙여라.")
        print(f"  python eval/run_e2e.py --n {a.n} --yes "
              f"--max-cost {a.max_cost:.0f}"
              + (f" --run {run_name}" if a.run else ""))
        print(BAR)
        return 0

    from src.agent import clova
    key, err = clova.get_api_key()
    if err:
        print(f"[중단] {err}", file=sys.stderr)
        return 2
    print(f"\n[key] {clova.mask_key(key)} · [model] {clova.resolve_model(a.model)}")
    print(f"[log] 호출 로그 {U.LOG_PATH} · 결과 {out_path}")

    budget = U.Budget(limit_won=a.max_cost)
    U.use_budget(budget)
    U.set_context(script=SCRIPT)

    t0 = time.time()
    rows = run(picked, out_path, budget, a.sleep, a.model, a.deadline,
               a.max_tool_calls)

    # 이어서 돌린 경우 리포트는 그 run 전체를 대상으로 낸다
    all_rows = []
    if out_path.is_file():
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        all_rows.append(json.loads(line))
                    except ValueError:
                        pass
    md = report(all_rows or rows, budget, run_name, pf)
    REPORTS.mkdir(parents=True, exist_ok=True)
    rp = REPORTS / f"{run_name}.md"
    rp.write_text(md, encoding="utf-8", newline="\n")

    print(f"\n{BAR}")
    print(f"끝. {len(rows)}문항 실행 · {time.time() - t0:.0f}초")
    print(f"  {budget.line()}")
    print(f"  결과 {out_path}")
    print(f"  리포트 {rp}")
    print(f"  누적 사용량: python eval/token_log.py --summary")
    print(BAR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
