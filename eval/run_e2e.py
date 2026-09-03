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

【비용은 입력·출력을 나눠서 센다】
HCX-005 는 입력 1,000토큰 1.25원, 출력 1,000토큰 5원이다. **출력이 4배 비싸다.**
이 Agent 는 도구 결과를 프롬프트에 계속 쌓으므로 입력 토큰이 압도적으로 많지만,
출력이 조금만 길어져도 비용은 빨리 오른다. 리포트는 둘을 따로 보여준다.

【채점은 여기서 하지 않는다 — 자동 채점은 폐기했다】
정답의 숫자가 답변에 몇 개 있는지 세는 방식을 잠정 채점으로 써 봤지만 못 쓴다.
정답 문자열에 "※ 오답 유형: … 를 답하면 오답" 형태의 **반례 숫자**가 섞여 있어
분모가 부풀려진다(40문항에서 일치율 0.5 초과 1건, 최고 0.857). 그래서 리포트에서
"정확도" 를 뺐고, 기계로 틀림없이 알 수 있는 것만 센다 — 근거 확보율·도구 성적·
타사 출처 인용·거절 횟수. 맞고 틀림은 reports/<run>_review.md 를 사람이 판정한다.

【표본은 한 번 정하고 얼어붙는다 — eval/e2e_sample.jsonl】
평가 표본이 실행할 때마다 달라지면 앞뒤 결과를 비교할 수 없다. 그래서 40문항을
한 번 뽑아 `eval/e2e_sample.jsonl` 에 적어 두고, 이후 실행은 **그 파일에서만** 고른다.
파일이 없으면 시드로 뽑아 만들고, 있으면 읽는다(문항 원본이 바뀌어 사라진 qid 가
있으면 지어내지 않고 경고한다).

5문항 스모크와 40문항 전량이 **같은 run 이름**을 쓰면 결과가 한 파일에 쌓이고,
`--resume` 이 이미 끝낸 문항을 건너뛴다. 스모크 5건을 다시 돌릴 필요가 없다.

  python eval/run_e2e.py --run e2e_sample --n 5 --types search_closed … --yes
  python eval/run_e2e.py --run e2e_sample --n 40 --resume --yes   # 남은 35건만

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
import traceback
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
SAMPLE_PATH = ROOT / "eval" / "e2e_sample.jsonl"
REPORTS = ROOT / "reports"
TEMPLATE_FILE = "benchmark_master_templates_24.json"
SCRIPT = "run_e2e"
BAR = "=" * 78

# 429 는 이 프로젝트에서 가장 흔한 실패다. 연속 호출 사이 20~30초가 안전하다(실측).
DEFAULT_SLEEP = 20.0
DEFAULT_MAX_COST = 5000.0

_NUM = re.compile(r"\d[\d,]{2,}(?:\.\d+)?")


# ---------------------------------------------------------------- 회사명 주입
#
# 【왜 필요한가 — 실측】
# 벤치 문항 208/240(87%)이 질문 안에 회사명을 적지 않는다. `doc_info` 로 대상
# 문서가 이미 정해진 상태를 전제하고 쓰였기 때문이다("이 보고서", "이 회사").
# 회사를 모르는 질문을 그대로 던지면 Agent 가 회사를 특정하지 못하고,
# 그걸 Agent 실패로 기록하게 된다. 실제로 첫 실행에서 3/5 가 그렇게 죽었다.
#
# 【무엇을 주고 무엇을 안 주는가】
# 회사명만 준다. `doc_info` 의 접수번호·보고서명은 **주지 않는다** —
# 문서를 찾는 일은 Agent 몫으로 남겨야 검색을 평가할 수 있다.
#
# 【규칙 — 단순하게, 그리고 되돌아볼 수 있게】
#   (A) 회사명·별칭이 질문에 이미 있으면 그대로 둔다
#   (B) 없으면 "{회사}에 대해 묻습니다. " 를 문장 앞에 붙인다
# 조사(이/가, 은/는, 의)를 붙여 주어 자리에 녹이는 방법도 있지만, 문항마다
# 첫 어절 품사가 달라("제50기 …", "「II. 사업의 내용 …」", "지금 걸려 있는 …")
# 규칙이 틀리면 질문 뜻이 바뀐다. 뜻을 건드리지 않는 쪽을 골랐다.
# 변환 전후는 reports/e2e_question_rewrite.md 에 전량 남긴다.

RULE_KEEP = "그대로 (회사명이 이미 있다)"
RULE_PREFIX = "앞에 붙임"


def _alias_set(corp: str) -> set:
    """그 회사를 가리키는 이름들. 못 찾으면 회사명 하나만 돌려준다."""
    names = {corp}
    try:
        from src.search.aliases import all_companies, resolve_company

        r = resolve_company(corp)
        if r.get("status") == "ok":
            code = str(r.get("stock_code"))
            names.add(str(r.get("corp_name") or ""))
            names.add(str(r.get("listed_name") or ""))
            for c in all_companies():
                if str(c.get("stock_code")) == code:
                    names.update(str(a) for a in (c.get("aliases") or []))
    except Exception as e:                 # noqa: BLE001 — 별칭을 못 읽어도 진행한다
        print(f"[주입] 별칭을 읽지 못했다({type(e).__name__}: {e}) — 회사명만 본다",
              file=sys.stderr)
    return {n for n in names if n}


def inject_company(question: str, corp: str) -> tuple:
    """(바뀐 질문, 적용한 규칙)."""
    q = question or ""
    for n in _alias_set(corp):
        if n in q:
            return q, RULE_KEEP
    return f"{corp}에 대해 묻습니다. {q}", RULE_PREFIX


def write_rewrite_report(items: list, path=None) -> Path:
    """변환 전후 전량. 사람이 눈으로 확인할 파일."""
    path = path or (REPORTS / "e2e_question_rewrite.md")
    n_pre = sum(1 for x in items if x["inject_rule"] == RULE_PREFIX)
    L = ["# 문항 회사명 주입 — 변환 전후", "",
         f"문항 {len(items)}건 · 앞에 붙임 {n_pre}건 · 그대로 {len(items) - n_pre}건", "",
         "규칙",
         f"- **{RULE_KEEP}**: 회사명이나 별칭이 질문 안에 이미 있으면 손대지 않는다",
         f"- **{RULE_PREFIX}**: 없으면 `{{회사}}에 대해 묻습니다. ` 를 앞에 붙인다",
         "",
         "`doc_info`(접수번호·보고서명)는 주지 않는다 — 문서 탐색은 Agent 몫이다.", ""]
    for x in items:
        L += [f"## {x['qid']}  ({x['inject_rule']})", "",
              f"- **전**: {x['question_raw']}", f"- **후**: {x['question']}", ""]
    REPORTS.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8", newline="\n")
    return path


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
            q2, rule = inject_company(q, corp)
            items.append({"qid": f"{corp}__{qtype}", "corp": corp, "qtype": qtype,
                          "question": q2, "question_raw": q, "inject_rule": rule,
                          "ground_truth": gt,
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


def ensure_sample(items: list, n: int, seed: int, path=SAMPLE_PATH) -> tuple:
    """평가 표본 40문항을 파일로 고정한다. (표본, 경고).

    이미 있으면 그대로 읽는다 — 실행할 때마다 표본이 바뀌면 비교가 불가능하다.
    파일에 있는 qid 가 문항 원본에서 사라졌으면 **채우지 않고 알린다**.
    """
    by_qid = {x["qid"]: x for x in items}
    if path.is_file():
        picked, warn, missing = [], [], []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    qid = json.loads(line).get("qid")
                except ValueError:
                    warn.append(f"{path.name} 에 읽을 수 없는 줄이 있다")
                    continue
                if qid in by_qid:
                    picked.append(by_qid[qid])
                else:
                    missing.append(qid)
        if missing:
            warn.append(f"{path.name} 의 문항 {len(missing)}건이 원본에 없다: "
                        f"{missing[:3]}{' …' if len(missing) > 3 else ''}")
        warn.insert(0, f"표본 {len(picked)}건을 {path.name} 에서 읽었다(고정 표본)")
        return picked, warn

    picked = sample(items, n, seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for x in picked:
            fh.write(json.dumps({"qid": x["qid"], "corp": x["corp"],
                                 "qtype": x["qtype"], "src_file": x["src_file"]},
                                ensure_ascii=False) + "\n")
    return picked, [f"표본 {len(picked)}건을 새로 뽑아 {path.name} 에 고정했다 "
                    f"(seed={seed}). 이후 실행은 이 파일에서만 고른다"]


# ------------------------------------------------- 진단 (기계로 잴 수 있는 것만)
#
# 【자동 채점을 폐기했다】
# 정답의 숫자가 답변에 몇 개 있는지 세는 `numbers_hit` 을 잠정 채점으로 썼는데,
# 40문항을 돌려 보고 못 쓴다는 것이 확정됐다. 정답 문자열에
# "※ 오답 유형: … 를 답하면 오답" 형태의 **반례 숫자**가 잔뜩 들어 있어 분모가
# 부풀려진다(일치율 0.5 초과 1건, 최고 0.857). 임계값 artifact 이지 성능이 아니다.
# 그래서 이 리포트에서 "정확도" 라는 말을 뺐다. 맞고 틀림은 사람이 판정한다.
#
# 여기서 세는 것은 **기계가 틀림없이 알 수 있는 것**뿐이다.
#   - 근거를 올렸는가 (근거 확보율)
#   - 도구가 ok 였는가
#   - 질문의 회사가 아닌 출처를 달았는가
#   - "확인되지 않음" 을 몇 번 밝혔는가

BUCKET_SEARCH = "검색"
BUCKET_PLAN = "계획"
BUCKET_HOLD = "보류(사람)"

# 정답 문자열에서 '핵심' 과 '주의사항' 을 가르는 표시. 이 앞까지가 답이고,
# 뒤는 오답 유형·환각 방지·역질문 지침이라 판정할 때 먼저 볼 것이 아니다.
_GT_TAIL = re.compile(r"(※|\[환각 방지\]|\[역질문)")


def split_ground_truth(gt: str) -> tuple:
    """(핵심, 뒤에 붙일 주의사항). 표시가 없으면 전부 핵심이다."""
    m = _GT_TAIL.search(gt or "")
    if not m:
        return (gt or "").strip(), ""
    return (gt or "")[:m.start()].strip(), (gt or "")[m.start():].strip()


def cited_other_corp(row: dict) -> list:
    """근거로 단 출처 중 **질문의 회사가 아닌** 것. 타사 데이터 인용을 잡는다.

    source 문자열은 `[회사명 보고서명 (…), 날짜, 접수번호 …]` 모양이라
    앞쪽에 회사명이 온다. 별칭까지 보고 하나도 안 맞으면 타사로 센다.
    """
    names = _alias_set(row.get("corp") or "")
    return [src for src in (row.get("sources") or [])
            if not any(n in src for n in names)]


def refusal_count(answer: str) -> int:
    """답변 안에서 '확인되지 않음' 류를 몇 번 밝혔는지. 항목별 거절을 세는 대용."""
    a = answer or ""
    return a.count("확인되지 않") + a.count("확인할 수 없") + a.count("기재되어 있지 않")


def bucket(row: dict) -> str:
    """근거를 못 올린 문항만 기계로 가른다. 맞고 틀림은 여기서 판단하지 않는다.

      검색 — 도구는 불렀는데 근거를 하나도 못 올렸다
      계획 — 조회 자체를 안 했거나 도구를 헤맸다(한도 도달·전부 실패)
      보류 — 근거는 올렸다. 답이 맞는지는 사람이 본다(작문 문제는 여기서 갈린다)
    """
    if row.get("sources"):
        return BUCKET_HOLD
    w = row.get("waste") or {}
    calls = row.get("tool_calls") or []
    if not calls or w.get("answered_without_tools") or w.get("hit_call_limit"):
        return BUCKET_PLAN
    if all(c.get("status") in ("error", "not_found") for c in calls):
        return BUCKET_PLAN
    return BUCKET_SEARCH


def _br(t: str) -> str:
    """HTML 표 칸 안에서 줄바꿈이 보이게."""
    return (t or "").strip().replace("&", "&amp;").replace("<", "&lt;") \
                            .replace(">", "&gt;").replace("\n", "<br>")


def tool_line(row: dict) -> str:
    """도구 호출을 한 줄로. 무엇을 몇 번 불러 어떻게 됐는지."""
    calls = row.get("tool_calls") or []
    if not calls:
        return "도구 호출 없음"
    agg: dict = {}
    for c in calls:
        k = (c.get("name"), c.get("status"))
        agg[k] = agg.get(k, 0) + 1
    return " · ".join(f"{n}×{cnt}({st})" for (n, st), cnt in agg.items())


def write_review(rows: list, run_name: str) -> Path:
    """사람이 1분 안에 판정하는 파일. 정답과 답변을 **나란히** 놓는다.

    스크롤을 오가면 판정이 느려진다. 그래서 두 칸짜리 표로 붙여 놓고,
    정답의 '※ 오답 유형' 이후는 접어서 맨 아래로 내린다.
    """
    L = [f"# e2e 사람 채점 — {run_name}", "",
         f"문항 {len(rows)}건. 각 문항의 판정란을 채운다.", "",
         "- **정답**: O(맞음) / X(틀림) / 부분(일부만 맞음)",
         "- **출처**: 인용한 접수번호가 실제 근거 문서인가",
         "- **환각**: 공시에 없는 수치·회사·사실을 지어냈는가",
         "",
         "정답 칸은 **핵심만** 보여준다. `※ 오답 유형` 이후 주의사항은 각 문항 끝에 접어 뒀다.",
         "", "---", ""]

    for i, r in enumerate(sorted(rows, key=lambda x: (x["qtype"], x["qid"])), 1):
        head, tail = split_ground_truth(r.get("ground_truth") or "")
        srcs = r.get("sources") or []
        rcepts = re.findall(r"\d{14}", " ".join(srcs)) or ["없음"]
        other = cited_other_corp(r)

        L += [f"## {i}. {r['qid']}  ·  {r['qtype']}", "",
              f"**질문** — {r.get('question', '')}", "",
              "<table>",
              "<tr><th width=\"50%\">정답(핵심)</th>"
              "<th width=\"50%\">Agent 답변</th></tr>",
              f"<tr><td valign=\"top\">{_br(head)}</td>"
              f"<td valign=\"top\">{_br(r.get('answer') or '')}</td></tr>",
              "</table>", "",
              f"- 인용 접수번호 {len(rcepts) if srcs else 0}건: `{', '.join(rcepts)}`"
              + (f"  ⚠ **타사 출처 {len(other)}건**" if other else ""),
              f"- 도구: {tool_line(r)}  ·  "
              f"{U.fmt_won((r.get('usage') or {}).get('est_won'))} · {r.get('elapsed')}초",
              "",
              "**판정** — [정답: O / X / 부분]  [출처: O / X]  [환각: O / X]  [메모: ]",
              ""]
        if tail:
            L += ["<details><summary>정답 주의사항(오답 유형·환각 방지·역질문)</summary>",
                  "", "```", tail, "```", "", "</details>", ""]
        L += ["---", ""]

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{run_name}_review.md"
    path.write_text("\n".join(L), encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------- 실행


def preflight(n_items: int, max_cost: float, avg) -> dict:
    """예상 토큰·비용. 입력과 출력을 나눠서 센다(출력 단가가 4배다).

    `avg` 는 `usage.avg_split_per_item()` 의 결과(dict) 또는 None.
    실측이 없으면 기본 가정을 쓰고, 가정이라는 사실을 화면에 밝힌다.
    """
    measured = bool(avg)
    p = avg["prompt"] if measured else U.DEFAULT_PROMPT_PER_ITEM
    c = avg["completion"] if measured else U.DEFAULT_COMPLETION_PER_ITEM
    tp, tc = int(p * n_items), int(c * n_items)
    return {"n": n_items, "prompt": p, "completion": c,
            "total_prompt": tp, "total_completion": tc, "total": tp + tc,
            "won": U.won(tp, tc), "measured": measured, "max_cost": max_cost,
            "n_measured": avg["n"] if measured else 0}


def print_preflight(pf: dict) -> None:
    src = (f"token_log.jsonl 실측 평균 (문항 {pf['n_measured']}건)" if pf["measured"]
           else f"기본 가정 입력 {U.DEFAULT_PROMPT_PER_ITEM:,} + "
                f"출력 {U.DEFAULT_COMPLETION_PER_ITEM:,} — 실측 이력이 아직 없다")
    wp = pf["total_prompt"] * U.WON_PER_PROMPT_TOKEN
    wc = pf["total_completion"] * U.WON_PER_COMPLETION_TOKEN
    print(f"{BAR}\n실행 전 예상치\n{BAR}")
    print(f"  {pf['n']}문항 × 평균 입력 {pf['prompt']:,.0f} + "
          f"출력 {pf['completion']:,.0f} 토큰")
    print(f"    입력 {pf['total_prompt']:,} 토큰 → {U.fmt_won(wp)}")
    print(f"    출력 {pf['total_completion']:,} 토큰 → {U.fmt_won(wc)}")
    print(f"    합계 {pf['total']:,} 토큰 ≈ **{U.fmt_won(pf['won'])}**")
    print(f"  근거: {src}")
    print(f"  상한: {U.fmt_won(pf['max_cost'])}")
    if pf["won"] > pf["max_cost"]:
        print(f"  ※ 예상치가 상한을 넘는다. 상한에 걸려 도중에 멈출 것이다 "
              f"(--max-cost 로 올리거나 --n 을 줄여라)")
    print(f"\n  {U.price_line()}")
    if not pf["measured"]:
        print("  ※ 문항당 토큰은 아직 **가정**이다. 한 번 돌리면 실측으로 바뀐다 — "
              "먼저 --n 5 정도로 재보는 것을 권한다")


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
        err = ""
        try:
            res = answer(it["question"], model=model, deadline=deadline,
                         max_tool_calls=max_tool_calls, qid=it["qid"])
        except BaseException as e:             # noqa: BLE001 — 한 문항에 전체가 멈추면 안 된다
            # answer() 는 내부에서 다 잡지만, 그 바깥(임포트·메모리 등)에서 터질 수 있다.
            # 그 문항만 error 로 남기고 다음으로 간다. 죽으면 --resume 이 여기서 이어간다.
            traceback.print_exc()
            err = f"{type(e).__name__}: {e}"
            res = {"answer": "", "sources": [], "tool_calls": [], "usage": {},
                   "waste": {}, "note": f"문항 실패: {err}", "documents": []}
        row = {
            "qid": it["qid"], "corp": it["corp"], "qtype": it["qtype"],
            "question": it["question"], "question_raw": it.get("question_raw", ""),
            "inject_rule": it.get("inject_rule", ""),
            "ground_truth": it["ground_truth"], "src_file": it["src_file"],
            "answer": res.get("answer") or "",
            "sources": res.get("sources") or [],
            # arguments 와 note 를 반드시 남긴다. 실패를 검색/계획/작문 셋으로 가르려면
            # "무엇을 넣고 불렀는지" 가 있어야 한다. 첫 실행에서 이게 없어 도구가
            # 0건을 돌려준 이유를 못 봤다.
            "tool_calls": [{k: c.get(k)
                            for k in ("n", "name", "arguments", "status",
                                      "summary", "note")}
                           for c in (res.get("tool_calls") or [])],
            "n_tool_calls": len(res.get("tool_calls") or []),
            "usage": res.get("usage") or {},
            "waste": res.get("waste") or {},
            "stopped_by_budget": bool(res.get("stopped_by_budget")),
            "unsourced_claim": bool(res.get("unsourced_claim")),
            "note": res.get("note") or "",
            "elapsed": round(time.time() - t0, 2),
            # 문항별 비용. usage.est_won 과 같은 값이지만 표를 만들 때 바로 쓴다
            "cost": (res.get("usage") or {}).get("est_won"),
            "error": err,           # 문항이 통째로 실패했을 때만 채워진다
            "verdict": None,        # 사람이 채운다 — 자동 채점은 폐기했다
        }
        rows.append(row)
        with open(out_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

        u = row["usage"]
        print(f"[{i}/{len(items)}] {it['qid']}  "
              f"도구 {row['n_tool_calls']}회 · "
              f"입력 {u.get('prompt', 0):,}/출력 {u.get('completion', 0):,} · "
              f"{U.fmt_won(u.get('est_won'))} · "
              f"{row['elapsed']:.0f}초 · 누적 {U.fmt_won(budget.spent_won)}"
              + ("  ← 상한으로 중단됨" if row["stopped_by_budget"] else "")
              + (f"  ← 실패 {err}" if err else ""))

        # 10문항마다 진행 상황과 누적 비용을 한 줄로. 긴 실행에서 이것만 봐도 된다.
        if i % 10 == 0 or i == len(items):
            n_err = sum(1 for r in rows if r.get("error"))
            sec = sum(r["elapsed"] for r in rows)
            print(f"  ── 진행 {i}/{len(items)} · 누적 {U.fmt_won(budget.spent_won)} · "
                  f"소요 {sec / 60:.1f}분 · 실패 {n_err}건", flush=True)

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

    tp = sum(r["usage"].get("prompt", 0) for r in rows)
    tc = sum(r["usage"].get("completion", 0) for r in rows)
    total_tok = tp + tc
    unknown = sum(r["usage"].get("unknown_calls", 0) for r in rows)
    llm_calls = sum(r["usage"].get("llm_calls", 0) for r in rows)
    wp = tp * U.WON_PER_PROMPT_TOKEN
    wc = tc * U.WON_PER_COMPLETION_TOKEN

    L += ["## 토큰·비용", "",
          "| 구분 | 토큰 | 단가(1K) | 추정 비용 |", "|---|---|---|---|",
          f"| 입력 | {tp:,} | {U.PRICE_PROMPT_PER_1K}원 | {U.fmt_won(wp)} |",
          f"| 출력 | {tc:,} | {U.PRICE_COMPLETION_PER_1K}원 | {U.fmt_won(wc)} |",
          f"| **합계** | **{total_tok:,}** | | **{U.fmt_won(wp + wc)}** |", ""]
    if rows:
        L.append(f"- LLM 호출 {llm_calls}회 · 문항당 평균 입력 {tp / len(rows):,.0f} + "
                 f"출력 {tc / len(rows):,.0f} 토큰 "
                 f"({U.fmt_won(U.won(tp / len(rows), tc / len(rows)))}/문항)")
    if budget.limit_won:
        L.append(f"- 상한 {U.fmt_won(budget.limit_won)} · 남은 예산 "
                 f"{U.fmt_won(budget.remaining_won())}")
    if pf["total"]:
        L.append(f"- 실행 전 예상 {U.fmt_won(pf['won'])} → 실제 "
                 f"{U.fmt_won(wp + wc)} ({(wp + wc) / pf['won'] * 100:.0f}%)")
    L.append("")
    if unknown:
        L += [f"- ⚠ 입력·출력 내역을 알 수 없는 호출 {unknown}건. 0 으로 세지 않았으므로 "
              f"위 합계는 **하한**이다", ""]
    L += [U.price_line(), ""]

    # 토큰 상위 5
    def cost_of(r):
        return U.won(r["usage"].get("prompt", 0), r["usage"].get("completion", 0)) or 0.0

    L += ["## 비용이 큰 문항 상위 5", "",
          "| 문항 | 유형 | 입력 | 출력 | 추정 비용 | 도구 | LLM |",
          "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: -cost_of(x))[:5]:
        u = r["usage"]
        L.append(f"| {r['qid']} | {r['qtype']} | {u.get('prompt', 0):,} | "
                 f"{u.get('completion', 0):,} | {U.fmt_won(cost_of(r))} | "
                 f"{r['n_tool_calls']} | {u.get('llm_calls', 0)} |")
    L.append("")

    # 낭비 신호
    w = Counter()
    for r in rows:
        ww = r.get("waste") or {}
        w["repeat_blocked"] += ww.get("repeat_blocked", 0)
        w["retry_after_bad"] += ww.get("retry_after_bad", 0)
        w["list_only_nudge"] += 1 if ww.get("list_only_nudge") else 0
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
          f"| 목록만 보고 답하려다 본문 검색으로 복귀 | {w['list_only_nudge']} | "
          f"내용 질문에서 list_disclosures 만 보고 답하려 한 횟수 |",
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
          "| 유형 | 문항 | 평균 입력 | 평균 출력 | 평균 비용 | 평균 도구 | 평균 초 | 근거 0건 |",
          "|---|---|---|---|---|---|---|---|"]
    for k in sorted(by):
        g = by[k]
        ap = sum(x["usage"].get("prompt", 0) for x in g) / len(g)
        ac = sum(x["usage"].get("completion", 0) for x in g) / len(g)
        L.append(f"| {k} | {len(g)} | {ap:,.0f} | {ac:,.0f} | "
                 f"{U.fmt_won(U.won(ap, ac))} | "
                 f"{sum(x['n_tool_calls'] for x in g) / len(g):.1f} | "
                 f"{sum(x['elapsed'] for x in g) / len(g):.0f} | "
                 f"{sum(1 for x in g if not x['sources'])} |")
    L.append("")

    # ── 근거를 못 올린 문항만 기계로 가른다
    b = Counter(bucket(r) for r in rows)
    L += ["## 근거를 못 올린 문항", "",
          "| 통 | 문항 | 뜻 |", "|---|---|---|",
          f"| 검색 | {b[BUCKET_SEARCH]} | 도구는 불렀는데 근거가 하나도 안 올라왔다 |",
          f"| 계획 | {b[BUCKET_PLAN]} | 조회를 안 했거나 도구를 헤맸다(한도·전부 실패) |",
          f"| 보류(사람) | {b[BUCKET_HOLD]} | 근거는 올렸다. 맞는지는 사람이 본다 |", "",
          "**자동 채점은 폐기했다.** 정답에 '오답 유형' 반례 숫자가 섞여 있어 숫자 일치로는",
          f"맞고 틀림을 가릴 수 없다. 판정은 `{REPORTS.name}/{run_name}_review.md` 에서 한다.",
          ""]

    # ── 유형별 (잰 것만)
    L += ["## 유형별 (기계로 잰 것만)", "",
          "| 유형 | 문항 | 근거 확보 | 근거 확보율 | 평균 근거 | 평균 도구 | 평균 비용 |",
          "|---|---|---|---|---|---|---|"]
    for k in sorted(by):
        g = by[k]
        got = [x for x in g if x.get("sources")]
        L.append(f"| {k} | {len(g)} | {len(got)} | {len(got) / len(g) * 100:.0f}% | "
                 f"{sum(len(x.get('sources') or []) for x in g) / len(g):.1f} | "
                 f"{sum(x['n_tool_calls'] for x in g) / len(g):.1f} | "
                 f"{U.fmt_won(sum((x['usage'].get('est_won') or 0) for x in g) / len(g))} |")
    got_all = sum(1 for x in rows if x.get("sources"))
    L += ["", f"전체 근거 확보율 **{got_all}/{len(rows)} "
              f"({got_all / len(rows) * 100:.0f}%)**", ""]

    # ── 도구별 성적
    tool_stat: dict = Counter()
    for r in rows:
        for c in r.get("tool_calls") or []:
            tool_stat[(c.get("name"), c.get("status"))] += 1
    names = sorted({n for n, _ in tool_stat})
    L += ["## 도구별 성적", "", "| 도구 | 호출 | ok | not_found | error |",
          "|---|---|---|---|---|"]
    for n in names:
        tot = sum(v for (nn, _), v in tool_stat.items() if nn == n)
        L.append(f"| {n} | {tot} | {tool_stat[(n, 'ok')]} | "
                 f"{tool_stat[(n, 'not_found')]} | {tool_stat[(n, 'error')]} |")
    L.append("")

    # ── [2][3][4] 수정 효과
    no_tool = [r for r in rows if (r.get("waste") or {}).get("answered_without_tools")]
    nudged = [r for r in rows if (r.get("waste") or {}).get("no_tool_nudge")]
    list_nudged = [r for r in rows if (r.get("waste") or {}).get("list_only_nudge")]
    other = [(r["qid"], cited_other_corp(r)) for r in rows if cited_other_corp(r)]
    refus = [(r["qid"], refusal_count(r.get("answer") or "")) for r in rows
             if refusal_count(r.get("answer") or "")]
    unsourced = [r for r in rows if r.get("unsourced_claim")]
    L += ["## 수정 효과 집계", "",
          "| 항목 | 값 |", "|---|---|",
          f"| 도구 0회로 답한 문항 | **{len(no_tool)}**/{len(rows)} "
          f"{[r['qid'] for r in no_tool] if no_tool else ''} |",
          f"| 조회하라고 되돌린 문항(FORCE_TOOL) | {len(nudged)} |",
          f"| 목록에서 본문 검색으로 되돌린 문항 | {len(list_nudged)} |",
          f"| 타사 데이터를 근거로 단 문항 | **{len(other)}** "
          f"{[q for q, _ in other] if other else ''} |",
          f"| 항목별 거절을 밝힌 문항 | {len(refus)} (총 {sum(n for _, n in refus)}회) |",
          f"| 근거 0건인데 단정한 문항(unsourced_claim) | **{len(unsourced)}** "
          f"{[r['qid'] for r in unsourced] if unsourced else ''} |",
          ""]

    L += ["## 판정 (사람이 채운다)", "",
          f"`{REPORTS.name}/{run_name}_review.md` 에 질문·정답·답변이 나란히 있다.",
          "이 표는 그 파일을 다 채운 뒤 옮겨 적는 자리다.", "",
          "| 문항 | 유형 | 근거 | 도구 | 정답 | 출처 | 환각 |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['qid']} | {r['qtype']} | {len(r.get('sources') or [])} | "
                 f"{r['n_tool_calls']} | | | |")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e 평가 (토큰·비용 상한 포함)")
    ap.add_argument("--n", type=int, default=40,
                    help="이번에 돌릴 문항 수 (0=고정 표본 전량)")
    ap.add_argument("--sample-n", type=int, default=40,
                    help="고정 표본 크기. eval/e2e_sample.jsonl 이 없을 때만 쓰인다")
    ap.add_argument("--all-items", action="store_true",
                    help="고정 표본을 무시하고 240문항 전체를 후보로 쓴다")
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

    if a.all_items:
        pool = items
        print("[표본] 고정 표본을 쓰지 않는다 — 240문항 전체가 후보")
    else:
        pool, swarn = ensure_sample(items, a.sample_n, a.seed)
        for m in swarn:
            print(f"[표본] {m}")
        if not pool:
            print("[중단] 고정 표본이 비었다", file=sys.stderr)
            return 2

    rw = write_rewrite_report(pool)
    print(f"[주입] 회사명 주입 규칙: 앞에 붙임 "
          f"{sum(1 for x in pool if x['inject_rule'] == RULE_PREFIX)}건 / "
          f"그대로 {sum(1 for x in pool if x['inject_rule'] == RULE_KEEP)}건 "
          f"· 전후 전량 {rw}")

    picked = sample(pool, a.n, a.types, a.corps, a.seed)

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
        print(f"[resume] {out_path.name} 에 이미 {len(already)}건 있다. "
              f"{before} → {len(picked)}건만 돈다 (끝난 것은 결과를 그대로 쓴다)")

    print(f"[표본] {len(picked)}건 · {dict(Counter(x['qtype'] for x in picked))}")
    if not picked:
        print("[끝] 돌 문항이 없다")
        return 0

    pf = preflight(len(picked), a.max_cost, U.avg_split_per_item(script=SCRIPT))
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
    final = all_rows or rows
    md = report(final, budget, run_name, pf)
    REPORTS.mkdir(parents=True, exist_ok=True)
    rp = REPORTS / f"{run_name}.md"
    rp.write_text(md, encoding="utf-8", newline="\n")
    review = write_review(final, run_name)

    print(f"\n{BAR}")
    print(f"끝. {len(rows)}문항 실행 · {time.time() - t0:.0f}초")
    print(f"  {budget.line()}")
    print(f"  결과 {out_path}")
    print(f"  리포트 {rp}")
    print(f"  사람 채점용 {review}")
    print(f"  누적 사용량: python eval/token_log.py --summary")
    print(BAR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
