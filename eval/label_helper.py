"""골든셋 라벨링 도구 — 사람이 정답 청크를 고른다.

**질문도 정답도 여기서 만들지 않는다.** 질문과 정답(`ground_truth`)은 사람이 만든
`benchmark_*.json` 에 있는 것을 그대로 쓰고, 이 도구는 "그 정답이 실린 청크가 어느 것인가"
만 사람에게 물어 받아 적는다. 자동으로 정답 청크를 정하는 코드는 이 파일 어디에도 없다.

  python eval/label_helper.py --sample          # 30문항 표본추출 -> eval/golden_todo.jsonl
  python eval/label_helper.py                   # 라벨링 시작(이어서 진행)
  python eval/label_helper.py --dry-run         # 첫 문항 화면만 띄우고 종료(기록 안 함)
  python eval/label_helper.py --show-queries    # 검색어 변환 전후 목록만 출력

【왜 benchmark_*.json 인가 — bench_q_part*.json 이 아니라】
둘 다 Question/ 아래에 있다.

  bench_q_part*.json   기계 생성 318문항 · 회사 15개 · **정답 없음** ·
                       회사/그룹/유형/접수일이 필드로 들어 있다
  benchmark_*.json     사람이 쓴 252문항 · 회사 40개 · **정답 있음(ground_truth)** ·
                       문서 정보는 doc_info 산문 한 줄뿐

benchmark 쪽을 쓴다. 이유는 셋이다.
  1. 회사가 40개라 "30문항 전부 서로 다른 회사" 가 실제로 가능하다(15개로는 불가능했다)
  2. 금융·보험+엔터 회사가 7곳이다(15개 풀에는 KB금융·JYP Ent 둘뿐이었다)
  3. **ground_truth 에 정답 숫자가 적혀 있어서** 그 숫자로 원문을 직접 grep 할 수 있다.
     검색기가 top-20 에 못 올린 정답을 사람이 눈으로 확인할 수 있다는 뜻이고,
     그래야 'not_in_top20' 을 정확히 기록해 recall 이 부풀려지지 않는다

【대신 포기한 것 — 접수일 필터】
benchmark_*.json 의 doc_info 는 **파일 단위 산문**이다.
"…사업보고서(2026.03.20 접수, 20260320001088) 및 투자판단관련주요경영사항 4건(…)"
처럼 문서를 여러 개 가리키는 파일이 42개 중 22개다. 어느 질문이 어느 문서 건지는
파일에 안 적혀 있다. 산문에서 추측해 붙이면 그게 바로 데이터를 지어내는 짓이다.

그래서 **회사 필터만** 건다. 접수일을 안 주므로 검색이 더 어려워지고 recall 은 낮게 나온다.
그게 9/3 Agent 가 실제로 겪는 조건이다 — Agent 도 접수일을 미리 못 받는다.

공시그룹(periodic/exchange/major/holding) 배분도 미리 못 맞춘다. 대신 **사람이 고른
정답 청크가 실제로 어느 공시에 있었는지**를 기록해 사후 집계한다. 지어내는 값이 없다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

EVAL = ROOT / "eval"
TODO = EVAL / "golden_todo.jsonl"
GOLDEN = EVAL / "golden.jsonl"
UNIVERSE = ROOT / "corpus" / "universe.csv"

# benchmark_*.json 을 찾을 곳. eval/ 을 먼저 보고, 없으면 Question/ 아래를 뒤진다.
BENCH_GLOBS = ("eval/benchmark_*.json",
               "Question/**/benchmark_*.json",
               "benchmark_*.json")

# 검색기 책임 밖인 유형은 후보에서 뺀다.
# multi_open·complex_* 는 여러 문서를 읽고 계산·서술하는 문제라 '정답 청크' 가 하나로
# 떨어지지 않는다. 검색 품질을 재는 데 쓰면 지표가 흐려진다.
KEEP_TYPES = ("search_closed", "search_open", "multi_closed")

SEED = 20260904                      # 재현용 고정 시드
N_TRIALS = 3000
N_SAMPLE = 30

QUOTA_TYPE = {"search_closed": 12, "search_open": 10, "multi_closed": 8}
FOCUS_SECTORS = ("금융·보험", "엔터테인먼트")   # '금융·보험·엔터'
FOCUS_MIN = 5
MAX_PER_CORP = 1                     # 30문항 전부 서로 다른 회사

TOP_K = 20
# 검색기 기본값은 한 문서에서 2청크까지만 올린다(hybrid.PER_DOC=2). 결과를 한 문서가
# 독점하지 못하게 하려는 것인데, 라벨링에서는 그 상한이 걸리면 top-20 을 보여준다고
# 해놓고 실제로는 몇 개만 보여주게 된다. 여기서는 푼다.
PREVIEW = 200                        # content 미리보기 글자 수
GREP_MAX_HITS = 12                   # grep 결과를 몇 개까지 찍을지
BAR = "─" * 78

_RCEPT = re.compile(r"\b(\d{14})\b")
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{4,}(?:\.\d+)?")
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------- 문항 읽기


def find_bench_files() -> list[Path]:
    for pat in BENCH_GLOBS:
        hits = sorted(ROOT.glob(pat))
        if hits:
            return hits
    return []


def load_bench() -> tuple[list[dict], list[Path], dict]:
    """benchmark_*.json 을 읽어 (문항, 파일목록, 건너뛴 파일) 을 돌려준다.

    - `scenarios` 가 없는 파일은 문항 파일이 아니다(benchmark_master_templates_24.json).
    - 내용이 완전히 같은 중복본이 있다(…_2024AR(1).json). 해시로 걸러낸다.
    """
    files = find_bench_files()
    if not files:
        raise SystemExit(
            "benchmark_*.json 을 찾지 못했다. 찾아본 곳: " + ", ".join(BENCH_GLOBS)
            + "\n문항 파일이 없으면 아무것도 만들 수 없다. 중단한다.")

    rows: list[dict] = []
    used: list[Path] = []
    skipped: dict = {"scenarios 없음": [], "내용 중복": []}
    seen_hash: dict[str, str] = {}
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "scenarios" not in data:
            skipped["scenarios 없음"].append(f.name)
            continue
        h = hashlib.md5(json.dumps(data, ensure_ascii=False,
                                   sort_keys=True).encode()).hexdigest()
        if h in seen_hash:
            skipped["내용 중복"].append(f"{f.name} (= {seen_hash[h]})")
            continue
        seen_hash[h] = f.name

        corp = data.get("company")
        if not corp:
            raise SystemExit(f"{f.name} 에 company 가 없다. 기본값으로 채우지 않고 중단한다")
        doc_info = data.get("doc_info") or ""
        for typ, sc in data["scenarios"].items():
            if not isinstance(sc, dict) or not sc.get("query"):
                raise SystemExit(f"{f.name} 의 {typ} 에 query 가 없다. 중단한다")
            if not sc.get("ground_truth"):
                raise SystemExit(f"{f.name} 의 {typ} 에 ground_truth 가 없다. 중단한다")
            rows.append({
                "id": f"{corp}__{typ}", "corp": corp, "type": typ,
                "query": sc["query"], "ground_truth": sc["ground_truth"],
                "doc_info": doc_info,
                "rcept_nos": sorted(set(_RCEPT.findall(doc_info))),
                "_file": f.name,
            })
        used.append(f)

    dup = [k for k, v in Counter(r["id"] for r in rows).items() if v > 1]
    if dup:
        raise SystemExit(f"문항 id 중복 {len(dup)}건: {dup[:5]}")
    return rows, used, skipped


def load_sectors() -> dict[str, str]:
    if not UNIVERSE.exists():
        raise SystemExit(f"{UNIVERSE} 가 없다. 업종 배분을 확인할 수 없어 중단한다")
    with open(UNIVERSE, encoding="utf-8-sig") as fh:
        return {r["corp_name"]: r.get("sector", "") for r in csv.DictReader(fh)}


# ---------------------------------------------------------------- 검색어 변환

# 사람이 쓴 질문이라 기계 생성 문항 같은 정형 껍데기가 없다. 그래도 회사명·연도·지시문은
# 똑같이 걷어낸다. 회사명은 필터로 따로 주므로 질의에 남으면 BM25 점수만 흐린다.
_YM = re.compile(r"20\d{2}\s*년(?:\s*\d{1,2}\s*월)?(?:\s*기준|말)?")
_BOILER = [
    r"각각\s*제시하시오", r"제시하시오", r"서술하시오", r"산출하시오", r"계산하시오",
    r"설명하시오", r"작성하시오", r"정리해서\s*알려줘", r"알려줄래\??", r"알려줘",
    r"정리해줄래\??", r"정리해줘", r"뽑아줘", r"봐줄래\??", r"풀어줘", r"해봐라",
    r"단위와\s*함께", r"같이\s*알려줘", r"함께\s*밝히", r"개조식으로", r"알기\s*쉽게",
    r"근거하여", r"근거로", r"기준으로", r"관련해서", r"관련하여", r"좀\s*",
]
_BOILER_RE = re.compile("|".join(_BOILER))
_PAREN = re.compile(r"[「」『』（）()\[\]{}<>《》〈〉]")

# 가상의 비교 대상이 든 마디는 통째로 버린다. 후보 126문항 중 40문항에 나온다.
# "동종업계 가상 경쟁사 '스타라이즈엔터'가 …" 는 코퍼스에 없는 내용이라
# 남겨두면 없는 회사와 없는 숫자를 찾아 헤맨다.
# 쉼표로 끊되 숫자 안의 쉼표(280,000,000)로는 안 끊는다.
_SEG = re.compile(r"(?<!\d)[,、](?!\d)")


def drop_hypothetical(text: str) -> str:
    kept = [g for g in _SEG.split(text) if "가상" not in g]
    return ", ".join(kept) if kept else text

STOPWORDS = {
    "기준", "금액", "단위", "각각", "숫자", "정확", "얼마", "퍼센트", "내용", "관련",
    "제시", "서술", "산출", "계산", "검산", "정리", "설명", "기록",
    "회사", "공시", "보고서", "제출", "접수", "다음", "포함", "여부", "일치",
    "가지", "부분", "사항", "문서", "해당", "이용", "차지", "기재",
    "확인", "동종", "업계", "경쟁사", "가상", "비교", "견주", "차액", "각각",
    # '~를 작성해줘', '브리핑해줘', '요즘', '구체적으로' — 지시문에서 오는 말
    "작성", "브리핑", "요즘", "구체",
    "년", "월", "일", "것", "수", "때", "등", "및", "좀", "안", "위", "중", "전체",
}
# 명사만 남긴다. 동사·형용사 어간은 질문 껍데기에서 오는 게 대부분이다.
_NOUN_TAGS = {"NNG", "NNP", "SL", "SH", "SN"}


def _strip_company(text: str, corp: str) -> str:
    forms = {corp, _WS.sub("", corp)}
    try:
        from src.search.aliases import resolve_company
        card = resolve_company(corp)
        for k in ("corp_name", "listed_name"):
            if card.get(k):
                forms.add(card[k])
                forms.add(_WS.sub("", card[k]))
    except Exception:                      # noqa: BLE001 — 별칭이 없어도 진행한다
        pass
    for f in sorted(forms, key=len, reverse=True):
        if len(f) >= 2:
            text = re.sub(re.escape(f) + r"\s*(?:이|가|은|는|의|을|를|에서|에|과|와)?",
                          " ", text)
    return text


def to_search_query(rec: dict) -> str:
    """질문 문장 -> 검색어.

      1. 가상의 비교 대상이 든 마디 삭제 (multi_closed 에 많다)
      2. 회사명(별칭 포함) 삭제 — 필터로 따로 준다
      3. 연·월 표현, 질문 껍데기(지시문) 삭제
      4. Kiwi 로 명사만 추출 -> 불용어 제거 -> 중복 제거 -> 앞에서 8개

    규칙이 전부 걷어내 버리면 원문을 그대로 쓴다. 빈 검색어는 넘기지 않는다.
    """
    t = drop_hypothetical(rec.get("query") or "")
    t = _strip_company(t, rec.get("corp") or "")
    t = _YM.sub(" ", t)
    t = _BOILER_RE.sub(" ", t)
    t = _PAREN.sub(" ", t)

    out, seen = [], set()
    for w in _nouns(t):
        if w in STOPWORDS or len(w) < 2 or w.isdigit() or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 8:
            break
    return " ".join(out) if out else _WS.sub(" ", (rec.get("query") or "")).strip()


def _nouns(text: str) -> list[str]:
    """Kiwi 로 명사만. BM25 색인과 같은 사용자사전을 쓴다."""
    try:
        from src.search.tokenize_ko import get_kiwi
        kiwi = get_kiwi()
    except Exception as e:                 # noqa: BLE001 — 형태소 분석기가 없어도 죽지 않는다
        print(f"[검색어] Kiwi 를 못 써서 공백 분리로 대체한다 ({type(e).__name__}: {e})",
              file=sys.stderr)
        return [w for w in _WS.split(text) if w]
    return [tk.form for tk in kiwi.tokenize(text)
            if tk.tag.split("-")[0] in _NOUN_TAGS]


# ---------------------------------------------------------------- 표본추출


def _plan(rng, cap_of):
    """유형마다 몇 문항을 뽑을지. 칸 상한은 그 유형 문항을 가진 회사 수다."""
    need = dict(QUOTA_TYPE)
    plan = Counter()
    for _ in range(N_SAMPLE):
        opts = [t for t in need if need[t] > 0 and plan[t] < cap_of.get(t, 0)]
        if not opts:
            return None
        t = rng.choice(opts)
        plan[t] += 1
        need[t] -= 1
    return plan


def _assign(plan, by_cell, caps, rng):
    """유형별로 필요한 만큼 **서로 다른 회사**를 고른다. 회사별 상한은 caps.

    최대유량으로 푼다.  source → 유형(용량 plan) → 회사(용량 1) → sink(용량 caps[회사])
    유형→회사 용량이 1이라 같은 유형에서 같은 회사가 두 번 뽑히지 않는다.
    탐욕이 아니라 되물러 가며 찾으므로, 가능한 배분이 있으면 찾아낸다.
    """
    cells = list(plan)
    corps = sorted({c for cell in cells for c in by_cell.get(cell, ())})
    ci = {c: i for i, c in enumerate(cells)}
    pi = {c: len(cells) + i for i, c in enumerate(corps)}
    S, T = len(cells) + len(corps), len(cells) + len(corps) + 1
    cap_e: dict = {}
    adj: dict = {}

    def edge(u, v, w):
        cap_e[(u, v)] = cap_e.get((u, v), 0) + w
        adj.setdefault(u, []).append(v)
        adj.setdefault(v, []).append(u)
        cap_e.setdefault((v, u), 0)

    for cell in cells:
        edge(S, ci[cell], plan[cell])
        for c in by_cell.get(cell, ()):
            edge(ci[cell], pi[c], 1)
    for c in corps:
        edge(pi[c], T, caps.get(c, 0))

    flow = 0
    while True:                                   # BFS 증가경로 (에드먼즈-카프)
        prev, q = {S: None}, [S]
        while q and T not in prev:
            u = q.pop(0)
            nbr = list(dict.fromkeys(adj.get(u, [])))
            rng.shuffle(nbr)                      # 시드에 따라 다른 해를 고르게
            for v in nbr:
                if v not in prev and cap_e.get((u, v), 0) > 0:
                    prev[v] = u
                    q.append(v)
        if T not in prev:
            break
        v, path = T, []
        while prev[v] is not None:
            path.append((prev[v], v))
            v = prev[v]
        for u, w in path:
            cap_e[(u, w)] -= 1
            cap_e[(w, u)] = cap_e.get((w, u), 0) + 1
        flow += 1
    if flow < N_SAMPLE:
        return None
    out = []
    for cell in cells:
        for c in by_cell.get(cell, ()):
            if cap_e.get((pi[c], ci[cell]), 0) > 0:      # 역방향 용량 = 이 간선을 썼다
                out.append((cell, c))
    return out


def _penalty(pick, sectors) -> tuple:
    ty = Counter(r["type"] for r in pick)
    per = Counter(r["corp"] for r in pick)
    focus = sum(1 for r in pick if sectors.get(r["corp"], "") in FOCUS_SECTORS)
    dev_t = sum(abs(ty[k] - v) for k, v in QUOTA_TYPE.items())
    return (N_SAMPLE - len(pick), dev_t,
            max(0, FOCUS_MIN - focus),
            sum(max(0, v - MAX_PER_CORP) for v in per.values()),
            -len(per))


def sample(cands, sectors, seed=SEED, trials=N_TRIALS):
    """고정 시드로 재현 가능한 표본. 조건을 정확히 못 맞추면 가장 가까운 것을 고른다."""
    rng = random.Random(seed)
    idx: dict = {}
    for r in cands:
        idx.setdefault(r["type"], {}).setdefault(r["corp"], r)
    by_cell = {k: sorted(v) for k, v in idx.items()}
    cap_of = {k: len(v) for k, v in by_cell.items()}
    corps = sorted({r["corp"] for r in cands})
    focus = [c for c in corps if sectors.get(c, "") in FOCUS_SECTORS]

    # 금융·엔터 문항이 모자라는 일이 없게, 먼저 그 회사들만으로 FOCUS_MIN 을 채운 해를
    # 찾아본다. 실패하면 상한만 걸고 무작위 재시도에 맡긴다.
    ladders = []
    if len(focus) >= FOCUS_MIN:
        ladders.append({c: (MAX_PER_CORP if c in focus else 0) for c in corps})
    ladders.append({c: MAX_PER_CORP for c in corps})

    best = None
    for _ in range(trials):
        plan = _plan(rng, cap_of)
        if plan is None:
            continue
        got = _assign(plan, by_cell, ladders[-1], rng)
        if got is None:
            continue
        pick = [idx[cell][corp] for cell, corp in got]
        if len({r["id"] for r in pick}) != len(pick):
            continue
        p = _penalty(pick, sectors)
        if best is None or p < best[0]:
            best = (p, pick)
        if p[:3] == (0, 0, 0):
            break
    if best is None:
        raise SystemExit("배분 조건을 만족하는 표본을 못 찾았다. 문항 수가 모자란다")
    return best[1]


def build_todo(seed=SEED) -> dict:
    rows, files, skipped = load_bench()
    sectors = load_sectors()
    cands = [r for r in rows if r["type"] in KEEP_TYPES]
    unknown = sorted({r["corp"] for r in cands if r["corp"] not in sectors})
    if unknown:
        print(f"[경고] universe.csv 에 없는 회사 {len(unknown)}개: {unknown}", file=sys.stderr)

    pick = sample(cands, sectors, seed)
    pick.sort(key=lambda r: (r["type"], r["corp"]))

    with open(TODO, "w", encoding="utf-8") as fh:
        for i, r in enumerate(pick, 1):
            fh.write(json.dumps({
                "n": i, "qid": r["id"], "corp": r["corp"], "type": r["type"],
                "sector": sectors.get(r["corp"], ""),
                "question": r["query"], "ground_truth": r["ground_truth"],
                "doc_info": r["doc_info"], "rcept_nos": r["rcept_nos"],
                "search_query": to_search_query(r), "src_file": r["_file"],
            }, ensure_ascii=False) + "\n")

    ty = Counter(r["type"] for r in pick)
    per = Counter(r["corp"] for r in pick)
    sec = Counter(sectors.get(r["corp"], "?") for r in pick)
    return {
        "seed": seed, "trials": N_TRIALS,
        "n_files": len(files), "skipped": skipped,
        "n_all": len(rows), "n_candidates": len(cands), "n_sampled": len(pick),
        "n_companies_in_pool": len({r["corp"] for r in cands}),
        "type": {k: [ty.get(k, 0), v] for k, v in QUOTA_TYPE.items()},
        "n_companies": len(per), "max_per_company": max(per.values()),
        "focus_sector_count": sum(v for k, v in sec.items() if k in FOCUS_SECTORS),
        "sectors": dict(sec.most_common()),
        "n_with_rcept_hint": sum(1 for r in pick if r["rcept_nos"]),
    }


def print_sample_stats(st: dict) -> None:
    print(f"\n표본 시드 = {st['seed']}  (재현하려면 --seed {st['seed']})")
    print(f"문항 파일 {st['n_files']}개 · 전체 {st['n_all']}문항 → "
          f"후보 {st['n_candidates']}문항({'/'.join(KEEP_TYPES)}) → 표본 {st['n_sampled']}문항")
    for why, names in st["skipped"].items():
        if names:
            print(f"  건너뜀({why}) {len(names)}개: {', '.join(names)}")
    print(f"\n{'유형':<16}{'실제':>6}{'목표':>6}")
    for k, (a, b) in st["type"].items():
        print(f"{k:<16}{a:>6}{b:>6}")
    print(f"\n서로 다른 회사 {st['n_companies']}개 / 표본 {st['n_sampled']}문항 "
          f"(후보 풀에 {st['n_companies_in_pool']}개 · 한 회사 최대 {st['max_per_company']}문항)")
    print(f"금융·보험 + 엔터테인먼트 문항 {st['focus_sector_count']}개 (목표 {FOCUS_MIN}개 이상)")
    print(f"doc_info 에 접수번호가 적힌 문항 {st['n_with_rcept_hint']}개 "
          f"(필터로는 안 쓴다 — grep 범위를 좁힐 때만 참고)")
    print("\n업종별: " + " · ".join(f"{k} {v}" for k, v in st["sectors"].items()))
    print("\n공시그룹 배분은 미리 못 맞춘다(문항에 그 정보가 없다). "
          "라벨링이 끝나면 정답 청크의 실제 공시그룹으로 사후 집계한다.")


# ---------------------------------------------------------------- 라벨링 화면


def load_todo() -> list[dict]:
    if not TODO.exists():
        raise SystemExit(f"{TODO} 가 없다. 먼저 python eval/label_helper.py --sample")
    return [json.loads(l) for l in TODO.read_text(encoding="utf-8").splitlines() if l.strip()]


def labeled_qids() -> set:
    if not GOLDEN.exists():
        return set()
    out = set()
    for l in GOLDEN.read_text(encoding="utf-8").splitlines():
        if l.strip():
            out.add(json.loads(l)["qid"])
    return out


def show_hits(res: dict) -> list:
    hits = res.get("results") or []
    print(f"후보 {res.get('n_candidates', 0):,}건 → pool {res.get('n_pool', 0)}건 · "
          f"status={res.get('status')}"
          + (f" · {res['note']}" if res.get("note") else ""))
    print(BAR)
    for i, h in enumerate(hits):
        src = h.get("source") or {}
        d = h.get("score_detail") or {}
        body = _WS.sub(" ", (h.get("text") or ""))[:PREVIEW]
        print(f"[{i:>2}] vec={d.get('vec'):<9} bm25={d.get('bm25'):<10} "
              f"{src.get('doc_group')} · {src.get('report_nm')} · {src.get('rcept_no')}"
              f"{'  ※정정전' if h.get('preserved') else ''}")
        print(f"     § {src.get('section')}")
        print(f"     {body}")
    if not hits:
        print("(검색 결과 없음)")
    print(BAR)
    return hits


def grep_company(rt, stock_code, needles, rcept_hint=()) -> None:
    """검색기를 거치지 않고 그 회사의 원문을 직접 훑는다.

    이게 없으면 '검색기가 top-20 에 올려준 것' 중에서만 정답을 고르게 되고,
    정답이 코퍼스에 있는데 못 찾은 경우를 사람이 확인할 방법이 없다.
    그러면 그 문항이 조용히 사라져 recall 이 실제보다 높게 나온다.
    숫자는 콤마 있는 형태와 없는 형태를 모두 시도한다.
    """
    import numpy as np

    want = set()
    for n in needles:
        n = n.strip()
        if not n:
            continue
        want.add(n)
        if re.fullmatch(r"[\d,]+(?:\.\d+)?", n):
            bare = n.replace(",", "")
            want.add(bare)
            head, _, tail = bare.partition(".")
            if head.isdigit():
                want.add(f"{int(head):,}" + (f".{tail}" if tail else ""))
    if not want:
        print("[grep] 찾을 문자열이 없다")
        return

    rows = np.flatnonzero((rt.stock_code == stock_code) & rt.is_latest)
    print(f"[grep] {len(rows):,}개 청크에서 {sorted(want)} 를 찾는다 (최신본만)")
    t0 = time.time()
    n = 0
    for row in rows:
        rec = rt.store.get(int(row))
        content = rec.get("content") or ""
        got = [x for x in want if x in content]
        if not got:
            continue
        n += 1
        if n <= GREP_MAX_HITS:
            no = str(rt.receipt_no[row])
            mark = " ←doc_info" if no in rcept_hint else ""
            pos = content.find(got[0])
            print(f"  {rec.get('chunk_id')}  [{rt.doc_group[row]} · {no}{mark}]")
            print(f"    § {' > '.join(rec.get('section_path') or [])}")
            print(f"    …{_WS.sub(' ', content[max(0, pos - 60):pos + 140])}…")
    extra = f" (앞 {GREP_MAX_HITS}개만 표시)" if n > GREP_MAX_HITS else ""
    print(f"[grep] {n}개 청크에서 발견{extra} · {time.time() - t0:.1f}초"
          if n else f"[grep] 이 회사 문서에 없다 · {time.time() - t0:.1f}초")


HELP = """  0 또는 0,3   정답 청크 번호(여러 개 가능)      n  top-20 안에 정답 없음
  g            ground_truth 의 숫자를 전부 원문에서 찾는다
  g <문자열>   그 문자열을 원문에서 찾는다(콤마 유무 모두 시도)
  e            검색어를 직접 입력해 재검색            s  보류(나중에 다시)
  a            정답(ground_truth) 다시 보기          ?  도움말
  q            저장하고 종료"""


def label(dry_run=False) -> int:
    todo = load_todo()
    done = labeled_qids()
    pending = [t for t in todo if t["qid"] not in done]
    print(f"전체 {len(todo)}문항 · 완료 {len(done)} · 남은 {len(pending)}")
    if not pending:
        print("남은 문항이 없다.")
        return 0

    from src.search.aliases import resolve_company
    from src.search.hybrid import Retriever
    rt = Retriever()

    queue = list(pending)
    deferred = []
    written = 0
    while queue:
        t = queue.pop(0)
        query = t["search_query"]
        card = resolve_company(t["corp"])
        stock = card.get("stock_code")
        while True:
            print(f"\n{'=' * 78}")
            print(f"[{t['n']}/{len(todo)}] {t['corp']} · {t['type']} · {t.get('sector')}")
            print(f"{'=' * 78}")
            print("질문:")
            for line in _wrap(t["question"]):
                print("  " + line)
            print("\n정답(ground_truth):")
            for line in _wrap(t["ground_truth"]):
                print("  " + line)
            if t.get("doc_info"):
                print("\ndoc_info(참고, 필터로는 안 씀):")
                for line in _wrap(t["doc_info"]):
                    print("  " + line)
            print(f"\n검색어(변환): {query}")
            if query != t["search_query"]:
                print(f"  (원래 변환값: {t['search_query']})")
            print(BAR)
            t0 = time.time()
            res = rt.search(query, corp_name=t["corp"], top_k=TOP_K,
                            pool=max(TOP_K, 50), per_doc=TOP_K)
            hits = show_hits(res)
            print(f"(검색 {time.time() - t0:.2f}초)")
            if dry_run:
                print("\n--dry-run 이라 여기서 멈춘다. 라벨은 기록하지 않았다.")
                return 0
            print(HELP)
            try:
                cmd = input("입력> ").strip()
            except EOFError:
                print("\n입력이 끊겼다. 저장하고 종료한다.")
                return 0

            if cmd == "?":
                continue
            if cmd == "a":
                continue
            if cmd == "q":
                print(f"\n저장하고 종료. 이번 세션에 {written}건 기록.")
                return 0
            if cmd == "s":
                deferred.append(t)
                break
            if cmd == "e":
                new = input("새 검색어> ").strip()
                if new:
                    query = new
                continue
            if cmd == "g" or cmd.startswith("g "):
                arg = cmd[2:].strip() if len(cmd) > 1 else ""
                needles = ([arg] if arg
                           else _NUMBER.findall(t["ground_truth"] or ""))
                if not stock:
                    print(f"[grep] 회사명을 못 풀었다: {card.get('status')}")
                else:
                    grep_company(rt, stock, needles, set(t.get("rcept_nos") or ()))
                continue
            if cmd == "n":
                _write(t, query, [], [], ["not_in_top20"], "")
                written += 1
                break
            ids = _parse_ids(cmd, len(hits))
            if ids is None:
                print("!! 알 수 없는 입력. '?' 로 도움말")
                continue
            gold = [hits[i]["chunk_id"] for i in ids]
            # 공시그룹은 문항이 아니라 **사람이 고른 청크가 실제로 있던 공시**에서 온다
            groups = sorted({(hits[i].get("source") or {}).get("doc_group")
                             for i in ids if (hits[i].get("source") or {}).get("doc_group")})
            note = input("메모(없으면 엔터)> ").strip()
            tags = ["edited_query"] if query != t["search_query"] else []
            _write(t, query, gold, groups, tags, note)
            written += 1
            print(f"기록: {gold} · 공시그룹 {groups}")
            break

    if deferred:
        print(f"\n보류 {len(deferred)}문항: " + ", ".join(d["qid"] for d in deferred))
    print(f"이번 세션에 {written}건 기록 → {GOLDEN}")
    return 0


def _parse_ids(cmd: str, n: int):
    if not cmd or not re.fullmatch(r"\d+(\s*,\s*\d+)*", cmd):
        return None
    ids = [int(x) for x in cmd.split(",")]
    if any(i < 0 or i >= n for i in ids):
        print(f"!! 0~{n - 1} 범위를 벗어났다")
        return None
    return sorted(set(ids))


def _write(t: dict, query: str, gold: list, groups: list, tags: list, note: str) -> None:
    rec = {
        "qid": t["qid"], "question": t["question"],
        "ground_truth": t["ground_truth"],
        # 접수일은 안 준다. benchmark_*.json 은 문항별 문서를 특정하지 않는다.
        "filters": {"corp_name": t["corp"], "rcept_dt": None, "fiscal_year": None},
        "search_query": query, "gold_chunk_ids": gold,
        "answer_type": t["type"],
        "doc_group": groups[0] if len(groups) == 1 else None,
        "doc_groups": groups,
        "tags": sorted(set(tags + [t["type"]] + groups)),
        "labeled_at": datetime.now().isoformat(timespec="seconds"), "note": note,
    }
    with open(GOLDEN, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _wrap(s: str, w: int = 74) -> list[str]:
    out, line = [], ""
    for word in _WS.sub(" ", s).split(" "):
        if len(line) + len(word) + 1 > w:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="골든셋 라벨링 도구")
    ap.add_argument("--sample", action="store_true", help="30문항 표본추출")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--show-queries", action="store_true", help="검색어 변환 전후만 출력")
    ap.add_argument("--dry-run", action="store_true", help="첫 문항 화면만 띄우고 종료")
    a = ap.parse_args()

    if a.sample:
        st = build_todo(a.seed)
        print_sample_stats(st)
        print(f"\n{TODO} 기록 — {st['n_sampled']}문항")
        return 0

    if a.show_queries:
        for t in load_todo():
            print(f"[{t['n']:>2}] {t['corp']} · {t['type']}")
            print(f"  전: {t['question']}")
            print(f"  후: {t['search_query']}\n")
        return 0

    return label(dry_run=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
