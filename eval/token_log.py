"""토큰·비용 누적 로그를 읽는다. LLM 은 부르지 않는다.

  python eval/token_log.py --summary          # 오늘/누적 + 스크립트별 분해
  python eval/token_log.py --summary --days 7 # 최근 7일만
  python eval/token_log.py --top 10           # 토큰을 많이 쓴 문항 상위 10

로그는 `src/agent/usage.py` 가 `eval/token_log.jsonl` 에 한 줄씩 append 한다.
`ask.py` · `quick_qa.py` · `run_e2e.py` 가 모두 같은 파일에 쓴다.

【입력과 출력을 나눠서 센다】
HCX-005 는 입력 1,000토큰 1.25원, 출력 1,000토큰 5원이다. **출력이 4배 비싸다.**
총 토큰만으로는 금액을 낼 수 없으므로 내역을 모르는 호출은 '토큰미상' 으로 따로 센다.
그런 호출이 있으면 합계는 하한이다.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from src.agent import usage as U  # noqa: E402

BAR = "=" * 78


class Acc:
    """한 덩어리의 합계. 토큰을 모르는 호출은 0 으로 세지 않고 따로 센다."""

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0
        self.calls = 0
        self.unknown = 0
        self.failed = 0
        self.retry = 0
        self.retry_429 = 0
        self.wait_s = 0.0

    def add(self, r: dict) -> None:
        self.calls += 1
        p, c = r.get("prompt_tokens"), r.get("completion_tokens")
        if p is None or c is None:
            # 입력·출력 단가가 4배 다르다. 내역을 모르면 금액을 낼 수 없다
            self.unknown += 1
        else:
            self.prompt += int(p)
            self.completion += int(c)
        if not r.get("ok", True):
            self.failed += 1
        self.retry += int(r.get("retries") or 0)
        self.retry_429 += int(r.get("retry_429") or 0)
        self.wait_s += float(r.get("retry_wait_s") or 0.0)

    @property
    def tokens(self) -> int:
        return self.prompt + self.completion

    @property
    def won(self) -> float:
        return U.won(self.prompt, self.completion) or 0.0

    def cells(self) -> list:
        return [f"{self.calls:,}", f"{self.prompt:,}", f"{self.completion:,}",
                U.fmt_won(self.won),
                f"{self.unknown}" if self.unknown else "-",
                f"{self.failed}" if self.failed else "-",
                f"{self.retry_429}" if self.retry_429 else "-"]


HEAD = ["호출", "입력", "출력", "추정 비용", "토큰미상", "실패", "429"]


def table(rows: list) -> str:
    """왼쪽 라벨 + 숫자 칸들. 폭을 맞춰 사람이 읽게."""
    if not rows:
        return "  (없음)"
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = [str(c).ljust(widths[i]) if i == 0 else str(c).rjust(widths[i])
                 for i, c in enumerate(r)]
        out.append("  " + "  ".join(cells))
    return "\n".join(out)


def summarize(rows: list, days=None, path=None) -> int:
    src = Path(path) if path else U.LOG_PATH
    if not rows:
        print(f"{src} 가 비어 있다. 아직 기록된 LLM 호출이 없다.")
        return 0

    today = time.strftime("%Y-%m-%d")
    cutoff = None
    if days:
        cutoff = time.strftime("%Y-%m-%d",
                               time.localtime(time.time() - int(days) * 86400))

    total, day_acc = Acc(), Acc()
    by_script: dict = defaultdict(Acc)
    by_model: dict = defaultdict(Acc)
    by_date: dict = defaultdict(Acc)
    for r in rows:
        d = str(r.get("ts") or "")[:10]
        if cutoff and d < cutoff:
            continue
        total.add(r)
        by_script[str(r.get("script") or "?")].add(r)
        by_model[str(r.get("model") or "?")].add(r)
        by_date[d].add(r)
        if d == today:
            day_acc.add(r)

    span = f"최근 {days}일" if days else "누적 전체"
    print(f"{BAR}\n토큰·비용 요약 ({span}) · {src.name}\n{BAR}")
    print(U.price_line() + "\n")

    print(table([["구분"] + HEAD,
                 [f"오늘({today})"] + day_acc.cells(),
                 [span] + total.cells()]))

    print(f"\n--- 스크립트별 ---")
    rows_s = [["스크립트"] + HEAD]
    for k, a in sorted(by_script.items(), key=lambda kv: -kv[1].tokens):
        rows_s.append([k] + a.cells())
    print(table(rows_s))

    print(f"\n--- 모델별 ---")
    rows_m = [["모델"] + HEAD]
    for k, a in sorted(by_model.items(), key=lambda kv: -kv[1].tokens):
        rows_m.append([k] + a.cells())
    print(table(rows_m))

    print(f"\n--- 날짜별 ---")
    rows_d = [["날짜"] + HEAD]
    for k in sorted(by_date, reverse=True)[:14]:
        rows_d.append([k] + by_date[k].cells())
    print(table(rows_d))

    if total.retry_429:
        print(f"\n429 재시도 {total.retry_429}회 · 대기 {total.wait_s:,.0f}초 "
              f"({total.wait_s / 60:.1f}분). 429 는 요청이 거절된 것이라 "
              f"usage 가 오지 않는다 — 낭비된 토큰은 측정할 수 없다")
    if total.unknown:
        print(f"\n토큰을 알 수 없는 호출 {total.unknown}건. 응답에 usage 가 없었다. "
              f"0 으로 세지 않았으므로 위 합계는 **하한**이다")

    avg = U.avg_split_per_item(path=path)
    if avg:
        print(f"\n문항 1건당 실측 평균 입력 {avg['prompt']:,.0f} + "
              f"출력 {avg['completion']:,.0f} 토큰 = "
              f"{U.fmt_won(U.won(avg['prompt'], avg['completion']))} 추정 "
              f"(문항 {avg['n']}건 기준)")
    return 0


def top_items(rows: list, n: int) -> int:
    """qid 별 토큰 합계 상위 n."""
    per: dict = defaultdict(lambda: [0, 0, 0])    # qid -> [입력, 출력, 호출수]
    for r in rows:
        qid = r.get("qid")
        if not qid:
            continue
        per[qid][2] += 1
        p, c = r.get("prompt_tokens"), r.get("completion_tokens")
        if p is not None and c is not None:
            per[qid][0] += int(p)
            per[qid][1] += int(c)
    if not per:
        print("qid 가 붙은 호출이 없다. 문항 단위 집계를 할 수 없다.")
        return 0
    print(f"{BAR}\n문항별 토큰 상위 {n}\n{BAR}")
    rows_o = [["문항", "입력", "출력", "추정 비용", "LLM 호출"]]
    for qid, (p, c, calls) in sorted(per.items(),
                                     key=lambda kv: -(U.won(kv[1][0], kv[1][1]) or 0))[:n]:
        rows_o.append([qid, f"{p:,}", f"{c:,}", U.fmt_won(U.won(p, c)), str(calls)])
    print(table(rows_o))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="토큰·비용 로그 요약 (LLM 안 부름)")
    ap.add_argument("--summary", action="store_true", help="오늘/누적 + 분해 (기본)")
    ap.add_argument("--days", type=int, default=None, help="최근 N일만")
    ap.add_argument("--top", type=int, default=0, help="문항별 토큰 상위 N")
    ap.add_argument("--path", default=None, help="다른 로그 파일")
    a = ap.parse_args()

    rows = U.read_log(a.path)
    if a.top:
        return top_items(rows, a.top)
    return summarize(rows, a.days, a.path)


if __name__ == "__main__":
    raise SystemExit(main())
