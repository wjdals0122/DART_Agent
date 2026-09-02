"""골든셋으로 검색기를 채점한다. 정답은 사람이 라벨링한 청크 id 다.

  python eval/run_eval.py                  # eval/golden.jsonl 전체
  python eval/run_eval.py --limit 5        # 짧게 확인
  python eval/run_eval.py --sweep          # w_vec/w_bm25 조합 비교
  python eval/run_eval.py --no-report      # eval/report.md 에 append 하지 않는다

【두 가지 지표를 같이 낸다 — 헷갈리면 안 된다】
한 문항의 정답 청크가 여러 개일 수 있다. 그래서 하나로 뭉뚱그리지 않는다.

  recall@k  : top-k 안에 든 정답 개수 / 그 문항의 정답 개수, 를 문항별로 낸 평균.
              "정답 여럿 중 몇 개나 건졌나"
  hit@k     : top-k 안에 정답이 **하나라도** 있는 문항 비율. "찾긴 찾았나"
정답이 하나뿐인 문항에서는 둘이 같다.

【한 문서 상한(per_doc)을 왜 푸는가】
검색기 기본값은 한 문서에서 2청크까지만 올린다(hybrid.PER_DOC=2).
결과를 한 문서가 독점하지 못하게 하려는 옳은 설계지만, 채점에서는 이게 걸리면
"정답이 원래 상한에 막혀 안 보인 것" 을 검색 실패로 기록하게 된다.
그래서 여기서는 상한을 푼다(--per-doc). 이 값이 다르면 숫자를 비교하면 안 되므로
리포트 맨 위에 항상 찍는다.

【필터는 회사명뿐이다 — 이 숫자를 오해하면 안 된다】
골든셋 문항(benchmark_*.json)은 문항별로 어느 문서인지 특정하지 않는다.
그래서 filters 의 rcept_dt·fiscal_year 가 전부 null 이고 회사 필터만 걸린다.
회사 하나의 전체 공시(수천~1만 청크)에서 찾는 것이라 recall 이 낮게 나오는 게 정상이다.
실제 Agent 는 질문에서 접수일·사업연도를 뽑아 필터로 넣으므로 이 수치보다 높다.

【공시그룹은 사후 집계다】
문항에는 공시그룹 정보가 없다. 라벨링에서 **사람이 고른 정답 청크가 실제로 어느 공시에
있었는지**를 기록해 두고 그걸 그대로 집계한다. 추측해 붙인 값이 아니다.

【정답이 비어 있는 문항】
라벨링에서 'n'(top-20 안에 정답 없음)으로 기록한 문항이다.
채점에서 빼되 **개수와 qid 를 반드시 보고한다.** 조용히 빼면 recall 이 부풀려진다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
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
SWEEP_KS = (1, 5, 10, 20)

CAVEAT = [
    "> **이 숫자를 그대로 Agent 성능으로 읽지 말 것.**",
    "> 골든셋의 `filters` 는 `rcept_dt` · `fiscal_year` 가 전부 `null` 이다.",
    "> 회사 필터만 걸린 상태라 후보가 수천~1만 건이고, 그 안에서 정답 청크를 찾는 조건이다.",
    "> recall 이 낮게 나오는 것이 정상이며, 실제 Agent 는 질문에서 접수일을 추출해",
    "> 필터로 넣으므로 이 수치보다 높다.",
]


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
                "ms": (time.time() - t0) * 1000, "ranks": [], "n_gold": 0,
                "n_candidates": 0, "status": "exception", "top1": None}
    ms = (time.time() - t0) * 1000
    hits = res.get("results") or []
    ids = [h["chunk_id"] for h in hits]
    gold = list(row.get("gold_chunk_ids") or [])
    ranks = sorted(ids.index(g) + 1 for g in gold if g in ids)
    return {"qid": row["qid"], "error": None, "ms": ms, "ranks": ranks,
            "n_gold": len(gold), "n_candidates": res.get("n_candidates", 0),
            "status": res.get("status"), "note": res.get("note", ""),
            "top1": ids[0] if ids else None}


def _metrics(scored) -> dict:
    n = len(scored)
    hit = {k: sum(1 for _, r in scored if any(x <= k for x in r["ranks"])) / n for k in KS}
    rec = {k: sum(sum(1 for x in r["ranks"] if x <= k) / max(r["n_gold"], 1)
                  for _, r in scored) / n for k in KS}
    mrr = sum((1.0 / r["ranks"][0]) if r["ranks"] else 0.0 for _, r in scored) / n
    return {"hit": hit, "recall": rec, "mrr": mrr}


def score(rows, results) -> dict:
    """정답이 빈 문항과 검색 실패는 채점에서 빼고 따로 센다."""
    by_qid = {r["qid"]: r for r in results}
    scored, empty, failed = [], [], []
    for row in rows:
        r = by_qid.get(row["qid"])
        if r is None:
            continue
        if not row.get("gold_chunk_ids"):
            empty.append((row, r))
        elif r["error"] or r["status"] in ("not_found", "ambiguous", "exception"):
            failed.append((row, r))            # 회사명 해석 실패 등 — 멈추지 않고 기록만
        else:
            scored.append((row, r))

    n = len(scored)
    out = {"n_total": len(rows), "n_scored": n, "n_empty_gold": len(empty),
           "n_failed": len(failed), "hit": {}, "recall": {}, "mrr": 0.0,
           "ms_avg": 0.0, "ms_max": 0.0, "empty": empty, "failed": failed,
           "scored": scored}
    if not n:
        return out
    out.update(_metrics(scored))
    ms = [r["ms"] for _, r in scored]
    out["ms_avg"] = sum(ms) / len(ms)
    out["ms_max"] = max(ms)
    return out


def subset(scored, pred) -> list:
    return [(row, r) for row, r in scored if pred(row, r)]


def metric_table(title, groups) -> list:
    """groups: [(이름, [(row, r), ...]), ...]"""
    L = ["", f"**{title}**", "",
         "| 구분 | 문항 | " + " | ".join(f"recall@{k}" for k in SWEEP_KS) + " | MRR |",
         "|---|---|" + "---|" * (len(SWEEP_KS) + 1)]
    for name, g in groups:
        if not g:
            L.append(f"| {name} | 0 |" + " — |" * (len(SWEEP_KS) + 1))
            continue
        m = _metrics(g)
        L.append(f"| {name} | {len(g)} | "
                 + " | ".join(f"{m['recall'][k]:.3f}" for k in SWEEP_KS)
                 + f" | {m['mrr']:.3f} |")
    return L


def rank_table(scored) -> list:
    """문항마다 정답이 몇 위에 걸렸는지. top-k 밖이면 miss."""
    L = ["", "**문항별 정답 순위**", "",
         "| qid | 정답 수 | 걸린 순위 | 후보 | 태그 |", "|---|---|---|---|---|"]
    for row, r in sorted(scored, key=lambda x: (not x[1]["ranks"],
                                                x[1]["ranks"][0] if x[1]["ranks"] else 0)):
        pos = ", ".join(str(x) for x in r["ranks"]) if r["ranks"] else "**miss**"
        tags = ",".join(t for t in (row.get("tags") or [])
                        if t in ("found_by_grep", "edited_query", "not_in_top20"))
        L.append(f"| {row['qid']} | {r['n_gold']} | {pos} | {r['n_candidates']:,} | {tags} |")
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
    L = [f"## 검색 평가 — {stamp}", "", f"골든셋 `{Path(a.golden).name}`"]

    if not rows:
        L += ["", "**0문항.** 골든셋이 비어 있거나 없다. "
                  "`python eval/label_helper.py` 로 라벨링을 먼저 한다.", ""]
        print("\n".join(L))
        if not a.no_report:
            _append(L)
        return 0

    from src.search.hybrid import Retriever
    rt = Retriever(device=a.device)

    if a.sweep:
        L += ["", "### 가중치 비교 (w_vec, w_bm25)", ""] + CAVEAT + ["",
              "| w_vec | w_bm25 | 문항 | "
              + " | ".join(f"recall@{k}" for k in SWEEP_KS) + " | MRR | 평균ms |",
              "|---|---|---|" + "---|" * (len(SWEEP_KS) + 2)]
        for wv, wb in SWEEP:
            res = [run_one(rt, r, wv, wb, a.top_k, a.per_doc) for r in rows]
            s = score(rows, res)
            if not s["n_scored"]:
                L.append(f"| {wv} | {wb} | 0 |" + " — |" * (len(SWEEP_KS) + 2))
                continue
            L.append(f"| {wv} | {wb} | {s['n_scored']} | "
                     + " | ".join(f"{s['recall'][k]:.3f}" for k in SWEEP_KS)
                     + f" | {s['mrr']:.3f} | {s['ms_avg']:.0f} |")
            print(f"  (w_vec={wv}, w_bm25={wb}) recall@5={s['recall'][5]:.3f}", flush=True)
        L.append("")
        print("\n".join(L))
        if not a.no_report:
            _append(L)
        return 0

    results = [run_one(rt, r, 1.0, 1.0, a.top_k, a.per_doc) for r in rows]
    s = score(rows, results)

    L += [""] + CAVEAT + ["",
          f"라벨 {s['n_total']}건 · **채점 {s['n_scored']}건** · "
          f"정답이 비어 채점 제외 {s['n_empty_gold']}건 · 검색 실패 {s['n_failed']}건",
          f"설정: `top_k={a.top_k}` · `per_doc={a.per_doc}` "
          f"(검색기 기본 `PER_DOC=2` 를 풀었다 — 다른 값과 숫자를 섞어 비교하지 말 것)"]

    if s["n_empty_gold"]:
        L += ["", "채점에서 뺀 문항(라벨링에서 `n` — top-20 안에 정답 없음): "
              + ", ".join(f"`{row['qid']}`" for row, _ in s["empty"])]

    if not s["n_scored"]:
        L += ["", "채점할 문항이 0건이다.", ""]
        print("\n".join(L))
        if not a.no_report:
            _append(L)
        return 0

    L += ["", "### 전체", "",
          "| 지표 | " + " | ".join(f"@{k}" for k in KS) + " |",
          "|---|" + "---|" * len(KS),
          "| recall | " + " | ".join(f"{s['recall'][k]:.3f}" for k in KS) + " |",
          "| hit | " + " | ".join(f"{s['hit'][k]:.3f}" for k in KS) + " |",
          "",
          f"MRR **{s['mrr']:.3f}** · 지연 평균 {s['ms_avg']:.0f}ms · 최악 {s['ms_max']:.0f}ms"]

    sc = s["scored"]
    L += rank_table(sc)

    def has(tag):
        return lambda row, r: tag in (row.get("tags") or [])

    L += metric_table(
        "라벨 방법별 — `found_by_grep` 은 검색으로 못 찾고 원문 grep 으로 찾은 것이다. "
        "여기가 낮을수록 개선 여지가 크다",
        [("found_by_grep", subset(sc, has("found_by_grep"))),
         ("그 외(검색 결과에서 고름)",
          subset(sc, lambda row, r: not has("found_by_grep")(row, r))),
         ("edited_query(검색어를 사람이 고침)", subset(sc, has("edited_query"))),
         ("검색어 그대로", subset(sc, lambda row, r: not has("edited_query")(row, r)))])

    by_type = defaultdict(list)
    for row, r in sc:
        by_type[row.get("answer_type", "?")].append((row, r))
    L += metric_table("유형별", sorted(by_type.items()))

    by_grp = defaultdict(list)
    for row, r in sc:
        for g in (row.get("doc_groups") or [row.get("doc_group") or "(미상)"]):
            by_grp[g].append((row, r))
    L += metric_table("공시그룹별 (정답 청크가 실제로 실린 공시)", sorted(by_grp.items()))

    miss = [(row, r) for row, r in sc if not r["ranks"]]
    L += ["", f"**top-{a.top_k} 안에 정답이 없던 문항 {len(miss)}건**", ""]
    if miss:
        L += ["| qid | 태그 | 후보 수 | top1 chunk_id |", "|---|---|---|---|"]
        for row, r in miss:
            L.append(f"| {row['qid']} | {','.join(row.get('tags') or [])} | "
                     f"{r['n_candidates']:,} | `{r['top1']}` |")
    else:
        L.append("없음")

    if s["failed"]:
        L += ["", f"**검색 실패 {len(s['failed'])}건** (회사명 해석 실패·예외)", "",
              "| qid | status | 사유 |", "|---|---|---|"]
        for row, r in s["failed"]:
            L.append(f"| {row['qid']} | {r['status']} | {r['error'] or r.get('note', '')} |")

    L.append("")
    print("\n".join(L))
    if not a.no_report:
        _append(L)
    return 0


def _append(lines) -> None:
    """덮어쓰지 않는다. 실행할 때마다 아래로 쌓는다."""
    head = "" if REPORT.exists() else "# 검색 평가 기록\n\n실행할 때마다 아래로 쌓는다.\n\n"
    with open(REPORT, "a", encoding="utf-8") as fh:
        fh.write(head + "\n".join(lines) + "\n\n---\n\n")
    print(f"\n[report] {REPORT} 에 append")


if __name__ == "__main__":
    raise SystemExit(main())
