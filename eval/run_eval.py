"""골든셋으로 검색기를 채점한다. 정답은 사람이 라벨링한 청크 id 다.

  python eval/run_eval.py                  # eval/golden.jsonl 전체
  python eval/run_eval.py --limit 5        # 짧게 확인
  python eval/run_eval.py --sweep          # w_vec/w_bm25 조합 비교
  python eval/run_eval.py --no-report      # eval/report.md 에 append 하지 않는다

【두 가지 지표를 같이 낸다 — 헷갈리면 안 된다】
한 문항의 정답 청크가 여러 개일 수 있다. 그래서 하나로 뭉뚱그리지 않는다.

  hit@k     : top-k 안에 정답이 **하나라도** 있는 문항 비율. "찾긴 찾았나"
  recall@k  : top-k 안에 든 정답 개수 / 그 문항의 정답 개수, 를 문항별로 낸 평균.
              "정답 여럿 중 몇 개나 건졌나"
정답이 하나뿐인 문항에서는 둘이 같다.

【한 문서 상한(per_doc)을 왜 푸는가】
검색기 기본값은 한 문서에서 2청크까지만 올린다(hybrid.PER_DOC=2).
결과를 한 문서가 독점하지 못하게 하려는 옳은 설계지만, 채점에서는 이게 걸리면
"정답이 원래 상한에 막혀 안 보인 것" 을 검색 실패로 기록하게 된다.
그래서 여기서는 상한을 푼다(--per-doc). 이 값이 다르면 숫자를 비교하면 안 되므로
리포트 맨 위에 항상 찍는다.

【필터는 회사명뿐이다】
골든셋 문항(benchmark_*.json)은 문항별로 어느 문서인지 특정하지 않는다.
그래서 접수일·사업연도를 안 준다. 회사 하나의 전체 공시에서 찾아야 하므로
숫자가 낮게 나오는데, 그게 Agent 가 실제로 겪는 조건이다.

【공시그룹은 사후 집계다】
문항(benchmark_*.json)에는 공시그룹 정보가 없다. 그래서 라벨링에서 **사람이 고른 정답
청크가 실제로 어느 공시에 있었는지**를 기록해 두고, 여기서는 그걸 그대로 집계한다.
산문에서 추측해 붙인 값이 아니다.

【정답이 top-20 밖인 문항】
라벨링에서 'n' 으로 기록한 문항이다. gold_chunk_ids 가 비어 있다.
채점에서 빼되 **개수는 반드시 따로 보고한다.** 조용히 빼면 recall 이 부풀려진다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

GOLDEN = ROOT / "eval" / "golden.jsonl"
REPORT = ROOT / "eval" / "report.md"

KS = (1, 3, 5, 10, 20, 50)
SWEEP = [(1.0, 1.0), (1.0, 0.0), (0.0, 1.0), (1.0, 2.0), (2.0, 1.0)]
BAR = "=" * 78


def load_golden(path: Path, limit=None) -> list[dict]:
    """없거나 비어 있어도 예외를 내지 않는다. 빈 리스트를 돌려준다."""
    if not path.exists():
        return []
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            print(f"[경고] {path.name} {i}행을 건너뛴다 — JSON 이 아니다: {e}",
                  file=sys.stderr)
            continue
        missing = [k for k in ("qid", "search_query", "gold_chunk_ids") if k not in r]
        if missing:
            print(f"[경고] {path.name} {i}행({r.get('qid')}) 에 {missing} 가 없다. 건너뛴다",
                  file=sys.stderr)
            continue
        rows.append(r)
    return rows[:limit] if limit else rows


def run_one(rt, row, w_vec, w_bm25, top_k, per_doc) -> dict:
    f = row.get("filters") or {}
    t0 = time.time()
    try:
        res = rt.search(row["search_query"], corp_name=f.get("corp_name"),
                        rcept_dt=f.get("rcept_dt"), fiscal_year=f.get("fiscal_year"),
                        top_k=top_k, pool=max(top_k, 50), per_doc=per_doc,
                        w_vec=w_vec, w_bm25=w_bm25)
    except Exception as e:                     # noqa: BLE001 — 한 문항 때문에 멈추지 않는다
        return {"qid": row["qid"], "error": f"{type(e).__name__}: {e}",
                "ms": (time.time() - t0) * 1000, "ranks": [], "n_candidates": 0,
                "status": "exception", "top1": None}
    ms = (time.time() - t0) * 1000
    status = res.get("status")
    hits = res.get("results") or []
    ids = [h["chunk_id"] for h in hits]
    gold = list(row.get("gold_chunk_ids") or [])
    ranks = [ids.index(g) + 1 for g in gold if g in ids]
    return {"qid": row["qid"], "error": None, "ms": ms, "ranks": sorted(ranks),
            "n_gold": len(gold), "n_candidates": res.get("n_candidates", 0),
            "status": status, "note": res.get("note", ""),
            "top1": ids[0] if ids else None}


def score(rows, results) -> dict:
    """빈 정답 문항과 회사명 해석 실패는 채점에서 빼고 따로 센다."""
    by_qid = {r["qid"]: r for r in results}
    scored, empty, failed = [], [], []
    for row in rows:
        r = by_qid.get(row["qid"])
        if r is None:
            continue
        if r["error"] or r["status"] in ("not_found", "ambiguous", "exception"):
            # 회사명을 못 풀었거나 예외가 났다 — 실패로 남기고 계속 간다
            if not row.get("gold_chunk_ids"):
                empty.append((row, r))
            else:
                failed.append((row, r))
            continue
        if not row.get("gold_chunk_ids"):
            empty.append((row, r))
            continue
        scored.append((row, r))

    n = len(scored)
    out = {"n_total": len(rows), "n_scored": n, "n_empty_gold": len(empty),
           "n_failed": len(failed), "hit": {}, "recall": {}, "mrr": 0.0,
           "ms_avg": 0.0, "ms_max": 0.0, "empty": empty, "failed": failed,
           "scored": scored}
    if not n:
        return out
    for k in KS:
        out["hit"][k] = sum(1 for _, r in scored if any(x <= k for x in r["ranks"])) / n
        out["recall"][k] = sum(
            (sum(1 for x in r["ranks"] if x <= k) / max(r["n_gold"], 1))
            for _, r in scored) / n
    out["mrr"] = sum((1.0 / r["ranks"][0]) if r["ranks"] else 0.0
                     for _, r in scored) / n
    ms = [r["ms"] for _, r in scored]
    out["ms_avg"] = sum(ms) / len(ms)
    out["ms_max"] = max(ms)
    return out


def breakdown(scored, key_fn, k=5) -> list[tuple]:
    g = defaultdict(list)
    for row, r in scored:
        for key in key_fn(row):
            g[key].append(r)
    out = []
    for key, rs in sorted(g.items()):
        hit = sum(1 for r in rs if any(x <= k for x in r["ranks"])) / len(rs)
        rec = sum(sum(1 for x in r["ranks"] if x <= k) / max(r["n_gold"], 1)
                  for r in rs) / len(rs)
        out.append((key, len(rs), hit, rec))
    return out


def fmt(s: dict, per_doc: int, top_k: int) -> list[str]:
    L = [f"라벨 문항 {s['n_total']}개 · 채점 {s['n_scored']}개 · "
         f"정답이 top-20 밖(gold 비어 있음) {s['n_empty_gold']}개 · "
         f"검색 실패 {s['n_failed']}개",
         f"설정: top_k={top_k} · per_doc={per_doc} "
         f"(검색기 기본 PER_DOC=2 를 풀었다 — 다른 값과 숫자를 섞어 비교하지 말 것)"]
    if not s["n_scored"]:
        L.append("")
        L.append("채점할 문항이 0개다. eval/golden.jsonl 을 먼저 만들어라 "
                 "(python eval/label_helper.py).")
        return L
    L += ["", "| 지표 | " + " | ".join(f"@{k}" for k in KS) + " |",
          "|---|" + "---|" * len(KS),
          "| hit | " + " | ".join(f"{s['hit'][k]:.3f}" for k in KS) + " |",
          "| recall | " + " | ".join(f"{s['recall'][k]:.3f}" for k in KS) + " |",
          "",
          f"MRR {s['mrr']:.3f} · 지연 평균 {s['ms_avg']:.0f}ms · 최악 {s['ms_max']:.0f}ms"]
    return L


def section(title, rows_) -> list[str]:
    L = ["", f"**{title}** (k=5)", "", "| 구분 | 문항 | hit@5 | recall@5 |", "|---|---|---|---|"]
    for key, n, hit, rec in rows_:
        L.append(f"| {key} | {n} | {hit:.3f} | {rec:.3f} |")
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋 검색 평가")
    ap.add_argument("--golden", default=str(GOLDEN))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--top-k", type=int, default=max(KS))
    ap.add_argument("--per-doc", type=int, default=50,
                    help="한 문서에서 올릴 최대 청크 수. 기본 50(상한 사실상 없음)")
    ap.add_argument("--sweep", action="store_true", help="w_vec/w_bm25 조합 비교")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args()

    rows = load_golden(Path(a.golden), a.limit)
    stamp = datetime.now().isoformat(timespec="seconds")
    lines = [f"## 검색 평가 — {stamp}", "",
             f"골든셋 `{Path(a.golden).name}`"]

    if not rows:
        lines += ["", "**0문항.** 골든셋이 비어 있거나 없다. "
                      "`python eval/label_helper.py` 로 라벨링을 먼저 한다.", ""]
        print("\n".join(lines))
        if not a.no_report:
            _append(lines)
        return 0

    from src.search.hybrid import Retriever
    rt = Retriever(device=a.device)

    if a.sweep:
        lines += ["", "### 가중치 비교 (w_vec, w_bm25)", "",
                  "| w_vec | w_bm25 | hit@1 | hit@5 | hit@20 | recall@5 | MRR | 평균ms |",
                  "|---|---|---|---|---|---|---|---|"]
        for wv, wb in SWEEP:
            res = [run_one(rt, r, wv, wb, a.top_k, a.per_doc) for r in rows]
            s = score(rows, res)
            if not s["n_scored"]:
                lines.append(f"| {wv} | {wb} | (채점 0문항) | | | | | |")
                continue
            lines.append(f"| {wv} | {wb} | {s['hit'][1]:.3f} | {s['hit'][5]:.3f} | "
                         f"{s['hit'][20]:.3f} | {s['recall'][5]:.3f} | {s['mrr']:.3f} | "
                         f"{s['ms_avg']:.0f} |")
            print(f"  (w_vec={wv}, w_bm25={wb}) hit@5={s['hit'][5]:.3f}", flush=True)
        print("\n".join(lines))
        if not a.no_report:
            _append(lines)
        return 0

    results = [run_one(rt, r, 1.0, 1.0, a.top_k, a.per_doc) for r in rows]
    s = score(rows, results)
    lines += [""] + fmt(s, a.per_doc, a.top_k)

    if s["n_scored"]:
        # 공시그룹은 문항이 아니라 **라벨링에서 사람이 고른 정답 청크**에서 온다.
        # 정답이 두 공시에 걸쳐 있으면 양쪽에 다 센다(그래서 합이 문항 수보다 클 수 있다).
        lines += section("공시그룹별", breakdown(
            s["scored"], lambda r: r.get("doc_groups") or [r.get("doc_group") or "(미상)"]))
        lines += section("유형별", breakdown(s["scored"], lambda r: [r.get("answer_type", "?")]))
        lines += section("태그별", breakdown(s["scored"], lambda r: r.get("tags") or ["(태그없음)"]))

    # 실패 문항 — top-20 안에 정답이 없었던 것
    miss = [(row, r) for row, r in s["scored"] if not any(x <= 20 for x in r["ranks"])]
    lines += ["", f"**top-20 안에 정답이 없던 문항 {len(miss)}개**", ""]
    if miss:
        lines += ["| qid | 태그 | n_candidates | top1 chunk_id |", "|---|---|---|---|"]
        for row, r in miss:
            lines.append(f"| {row['qid']} | {','.join(row.get('tags') or [])} | "
                         f"{r['n_candidates']:,} | {r['top1']} |")
    else:
        lines.append("없음")

    if s["failed"]:
        lines += ["", f"**검색 실패 {len(s['failed'])}개** (회사명 해석 실패·예외 포함)", "",
                  "| qid | status | 사유 |", "|---|---|---|"]
        for row, r in s["failed"]:
            lines.append(f"| {row['qid']} | {r['status']} | "
                         f"{r['error'] or r.get('note', '')} |")

    if s["empty"]:
        lines += ["", f"**정답이 top-20 밖이라 채점에서 뺀 문항 {len(s['empty'])}개** "
                      "(라벨링에서 'n' 으로 기록)", "",
                  "  " + ", ".join(row["qid"] for row, _ in s["empty"])]

    lines.append("")
    print("\n".join(lines))
    if not a.no_report:
        _append(lines)
    return 0


def _append(lines) -> None:
    """덮어쓰지 않는다. 실행할 때마다 아래로 쌓는다."""
    head = "" if REPORT.exists() else "# 검색 평가 기록\n\n실행할 때마다 아래로 쌓는다.\n\n"
    with open(REPORT, "a", encoding="utf-8") as fh:
        fh.write(head + "\n".join(lines) + "\n\n---\n\n")
    print(f"\n[report] {REPORT} 에 append")


if __name__ == "__main__":
    raise SystemExit(main())
