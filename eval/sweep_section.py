"""절 제목 가중치(w_section) 스윕. **LLM 을 한 번도 부르지 않는다.**

  python eval/sweep_section.py
  python eval/sweep_section.py --device cpu

【w_section 은 원래 없던 파라미터다】
지시받은 시점에 이 리포에 `w_section` 은 **존재하지 않았다**(전체 grep 0건).
재보고 없이 스윕만 돌릴 방법이 없어 `hybrid.search(w_section=...)` 를 새로 넣었다.
**기본값 0.0 = 지금까지의 동작과 완전히 같다** — 0 이면 절 제목을 읽지도 않는다.
그러니 이 스윕이 "개선 없음" 으로 끝나면 아무것도 안 고치면 된다.

무엇을 하는 값인가: 청크의 **절 제목**(`section_path`)이 질의어를 몇 개나 담고
있는지를 0~1 로 재서, BM25·dense 와 나란히 **세 번째 리스트**로 RRF 에 넣는다.
가중치가 w_section 이다. RRF 규칙(각 리스트 top-M 안에 든 것만 기여)은 그대로다.

【짧은 질의를 따로 보는 이유】
'연결 매출액' 같은 2어절 질의는 BM25 가 기댈 토큰이 적어 절 제목이 상대적으로
더 큰 신호가 된다. 그래서 **같은 골든셋 문항의 검색어만 짧게 줄인** 표를 따로 낸다.
정답 청크는 그대로다. 두 표의 절대 수치를 서로 비교하면 안 된다 —
질의가 다르므로 난이도가 다르다. **표 안에서 w_section 끼리만 비교한다.**

【채점 조건】
- 필터는 회사명뿐이다(골든셋의 rcept_dt·fiscal_year 가 전부 null). 후보가 수천~1만
- `per_doc=50` — 한 문서 상한(PER_DOC=2)을 푼다. 안 풀면 상한에 막힌 정답을
  검색 실패로 기록하게 된다
- 정답 청크가 없는 문항(라벨링 'n')은 채점에서 빼고 개수를 보고한다
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from eval.run_eval import load_golden          # noqa: E402

GOLDEN = ROOT / "eval" / "golden.jsonl"
OUT_MD = ROOT / "reports" / "sweep_section.md"
OUT_JSON = ROOT / "reports" / "sweep_section.json"

WS = (0.0, 0.3, 0.5, 1.0)
KS = (5, 20)
TOP_K = 20
PER_DOC = 50

# 짧은 질의 3건 — 같은 골든셋 문항의 **검색어만** 2어절로 줄인 것이다.
# 정답 청크는 원본 그대로 쓴다. 새 라벨을 만들지 않았다.
SHORT = {
    "삼성바이오로직스__multi_closed": "연결 매출액",
    "LG생활건강__multi_closed": "부문 매출",
    "삼성전기__search_closed": "배당 주당",
}


def run_one(rt, row, w_section: float, query: str | None = None) -> dict:
    f = row.get("filters") or {}
    q = query if query is not None else row["search_query"]
    t0 = time.time()
    try:
        res = rt.search(q, corp_name=f.get("corp_name"),
                        rcept_dt=f.get("rcept_dt"), fiscal_year=f.get("fiscal_year"),
                        top_k=TOP_K, pool=max(TOP_K, 50), per_doc=PER_DOC,
                        w_section=w_section)
    except Exception as e:                     # noqa: BLE001 — 한 문항에 멈추지 않는다
        return {"qid": row["qid"], "query": q, "error": f"{type(e).__name__}: {e}",
                "ms": (time.time() - t0) * 1000, "ranks": [], "n_gold": 0,
                "status": "exception"}
    ms = (time.time() - t0) * 1000
    ids = [h["chunk_id"] for h in (res.get("results") or [])]
    gold = list(row.get("gold_chunk_ids") or [])
    return {"qid": row["qid"], "query": q, "error": None, "ms": ms,
            "ranks": sorted(ids.index(g) + 1 for g in gold if g in ids),
            "n_gold": len(gold), "status": res.get("status")}


def metrics(res: list) -> dict:
    ok = [r for r in res if not r["error"] and r["n_gold"]]
    n = len(ok) or 1
    out = {"n": len(ok)}
    for k in KS:
        out[f"recall@{k}"] = sum(
            sum(1 for x in r["ranks"] if x <= k) / max(r["n_gold"], 1) for r in ok) / n
        out[f"hit@{k}"] = sum(1 for r in ok if any(x <= k for x in r["ranks"])) / n
    out["mrr"] = sum((1.0 / r["ranks"][0]) if r["ranks"] else 0.0 for r in ok) / n
    out["ms"] = sum(r["ms"] for r in ok) / n
    return out


def table(by_w: dict, title: str, n_items: int) -> list:
    L = ["", f"### {title} (문항 {n_items}건)", "",
         "| w_section | recall@5 | recall@20 | hit@5 | hit@20 | MRR | 평균ms |",
         "|---|---|---|---|---|---|---|"]
    base = by_w[WS[0]]
    for w in WS:
        m = by_w[w]
        d5 = m["recall@5"] - base["recall@5"]
        mark = "" if w == WS[0] else f" ({d5:+.3f})"
        L.append(f"| {w} | {m['recall@5']:.3f}{mark} | {m['recall@20']:.3f} | "
                 f"{m['hit@5']:.3f} | {m['hit@20']:.3f} | {m['mrr']:.3f} | {m['ms']:.0f} |")
    return L


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--golden", default=str(GOLDEN))
    a = ap.parse_args()

    rows = load_golden(Path(a.golden))
    scored = [r for r in rows if r.get("gold_chunk_ids")]
    empty = [r["qid"] for r in rows if not r.get("gold_chunk_ids")]
    print(f"[sweep] 골든셋 {len(rows)}건 · 채점 {len(scored)}건 · "
          f"정답 없어 제외 {len(empty)}건 {empty}")

    short_rows = [r for r in scored if r["qid"] in SHORT]
    missing = [q for q in SHORT if q not in {r['qid'] for r in scored}]
    if missing:
        print(f"[중단] 짧은 질의용 문항이 골든셋에 없다: {missing}")
        return 2

    from src.search.hybrid import Retriever
    rt = Retriever(device=a.device)

    full_by_w, short_by_w, raw = {}, {}, {}
    for w in WS:
        res = [run_one(rt, r, w) for r in scored]
        full_by_w[w] = metrics(res)
        raw[f"full_{w}"] = res
        print(f"  w_section={w}  recall@5={full_by_w[w]['recall@5']:.3f} "
              f"recall@20={full_by_w[w]['recall@20']:.3f} "
              f"({full_by_w[w]['ms']:.0f}ms)", flush=True)

    for w in WS:
        res = [run_one(rt, r, w, SHORT[r["qid"]]) for r in short_rows]
        short_by_w[w] = metrics(res)
        raw[f"short_{w}"] = res
        print(f"  [짧은질의] w_section={w}  recall@5={short_by_w[w]['recall@5']:.3f}",
              flush=True)

    L = [f"# w_section 스윕 — {time.strftime('%Y-%m-%dT%H:%M:%S')}", "",
         "**LLM 호출 0회.** 검색기만 돌린다.", "",
         f"골든셋 `{Path(a.golden).name}` {len(rows)}건 중 정답 청크가 있는 "
         f"**{len(scored)}건**을 채점했다."]
    if empty:
        L.append(f"제외 {len(empty)}건(라벨링에서 top-20 안에 정답 없음): "
                 + ", ".join(f"`{q}`" for q in empty))
    L += ["",
          f"설정 `top_k={TOP_K}` · `per_doc={PER_DOC}`(문서당 상한 해제) · "
          "필터는 회사명만",
          "",
          "> `w_section` 은 이 작업 전까지 리포에 **없던 파라미터**다. 스윕을 위해",
          "> `hybrid.search(w_section=...)` 로 새로 넣었고 **기본값 0.0 은 기존 동작과",
          "> 완전히 같다**(0 이면 절 제목을 읽지도 않는다).",
          "> 절 제목이 질의어를 몇 개 담았는지를 0~1 로 재서 세 번째 리스트로 RRF 에 넣는다."]

    L += table(full_by_w, "골든셋 전체 — 원래 검색어", full_by_w[0.0]["n"])
    L += table(short_by_w, "짧은 질의(2어절)로 줄였을 때", short_by_w[0.0]["n"])
    L += ["", "짧은 질의는 같은 문항의 **검색어만** 줄인 것이다. 정답 청크는 그대로다.",
          "두 표의 절대 수치를 서로 비교하지 말 것 — 질의가 달라 난이도가 다르다.", ""]
    for qid, q in SHORT.items():
        orig = next(r["search_query"] for r in scored if r["qid"] == qid)
        L.append(f"- `{qid}` — `{orig}` → `{q}`")

    best = max(WS, key=lambda w: full_by_w[w]["recall@5"])
    L += ["", "## 판정", ""]
    if full_by_w[best]["recall@5"] <= full_by_w[0.0]["recall@5"] + 1e-9:
        L.append("**개선 없음. w_section 은 0 으로 둔다.** 기본값이 0 이므로 "
                 "코드에서 더 할 일이 없다.")
    else:
        L.append(f"recall@5 가 w_section={best} 에서 "
                 f"{full_by_w[0.0]['recall@5']:.3f} → {full_by_w[best]['recall@5']:.3f} "
                 f"({full_by_w[best]['recall@5'] - full_by_w[0.0]['recall@5']:+.3f}) 로 올랐다. "
                 "문항 수가 적어 그대로 채택하지 말고 사람이 판단할 것.")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(
        {"full": {str(k): v for k, v in full_by_w.items()},
         "short": {str(k): v for k, v in short_by_w.items()},
         "raw": raw}, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print("\n".join(L))
    print(f"\n[sweep] {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
