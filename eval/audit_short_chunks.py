"""표 제목만 담고 본문이 없는 청크가 얼마나 되는지 센다. 청킹은 고치지 않는다.

라벨링 중 세 문항에서 같은 현상이 반복됐다 — 검색이 표 제목 청크를 물어오고
실제 금액 표는 다음 청크에 있다.

  OCI홀딩스  "가. 부문별 요약 재무현황 … 다음과 같습니다. (당기)"   ← 표 없음
  CJ제일제당 "가. 요약연결재무정보 (단위 : 천원)"                   ← 표 없음

  python eval/audit_short_chunks.py
"""
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.search.source import iter_chunks_canonical  # noqa: E402

OUT = ROOT / "reports" / "short_chunk_audit.json"
SHORT = 150                      # 이 글자 수 미만이면 '짧은 청크'
_WS = re.compile(r"\s+")

# "표 제목으로 끝난다" 판정. 어느 규칙에 걸렸는지 따로 센다.
TAIL_WORDS = ("다음과 같습니다", "다음과 같음", "다음과 같다", "현황", "(단위")


def caption_reason(text: str):
    t = (text or "").rstrip()
    if not t:
        return None
    if t.endswith(")") or t.endswith("）"):
        return ")로 끝남"
    if t.endswith(":") or t.endswith("："):
        return ":로 끝남"
    for w in TAIL_WORDS:
        if t.endswith(w) or t.endswith(w + "."):
            return f"'{w}'로 끝남"
    return None


def has_table(text: str) -> bool:
    return any(l.lstrip().startswith("|") for l in (text or "").splitlines())


def main() -> int:
    t0 = time.time()
    n_all = n_short = 0
    n_short_table = 0                      # 짧은데 표를 이미 담고 있는 것
    cap = Counter()                        # 판정 사유별
    by_corp = Counter()
    by_corp_all = Counter()
    by_sec = Counter()
    by_sec_all = Counter()
    nxt = Counter()                        # 다음 청크가 표를 담고 있나
    # 엄격 집합: 짧다 + 표 제목처럼 끝난다 + **자기는 표가 없다** + 다음 청크에 표가 있다.
    # ')로 끝남' 규칙이 '(첨부 이미지: 대표이사 확인서)' 같은 것도 잡아서, 실제로
    # "제목만 있고 표는 다음에 있는" 경우만 따로 센다.
    n_strict = 0
    strict_corp = Counter()
    strict_sec = Counter()
    strict_ex = []
    n_orphan = 0                           # 반대 방향: 제목 없이 표로 시작하는 청크
    examples = defaultdict(list)
    len_hist = Counter()

    prev = None                            # 한 칸 뒤에서 따라가며 '다음 청크' 를 본다
    for rec in iter_chunks_canonical():
        n_all += 1
        content = rec.get("content") or ""
        sp = rec.get("section_path") or []
        corp = str(rec.get("company") or "?")
        sec1 = sp[1] if len(sp) > 1 else "(장 없음)"
        by_corp_all[corp] += 1
        by_sec_all[sec1] += 1

        if prev is not None:
            p = prev
            same_doc = p["doc_id"] == rec.get("doc_id")
            if same_doc and has_table(content):
                nxt["다음 청크에 표 있음"] += 1
                if not p["self_table"]:
                    n_strict += 1
                    strict_corp[p["corp"]] += 1
                    strict_sec[p["sec1"]] += 1
                    if len(strict_ex) < 6:
                        strict_ex.append({
                            "chunk_id": p["chunk_id"], "corp": p["corp"],
                            "section": p["section"], "len": p["len"],
                            "text": _WS.sub(" ", p["text"])[:150],
                            "next_head": _WS.sub(" ", content)[:120]})
            elif same_doc:
                nxt["다음 청크에 표 없음"] += 1
            else:
                nxt["다음 청크가 없음(문서 끝)"] += 1
            if len(examples[p["reason"]]) < 3:
                examples[p["reason"]].append({
                    "chunk_id": p["chunk_id"], "corp": p["corp"],
                    "section": p["section"], "len": p["len"],
                    "text": _WS.sub(" ", p["text"])[:180],
                    "next_has_table": bool(same_doc and has_table(content)),
                    "next_head": _WS.sub(" ", content)[:120] if same_doc else "(문서 끝)",
                })
            prev = None

        if len(content) < SHORT:
            n_short += 1
            len_hist[(len(content) // 25) * 25] += 1
            if has_table(content):
                n_short_table += 1
            reason = caption_reason(content)
            if reason:
                cap[reason] += 1
                by_corp[corp] += 1
                by_sec[sec1] += 1
                prev = {"chunk_id": rec.get("chunk_id"), "doc_id": rec.get("doc_id"),
                        "corp": corp, "section": " > ".join(sp[1:3]), "sec1": sec1,
                        "len": len(content), "text": content, "reason": reason,
                        "self_table": has_table(content)}

        # 반대 방향 — 제목 없이 곧바로 표로 시작하는 청크. LLM 이 표 제목을 못 본다
        first = next((l for l in content.splitlines() if l.strip()), "")
        if first.lstrip().startswith("|"):
            n_orphan += 1

        if n_all % 200_000 == 0:
            print(f"  {n_all:,}행 ({time.time() - t0:.0f}초)", flush=True)

    n_cap = sum(cap.values())
    tot_next = sum(nxt.values())
    stats = {
        "n_chunks": n_all,
        "short_threshold": SHORT,
        "n_short": n_short,
        "pct_short": round(n_short / n_all * 100, 2),
        "n_short_with_table": n_short_table,
        "n_caption_like": n_cap,
        "pct_caption_of_all": round(n_cap / n_all * 100, 2),
        "pct_caption_of_short": round(n_cap / max(n_short, 1) * 100, 2),
        "caption_reasons": dict(cap.most_common()),
        "len_histogram": {str(k): v for k, v in sorted(len_hist.items())},
        "next_chunk": dict(nxt.most_common()),
        "pct_next_has_table": round(nxt["다음 청크에 표 있음"] / max(tot_next, 1) * 100, 2),
        "n_strict_caption_only": n_strict,
        "pct_strict_of_all": round(n_strict / n_all * 100, 2),
        "n_orphan_table_start": n_orphan,
        "pct_orphan_of_all": round(n_orphan / n_all * 100, 2),
        "strict_by_company_top10": [
            {"corp": c, "n": v, "chunks": by_corp_all[c],
             "pct": round(v / max(by_corp_all[c], 1) * 100, 2)}
            for c, v in strict_corp.most_common(10)],
        "strict_by_section_top12": [
            {"section": s_, "n": v, "chunks": by_sec_all[s_],
             "pct": round(v / max(by_sec_all[s_], 1) * 100, 2)}
            for s_, v in strict_sec.most_common(12)],
        "strict_examples": strict_ex,
        "by_company_top20": [
            {"corp": c, "caption_like": v, "chunks": by_corp_all[c],
             "pct": round(v / max(by_corp_all[c], 1) * 100, 2)}
            for c, v in by_corp.most_common(20)],
        "by_section_top20": [
            {"section": s, "caption_like": v, "chunks": by_sec_all[s],
             "pct": round(v / max(by_sec_all[s], 1) * 100, 2)}
            for s, v in by_sec.most_common(20)],
        "examples": {k: v for k, v in examples.items()},
        "seconds": round(time.time() - t0, 1),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n청크 {n_all:,}개 · {stats['seconds']}초\n")
    print(f"1) {SHORT}자 미만  {n_short:,}개 ({stats['pct_short']}%)")
    print(f"   그중 이미 표를 담은 것 {n_short_table:,}개")
    print(f"\n2) 그중 '표 제목으로 끝나는' 것  {n_cap:,}개 "
          f"(전체의 {stats['pct_caption_of_all']}% · 짧은 청크의 {stats['pct_caption_of_short']}%)")
    for k, v in cap.most_common():
        print(f"     {v:>7,}  {k}")
    print(f"\n4) 그 청크의 '바로 다음 청크' 가 표를 담고 있나")
    for k, v in nxt.most_common():
        print(f"     {v:>7,} ({v / max(tot_next, 1):.1%})  {k}")
    print(f"\n3-a) 회사별 상위 10 (표제목청크 / 그 회사 전체청크)")
    for d in stats["by_company_top20"][:10]:
        print(f"     {d['caption_like']:>6,} / {d['chunks']:>7,}  ({d['pct']:>5.2f}%)  {d['corp']}")
    print(f"\n3-b) 장(section_path[1])별 상위 12")
    for d in stats["by_section_top20"][:12]:
        print(f"     {d['caption_like']:>6,} / {d['chunks']:>7,}  ({d['pct']:>5.2f}%)  {d['section'][:52]}")
    print(f"\n[reports] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
