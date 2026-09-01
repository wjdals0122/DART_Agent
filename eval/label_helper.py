"""골든셋 라벨링 도구 — 사람이 정답 청크를 고른다.

**질문도 정답도 여기서 만들지 않는다.** 질문은 `bench_q_part*.json` 에 있는 것을 쓰고,
정답 청크는 화면에 띄운 실제 검색 결과 중에서 사람이 번호로 고른다.
자동으로 정답을 정하는 코드는 이 파일 어디에도 없다.

  python eval/label_helper.py --sample          # 30문항 표본추출 -> eval/golden_todo.jsonl
  python eval/label_helper.py                   # 라벨링 시작(이어서 진행)
  python eval/label_helper.py --dry-run         # 첫 문항 화면만 띄우고 종료(기록 안 함)
  python eval/label_helper.py --show-queries    # 검색어 변환 전후 목록만 출력

【왜 문항의 query 를 그대로 검색에 안 넣는가】
문항은 "LG이노텍이 20250813에 접수한 반기보고서 (2025.06)을 기준으로, …" 처럼
회사·날짜·보고서명을 문장 안에 품고 있다. 그건 이미 **하드필터로 주는 값**이다.
질의 문자열에 또 넣으면 BM25 가 회사명·날짜 토큰에 점수를 몰아주고,
정작 "무엇을 묻는지"(요약연결재무정보 영업이익)는 묻혀 버린다.
그래서 필터로 줄 값은 빼고 물음의 알맹이만 남긴다. 규칙은 to_search_query() 에 있다.

변환은 어디까지나 기계 규칙이라 틀릴 수 있다. 그래서 화면에 **변환 전후를 같이** 띄우고,
'e' 로 사람이 검색어를 직접 고쳐 재검색할 수 있게 했다.
"""
from __future__ import annotations

import argparse
import csv
import glob
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

# bench_q_part*.json 을 찾을 곳. eval/ 을 먼저 보고, 없으면 Question/ 아래를 뒤진다.
BENCH_GLOBS = ("eval/bench_q_part*.json",
               "Question/**/bench_q_part*.json",
               "bench_q_part*.json")

# 검색기 책임 밖인 유형은 후보에서 뺀다.
# multi_open·complex_* 는 여러 문서를 읽고 계산·서술하는 문제라 '정답 청크' 가 하나로
# 떨어지지 않는다. 검색 품질을 재는 데 쓰면 지표가 흐려진다.
KEEP_TYPES = ("search_closed", "search_open", "multi_closed")

SEED = 20260904                      # 재현용 고정 시드
N_TRIALS = 4000                      # 배분 탐색 재시도 횟수
N_SAMPLE = 30

QUOTA_GROUP = {"periodic": 12, "exchange": 8, "major": 5, "holding": 5}
QUOTA_TYPE = {"search_closed": 12, "search_open": 10, "multi_closed": 8}
FOCUS_SECTORS = ("금융·보험", "엔터테인먼트")   # '금융·보험·엔터'
FOCUS_MIN = 5

TOP_K = 20
# 검색기 기본값은 한 문서에서 2청크까지만 올린다(PER_DOC=2). 결과를 한 문서가
# 독점하지 못하게 하려는 것인데, 여기서는 **접수일로 문서를 하나로 좁혀 놓고**
# 그 안에서 정답 청크를 고르는 상황이라 그 상한이 그대로 걸리면 후보가 2~6개뿐이다.
# 라벨링에서는 상한을 풀어 top-20 을 실제로 채운다.
PREVIEW = 200                        # content 미리보기 글자 수
BAR = "─" * 78


# ---------------------------------------------------------------- 문항 읽기


def find_bench_files() -> list[Path]:
    for pat in BENCH_GLOBS:
        hits = sorted(ROOT.glob(pat))
        if hits:
            return hits
    return []


def load_bench() -> tuple[list[dict], list[Path]]:
    files = find_bench_files()
    if not files:
        raise SystemExit(
            "bench_q_part*.json 을 찾지 못했다. 찾아본 곳: "
            + ", ".join(BENCH_GLOBS) + "\n문항 파일이 없으면 아무것도 만들 수 없다. 중단한다.")
    rows: list[dict] = []
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise SystemExit(f"{f} 가 리스트가 아니다({type(data).__name__}). 중단한다")
        for r in data:
            missing = [k for k in ("id", "corp", "grp", "type", "query", "rcept_dt")
                       if not r.get(k)]
            if missing:
                raise SystemExit(f"{f} 의 문항 {r.get('id')} 에 {missing} 가 없다. "
                                 f"기본값으로 채우지 않고 중단한다")
            r["_file"] = f.name
            rows.append(r)
    dup = [k for k, v in Counter(r["id"] for r in rows).items() if v > 1]
    if dup:
        raise SystemExit(f"문항 id 중복 {len(dup)}건: {dup[:5]}")
    return rows, files


def load_sectors() -> dict[str, str]:
    if not UNIVERSE.exists():
        raise SystemExit(f"{UNIVERSE} 가 없다. 업종 배분을 확인할 수 없어 중단한다")
    with open(UNIVERSE, encoding="utf-8-sig") as fh:
        return {r["corp_name"]: r.get("sector", "") for r in csv.DictReader(fh)}


# ---------------------------------------------------------------- 검색어 변환
#
# 규칙 순서가 중요하다. 긴 덩어리(가상 경쟁사 절, 보고서명)를 먼저 지우고,
# 그 다음에 짧은 조각(날짜, 회사명)을 지운다. 반대로 하면 덩어리가 안 잡힌다.

# (1) 가상의 비교 대상 절 — multi_closed 에만 나온다. "가상의 경쟁사 '대한이엔씨'와
#     비교해서…" 는 코퍼스에 없는 내용이라, 남겨두면 없는 회사를 찾아 헤맨다.
#     쉼표로 끊어 '가상' 이 든 마디를 통째로 버린다. 숫자 안의 쉼표(2,000)로는 안 끊는다.
_SEG = re.compile(r"(?<!\d)[,、](?!\d)")


def drop_hypothetical(text: str) -> str:
    segs = _SEG.split(text)
    kept = [g for g in segs if "가상" not in g]
    return ", ".join(kept) if kept else text

# (2) 날짜 + 접수/공시 동사
_DATE_ACT = re.compile(
    r"\d{8}\s*(?:자로|자|에|일에|일자로)?\s*"
    r"(?:접수(?:된|한)?|공시(?:된|한)?|제출(?:된|한)?|올린|낸|제출)?")
_YM = re.compile(r"20\d{2}\s*년(?:\s*\d{1,2}\s*월)?(?:\s*기준)?")

# (3) 질문 껍데기. 물음의 알맹이가 아니라 지시문이다.
_BOILER = [
    r"을\s*기준으로", r"를\s*기준으로", r"에\s*근거하여", r"을\s*근거로", r"를\s*근거로",
    r"공시\s*본문에\s*근거하여", r"관련해서", r"관련하여", r"에\s*나온", r"에\s*나와\s*있는",
    r"각각\s*제시하시오", r"제시하시오", r"서술하시오", r"산출하시오", r"계산하시오",
    r"검산하시오", r"작성하시오", r"정리해서\s*알려줘", r"알려줄래\??", r"알려줘",
    r"정리해줄래\??", r"정리해줘", r"뽑아줘", r"봐줄래\??", r"풀어줘", r"해봐라", r"하라",
    r"함께\s*계산하라", r"단위와\s*함께", r"숫자만\s*딱\s*집어서", r"개조식으로",
    r"알기\s*쉽게", r"쉽게", r"좀\s*", r"직접\s*계산", r"직접\s*검산", r"검산해봐라",
    r"포함할\s*것", r"함께\s*밝히", r"몇\s*퍼센트로\s*적혀\s*있어\??", r"적혀\s*있어\??",
    r"얼마인지", r"몇\s*%p", r"%p로", r"어느\s*쪽이\s*얼마나\s*큰지",
]
_BOILER_RE = re.compile("|".join(_BOILER))

# (4) 남은 토큰 중 버릴 일반 어휘. 어느 문항에나 나와서 변별력이 없다.
STOPWORDS = {
    "기준", "금액", "단위", "각각", "숫자", "정확", "얼마", "퍼센트", "내용", "관련",
    "제시", "서술", "산출", "계산", "검산", "정리", "비교", "격차", "차액", "배수",
    "기록", "가상", "경쟁사", "동종업계", "기관투자자",
    "직전", "이번", "회사", "공시", "보고서", "제출", "접수", "다음",
    "포함", "여부", "일치", "가지", "부분", "사항", "문서",
    # '해당하는지', '이용해', '차지하는', '기재 비율' 처럼 질문 문장을 잇는 말
    "해당", "이용", "차지", "기재",
    "년", "월", "일", "것", "수", "때", "등", "및", "좀", "안", "위", "중",
}

# 명사만 남긴다. 동사·형용사 어간은 질문 껍데기에서 오는 게 대부분이다.
_NOUN_TAGS = {"NNG", "NNP", "SL", "SH", "SN"}
_WS = re.compile(r"\s+")
_PAREN = re.compile(r"[「」『』（）()\[\]{}<>《》〈〉]")


def _strip_company(text: str, corp: str) -> str:
    """회사명과 그 변형을 지운다. 별칭 사전이 있으면 같이 쓴다."""
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
    """문항 문장 -> 검색어. 필터로 줄 값(회사·날짜·보고서명)과 지시문을 걷어낸다.

    순서:
      1. 가상의 비교 대상이 든 마디 삭제 (multi_closed)
      2. 보고서명 삭제 — 이미 rcept_dt 필터로 문서를 특정한다
      3. 날짜 + 접수/공시 동사, 연·월 표현 삭제
      4. 회사명(별칭 포함) 삭제
      5. 질문 껍데기(지시문) 삭제
      6. Kiwi 로 명사만 추출 -> 불용어 제거 -> 중복 제거 -> 앞에서 8개
    """
    t = rec.get("query") or ""
    t = drop_hypothetical(t)

    rn = rec.get("report_nm") or ""
    if rn:
        t = t.replace(rn, " ")
        base = re.sub(r"\s*\(.*?\)\s*", "", rn).strip()      # '사업보고서 (2025.12)' -> '사업보고서'
        if len(base) >= 3:
            t = t.replace(base, " ")

    t = _DATE_ACT.sub(" ", t)
    t = _YM.sub(" ", t)
    t = _strip_company(t, rec.get("corp") or "")
    t = _BOILER_RE.sub(" ", t)
    t = _PAREN.sub(" ", t)

    toks = _nouns(t)
    out, seen = [], set()
    for w in toks:
        if w in STOPWORDS or len(w) < 2 or w.isdigit():
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 8:
            break
    # 규칙이 전부 걷어내 버린 경우엔 원문을 그대로 쓴다. 빈 검색어를 넘기지 않는다.
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


def _cell_plan(rng, cap_of):
    """(공시그룹, 유형) 칸마다 몇 문항을 뽑을지 정한다.

    행 합 = QUOTA_GROUP, 열 합 = QUOTA_TYPE 인 표를 만든다. 칸의 상한은
    **그 (그룹,유형) 문항을 가진 회사 수**다 — 한 회사가 같은 칸에서 두 문항을 낼 일은
    없기 때문이다(문항 생성기가 회사·그룹·유형마다 하나씩 만들었다).
    무작위로 한 칸씩 채우다 막히면 실패를 돌려주고, 부르는 쪽이 다시 시도한다.
    """
    need_g = dict(QUOTA_GROUP)
    need_t = dict(QUOTA_TYPE)
    plan = Counter()
    for _ in range(N_SAMPLE):
        opts = [(g, t) for g in need_g for t in need_t
                if need_g[g] > 0 and need_t[t] > 0 and plan[(g, t)] < cap_of.get((g, t), 0)]
        if not opts:
            return None
        g, t = rng.choice(opts)
        plan[(g, t)] += 1
        need_g[g] -= 1
        need_t[t] -= 1
    return plan


def _assign(plan, by_cell, caps, rng):
    """칸마다 필요한 만큼 **서로 다른 회사**를 고른다. 회사별 상한은 caps 로 준다.

    최대유량으로 푼다.  source → 칸(용량 plan) → 회사(용량 1) → sink(용량 caps[회사])
    칸→회사 용량이 1이라 **같은 칸에서 같은 회사가 두 번 뽑히는 일이 없다.**
    (앞서 이분매칭으로 짰을 때 실제로 HMM 문항 하나가 두 번 뽑혔다.)
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
    g = Counter(r["grp"] for r in pick)
    ty = Counter(r["type"] for r in pick)
    per = Counter(r["corp"] for r in pick)
    focus = sum(1 for r in pick if sectors.get(r["corp"], "") in FOCUS_SECTORS)
    dev_g = sum(abs(g[k] - v) for k, v in QUOTA_GROUP.items())
    dev_t = sum(abs(ty[k] - v) for k, v in QUOTA_TYPE.items())
    # 회사 쏠림은 '최대 몇 번' 보다 '2회를 넘긴 총합' 으로 잰다.
    # 한 회사만 3문항인 것과 여러 회사가 3문항인 것을 구별해야 한다.
    excess = sum(max(0, v - 2) for v in per.values())
    return (N_SAMPLE - len(pick),                  # 30개를 못 채우면 최악
            dev_g, dev_t,
            max(0, FOCUS_MIN - focus),             # 금융·엔터 부족분
            excess,
            -len(per))                             # 서로 다른 회사 수는 많을수록 좋다


def sample(cands, sectors, seed=SEED, trials=N_TRIALS):
    """고정 시드로 재현 가능한 표본. 배분을 정확히 못 맞추면 가장 가까운 것을 고른다.

    회사 수 상한(cap)을 2부터 올려 가며 찾는다. 회사가 적어 30문항을 서로 다른 회사로
    채울 수 없으면 cap 이 올라가고, 그 사실은 보고에 그대로 남는다.
    """
    rng = random.Random(seed)
    idx: dict = {}
    for r in cands:
        idx.setdefault((r["grp"], r["type"]), {}).setdefault(r["corp"], r)
    by_cell = {k: sorted(v) for k, v in idx.items()}
    cap_of = {k: len(v) for k, v in by_cell.items()}

    corps = sorted({r["corp"] for r in cands})
    focus = [c for c in corps if sectors.get(c, "") in FOCUS_SECTORS]

    # 상한을 조금씩만 푼다. 무작정 3으로 올리면 아무 회사나 3문항이 되는데,
    # 실제로 3문항이 필요한 곳은 '금융·보험 + 엔터 5문항 이상' 을 맞춰야 하는
    # 업종뿐이다(후보 풀에 그 업종 회사가 2곳밖에 없다).
    ladders = [{c: 2 for c in corps}]
    if focus:
        ladders.append({c: (3 if c in focus else 2) for c in corps})
    for n in (3, 4, 5, N_SAMPLE):
        ladders.append({c: n for c in corps})

    best = None
    for caps in ladders:
        for _ in range(max(1, trials // 4)):
            plan = _cell_plan(rng, cap_of)
            if plan is None:
                continue
            got = _assign(plan, by_cell, caps, rng)
            if got is None:
                continue
            pick = [idx[cell][corp] for cell, corp in got]
            if len({r["id"] for r in pick}) != len(pick):
                continue                          # 같은 문항이 두 번 들어오면 버린다
            p = _penalty(pick, sectors)
            if best is None or p < best[0]:
                best = (p, pick)
        if best is not None and best[0][:4] == (0, 0, 0, 0):
            break
    if best is None:
        raise SystemExit("배분 조건을 만족하는 표본을 못 찾았다. 문항 수가 모자란다")
    return best[1]


def build_todo(seed=SEED) -> dict:
    rows, files = load_bench()
    sectors = load_sectors()
    cands = [r for r in rows if r["type"] in KEEP_TYPES]
    unknown = sorted({r["corp"] for r in cands if r["corp"] not in sectors})
    if unknown:
        print(f"[경고] universe.csv 에 없는 회사 {len(unknown)}개: {unknown}", file=sys.stderr)

    pick = sample(cands, sectors, seed)
    pick.sort(key=lambda r: (r["grp"], r["type"], r["corp"]))

    with open(TODO, "w", encoding="utf-8") as fh:
        for i, r in enumerate(pick, 1):
            fh.write(json.dumps({
                "n": i, "qid": r["id"], "corp": r["corp"], "code": r.get("code"),
                "grp": r["grp"], "type": r["type"], "report_nm": r.get("report_nm"),
                "rcept_dt": r["rcept_dt"], "sector": sectors.get(r["corp"], ""),
                "question": r["query"], "search_query": to_search_query(r),
                "src_file": r["_file"],
            }, ensure_ascii=False) + "\n")

    g = Counter(r["grp"] for r in pick)
    ty = Counter(r["type"] for r in pick)
    per = Counter(r["corp"] for r in pick)
    sec = Counter(sectors.get(r["corp"], "?") for r in pick)
    stats = {
        "seed": seed, "trials": N_TRIALS,
        "files": [f.name for f in files],
        "n_all": len(rows), "n_candidates": len(cands), "n_sampled": len(pick),
        "n_companies_in_pool": len({r["corp"] for r in cands}),
        "group": {k: [g.get(k, 0), v] for k, v in QUOTA_GROUP.items()},
        "type": {k: [ty.get(k, 0), v] for k, v in QUOTA_TYPE.items()},
        "n_companies": len(per), "max_per_company": max(per.values()),
        "over_twice": sum(max(0, v - 2) for v in per.values()),
        "repeated": {k: v for k, v in per.items() if v > 2},
        "focus_sector_count": sum(v for k, v in sec.items() if k in FOCUS_SECTORS),
        "sectors": dict(sec.most_common()),
        "per_company": dict(per.most_common()),
    }
    return stats


def print_sample_stats(st: dict) -> None:
    print(f"\n표본 시드 = {st['seed']}  (재현하려면 --seed {st['seed']})")
    print(f"문항 파일 {len(st['files'])}개 · 전체 {st['n_all']}문항 → "
          f"후보 {st['n_candidates']}문항({'/'.join(KEEP_TYPES)}) → 표본 {st['n_sampled']}문항")
    print(f"\n{'공시그룹':<12}{'실제':>6}{'목표':>6}   {'유형':<16}{'실제':>6}{'목표':>6}")
    gk, tk = list(st["group"].items()), list(st["type"].items())
    for i in range(max(len(gk), len(tk))):
        a = f"{gk[i][0]:<12}{gk[i][1][0]:>6}{gk[i][1][1]:>6}" if i < len(gk) else " " * 24
        b = (f"   {tk[i][0]:<16}{tk[i][1][0]:>6}{tk[i][1][1]:>6}") if i < len(tk) else ""
        print(a + b)
    print(f"\n서로 다른 회사 {st['n_companies']}개 (후보 풀에 {st['n_companies_in_pool']}개 "
          f"· 한 회사 최대 {st['max_per_company']}문항)")
    print(f"금융·보험 + 엔터테인먼트 문항 {st['focus_sector_count']}개 (목표 {FOCUS_MIN}개 이상)")
    print("\n업종별: " + " · ".join(f"{k} {v}" for k, v in st["sectors"].items()))


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
              f"{'※정정전' if h.get('preserved') else ''}")
        print(f"     § {src.get('section')}")
        print(f"     {body}")
    if not hits:
        print("(검색 결과 없음)")
    print(BAR)
    return hits


def grep_doc(rt, rcept_no: str, needle: str) -> None:
    """검색기를 거치지 않고 문서 안 원문을 직접 훑는다.

    이게 없으면 '검색기가 top-20 안에 넣어준 것' 중에서만 정답을 고르게 되고,
    그러면 recall 이 실제보다 높게 나온다. 정답이 코퍼스에 있는데 검색기가 못 찾은
    경우를 'not_in_top20' 으로 정확히 기록하려면 원문을 직접 볼 수 있어야 한다.
    숫자는 콤마 있는 형태와 없는 형태를 모두 시도한다.
    """
    import numpy as np

    needles = {needle}
    if re.fullmatch(r"[\d,]+", needle):
        bare = needle.replace(",", "")
        needles.add(bare)
        if bare.isdigit():
            needles.add(f"{int(bare):,}")
    rows = np.flatnonzero(rt.receipt_no == str(rcept_no))
    print(f"[grep] 접수번호 {rcept_no} · 청크 {len(rows):,}개 · "
          f"찾는 문자열 {sorted(needles)}")
    n = 0
    for row in rows:
        rec = rt.store.get(int(row))
        content = rec.get("content") or ""
        if any(x in content for x in needles):
            n += 1
            sec = " > ".join(rec.get("section_path") or [])
            pos = min((content.find(x) for x in needles if x in content), default=0)
            print(f"  {rec.get('chunk_id')}")
            print(f"    § {sec}")
            print(f"    …{_WS.sub(' ', content[max(0, pos - 60):pos + 120])}…")
    print(f"[grep] {n}개 청크에서 발견" if n else "[grep] 이 문서에 없다")


def rcept_no_of(rt, corp_stock, rcept_dt):
    import numpy as np
    m = (rt.rcept_dt == str(rcept_dt))
    if corp_stock:
        m &= (rt.stock_code == corp_stock)
    rows = np.flatnonzero(m)
    return sorted({str(rt.receipt_no[r]) for r in rows})


HELP = """  0 또는 0,3   정답 청크 번호(여러 개 가능)      n  top-20 안에 정답 없음
  s            보류(나중에 다시)                   e  검색어를 직접 입력해 재검색
  g <문자열>   원문 직접 찾기(콤마 유무 모두 시도)  ?  도움말
  q            저장하고 종료"""


def label(dry_run=False) -> int:
    todo = load_todo()
    done = labeled_qids()
    pending = [t for t in todo if t["qid"] not in done]
    print(f"전체 {len(todo)}문항 · 완료 {len(done)} · 남은 {len(pending)}")
    if not pending:
        print("남은 문항이 없다.")
        return 0

    from src.search.hybrid import Retriever
    rt = Retriever()

    queue = list(pending)
    deferred = []
    written = 0
    while queue:
        t = queue.pop(0)
        query = t["search_query"]
        while True:
            print(f"\n{'=' * 78}")
            print(f"[{t['n']}/{len(todo)}] {t['corp']} · {t.get('report_nm')} · "
                  f"접수 {t['rcept_dt']} · {t['type']} · {t['grp']} · {t.get('sector')}")
            print(f"{'=' * 78}")
            print("원 질문:")
            for line in _wrap(t["question"]):
                print("  " + line)
            print(f"\n검색어(변환): {query}")
            if query != t["search_query"]:
                print(f"  (원래 변환값: {t['search_query']})")
            print(BAR)
            t0 = time.time()
            res = rt.search(query, corp_name=t["corp"], rcept_dt=t["rcept_dt"],
                            top_k=TOP_K, pool=max(TOP_K, 50), per_doc=TOP_K)
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
            if cmd.startswith("g "):
                needle = cmd[2:].strip()
                nos = rcept_no_of(rt, res.get("filters", {}).get("stock_code"),
                                  t["rcept_dt"])
                if not nos:
                    print("[grep] 그 접수일의 문서를 못 찾았다")
                for no in nos:
                    grep_doc(rt, no, needle)
                continue
            if cmd == "n":
                _write(t, query, [], ["not_in_top20"], "")
                written += 1
                break
            ids = _parse_ids(cmd, len(hits))
            if ids is None:
                print("!! 알 수 없는 입력. '?' 로 도움말")
                continue
            gold = [hits[i]["chunk_id"] for i in ids]
            note = input("메모(없으면 엔터)> ").strip()
            tags = ["edited_query"] if query != t["search_query"] else []
            _write(t, query, gold, tags, note)
            written += 1
            print(f"기록: {gold}")
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


def _write(t: dict, query: str, gold: list, tags: list, note: str) -> None:
    rec = {
        "qid": t["qid"], "question": t["question"],
        "filters": {"corp_name": t["corp"], "rcept_dt": t["rcept_dt"],
                    "fiscal_year": None},
        "search_query": query, "gold_chunk_ids": gold,
        "answer_type": t["type"], "doc_group": t["grp"],
        "tags": sorted(set(tags + [t["type"], t["grp"]])),
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
            print(f"[{t['n']:>2}] {t['corp']} · {t['type']} · {t['grp']}")
            print(f"  전: {t['question']}")
            print(f"  후: {t['search_query']}\n")
        return 0

    return label(dry_run=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
