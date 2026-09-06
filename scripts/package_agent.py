"""배포본 묶기 — 임베딩 인덱스 + Agent 코드를 한 zip 으로.

  python scripts/package_agent.py --code-only     # 코드만 (~15MB, 10초)
  python scripts/package_agent.py                 # 인덱스까지 전부 (~6.7GB → 2.5GB)
  python scripts/package_agent.py --dry-run       # 무엇이 들어가는지만 본다

기존 `dist.zip`(2026-08-29)에는 임베딩 단계 산출물과 그때의 검색 코드만 들어 있다.
그 뒤에 만든 것 — 하이브리드 검색기, 도구 계층, Agent 루프, 평가 하네스 — 이 빠져 있고,
BM25 인덱스와 정정 마스크처럼 **Agent 가 없으면 못 도는 파일**도 빠져 있다.
이 스크립트는 리포에서 곧바로 묶는다.

【레이아웃은 리포와 같다】
푼 폴더가 곧 리포 루트다. `ROOT = Path(__file__).parents[2]` 같은 경로 계산이
그대로 맞아떨어져서 클론 없이 그 자리에서 돈다.

【절대 넣지 않는 것】
`.env` — CLOVA_API_KEY 가 들어 있다. 실수로라도 들어가지 않게 이름으로 막고,
묶기 전에 목록을 한 번 더 훑어 걸리면 중단한다.
`corpus/raw/`(5.2GB 원본 공시), `data/interim`, `data/verify`, `logs/`,
실행 산출물(`eval/token_log.jsonl`, `eval/e2e_runs/`)도 뺀다 — 재생성되거나 로컬 기록이다.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# 절대 들어가면 안 되는 것. 이름이 걸리면 묶지 않고 멈춘다.
FORBIDDEN = (".env", "credentials", "secret", "id_rsa")

# 이미 압축된 형식은 다시 압축해도 안 줄고 CPU 만 먹는다.
STORE_EXT = {".npz", ".parquet", ".gz", ".zip", ".xlsx", ".png", ".jpg"}

# (경로, 큰 데이터인가) — 큰 것은 --code-only 에서 빠진다
SPEC = [
    ("requirements.txt", False),
    ("CLAUDE.md", False),
    ("config/aliases.yaml", False),
    ("config/tools.json", False),
    ("config/exception_policy.yaml", False),
    ("corpus/manifest.jsonl", False),
    ("corpus/universe.csv", False),
    ("corpus/README.md", False),
    ("corpus/data_filter.md", False),
    ("prompts/", False),
    ("src/", False),
    ("eval/", False),
    ("docs/", False),
    ("scripts/", False),
    # 파싱 단계는 src/ 만으로 돌지 않는다. src/normalize/document.py 가
    # parser/ 의 파서 4종을 sys.path 로 직접 집어 온다(거기 _PARSER_DIR).
    # 이게 빠지면 03_build_docjson.py 가 import 에서 죽는다 — 재현 불가.
    ("parser/", False),
    # XML 이 없는 3건(status 014)의 뷰어 HTML 경로.
    # scripts/12_rebuild_corrections.py 가 여기 세 부품을 쓴다.
    ("pdf_parser/", False),
    ("data/index/latest_mask.npz", False),
    ("data/index/doc_fiscal_map.json", False),
    # ── 여기부터 큰 것
    ("data/index/vectors/dense.f32.npy", True),
    ("data/index/vectors/sparse.npz", True),
    ("data/index/vectors/id_map.parquet", True),
    ("data/index/vectors/text_offsets.npz", True),
    ("data/index/vectors/meta.json", True),
    ("data/index/bm25/", True),
    ("data/processed/chunks_by_10_companies/", True),
]

# 폴더를 넣을 때 걸러낼 것
SKIP_PARTS = {"__pycache__", ".git", "venv", "e2e_runs", "_work", "_archive", "plan_old"}
SKIP_NAMES = {"token_log.jsonl", ".DS_Store"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def wanted(p: Path) -> bool:
    if not p.is_file():
        return False
    if set(p.parts) & SKIP_PARTS:
        return False
    if p.name in SKIP_NAMES or p.suffix in SKIP_SUFFIX:
        return False
    # reports/ 는 실행마다 쏟아진다. 사람이 읽는 md 만 남긴다
    if "reports" in p.parts and p.suffix != ".md":
        return False
    return True


def collect(code_only: bool, with_questions: bool) -> list:
    out: list = []
    for rel, big in SPEC:
        if big and code_only:
            continue
        src = ROOT / rel
        if not src.exists():
            print(f"  [없음] {rel} — 건너뛴다", file=sys.stderr)
            continue
        if src.is_dir():
            out += [p for p in sorted(src.rglob("*")) if wanted(p)]
        elif wanted(src):
            out.append(src)
    if with_questions:
        q = ROOT / "Question"
        if q.is_dir():
            out += [p for p in sorted(q.rglob("*")) if wanted(p)]
    # 중복 제거(순서 유지)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def guard(files: list) -> None:
    """비밀이 섞였으면 묶지 않는다."""
    bad = [p for p in files
           if any(w in p.name.lower() for w in FORBIDDEN)]
    if bad:
        raise SystemExit("[중단] 넣으면 안 되는 파일이 목록에 있다: "
                         + str([str(p.relative_to(ROOT)) for p in bad]))


def readme(files: list, code_only: bool) -> str:
    n_big = sum(1 for p in files if "data" in p.parts)
    total = sum(p.stat().st_size for p in files)
    L = [
        "# 공시 Agent — 배포본",
        "",
        f"묶은 날짜 {time.strftime('%Y-%m-%d %H:%M')} · 파일 {len(files):,}개 · "
        f"원본 {total / 1e9:.2f}GB",
        "",
        "임베딩 인덱스와 **Agent 코드 전체**가 들어 있다.",
        "이 폴더가 곧 리포 루트다 — 클론하지 않아도 여기서 그대로 돈다.",
        "",
        "## 0. 먼저 할 것",
        "",
        "```bash",
        "pip install -r requirements.txt",
        "# torch 는 CUDA 버전에 맞춰 따로. 이 인덱스를 만든 환경은 2.7.1+cu118:",
        "#   pip install torch --index-url https://download.pytorch.org/whl/cu118",
        "",
        "# .env 를 만든다 (배포본에 키는 들어 있지 않다)",
        "echo CLOVA_API_KEY=발급받은키 > .env",
        "",
        "python scripts/preflight.py        # 기동 전 점검 (1초, 인덱스 안 올림)",
        "```",
        "",
        "`preflight` 가 프록시 환경변수 · 임베딩 모델 캐시 · 인덱스 파일 · GPU 여유 ·",
        "API 키 다섯 가지를 보고, 막힌 것이 있으면 **고치는 법과 함께** 실패한다.",
        "",
        "## 1. Agent 에게 묻기",
        "",
        "```bash",
        'python eval/ask.py "SK하이닉스 2024년 연결 매출액이 얼마야?"',
        'python eval/ask.py "..." --verbose      # 프롬프트 전문까지',
        "```",
        "",
        "인덱스와 임베딩 모델을 올리는 데 35초쯤 걸린다(질문 시간에는 안 들어간다).",
        "",
        "## 2. 평가",
        "",
        "```bash",
        "python eval/run_e2e.py --n 40                        # 예상 비용만 (LLM 안 부름)",
        "python eval/run_e2e.py --n 40 --yes --max-cost 3000  # 실행",
        "python eval/token_log.py --summary                   # 누적 토큰·비용",
        "```",
        "",
        "`--yes` 없이는 예상 비용만 찍고 끝난다. `--max-cost` 를 넘으면 문항 중간에도 멈춘다.",
        "",
        "## 3. 검증 (LLM 을 한 번도 안 부른다)",
        "",
        "```bash",
        "python eval/test_recency.py   # 목록 최신순 · 접수번호/연도 방어",
        "python eval/test_usage.py     # 토큰·비용 안전장치",
        "python eval/test_loop.py      # 루프가 어떤 경우에도 규격 dict 를 내는지",
        "python eval/test_tools.py     # 도구가 예외를 안 던지는지",
        "python eval/test_context.py   # <document> 조립 · 정정 경고",
        "```",
        "",
        "## 4. 무엇이 들어 있나",
        "",
        "| 경로 | 내용 |",
        "|---|---|",
        "| `src/agent/` | LLM 루프 · HyperCLOVA X 클라이언트 · 토큰 회계 · 컨텍스트 조립 |",
        "| `src/tools/` | search_disclosure · list_disclosures · calculate · resolve_company |",
        "| `src/search/` | 하이브리드 검색기(BM25+dense RRF) · 별칭 · 정정 마스크 |",
        "| `src/index/` | 임베딩 단계 코드 |",
        "| `src/normalize/` · `src/render/` · `parser/` | 원문 XML → 마크다운. "
        "파이프라인이 실제로 import 하는 파서 4종은 `parser/` 쪽이다 |",
        "| `pdf_parser/` | XML 이 없는 3건(status 014)의 공시뷰어 HTML 경로. "
        "`scripts/12_rebuild_corrections.py` 가 쓴다 |",
        "| `prompts/` | planner.txt · system.txt |",
        "| `config/` | tools.json(함수 정의) · aliases.yaml(회사 별칭) |",
        "| `eval/` | Agent 실행기 · e2e 평가 · 검증 스크립트 |",
        "| `docs/` | WORKLOG.md(작업 기록) · step6_review.md(주제별 정리) |",
        "",
    ]
    if code_only:
        L += ["## ⚠ 이 배포본에는 인덱스가 없다", "",
              "`--code-only` 로 묶었다. 아래가 따로 필요하다:", "",
              "```",
              "data/index/vectors/     dense.f32.npy · sparse.npz · id_map.parquet 등",
              "data/index/bm25/        bm25_csc.npz · vocab.txt · doc_len.npy",
              "data/processed/chunks_by_10_companies/   원본 청크 JSONL 8개",
              "```", ""]
    else:
        L += [f"인덱스 파일 {n_big}개가 포함돼 있다. `SHA256SUMS` 로 무결성을 확인할 수 있다.",
              "압축·업로드를 거치면 파일이 조용히 깨질 수 있고, **깨져도 에러가 안 나고**",
              "엉뚱한 문단이 근거로 나온다.", ""]
    L += ["## 넣지 않은 것", "",
          "- `.env` — API 키. 받는 사람이 직접 만든다",
          "- `corpus/raw/` — 원본 공시 5.2GB. 인덱스가 있으면 필요 없다",
          "- `logs/` · `eval/e2e_runs/` · `eval/token_log.jsonl` — 로컬 실행 기록",
          ""]
    return "\n".join(L)


def sha256(p: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while True:
            b = fh.read(buf)
            if not b:
                return h.hexdigest()
            h.update(b)


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent 배포본 묶기")
    ap.add_argument("--out", default=None, help="출력 zip 경로")
    ap.add_argument("--code-only", action="store_true",
                    help="인덱스·청크를 빼고 코드만 (~15MB)")
    ap.add_argument("--with-questions", action="store_true",
                    help="Question/ 벤치 문항(정답 포함)까지 넣는다")
    ap.add_argument("--dry-run", action="store_true", help="목록만 보고 끝낸다")
    ap.add_argument("--no-sha", action="store_true", help="SHA256SUMS 를 만들지 않는다")
    a = ap.parse_args()

    files = collect(a.code_only, a.with_questions)
    guard(files)

    total = sum(p.stat().st_size for p in files)
    print(f"파일 {len(files):,}개 · 원본 {total / 1e9:.2f}GB")
    big = sorted(files, key=lambda p: -p.stat().st_size)[:8]
    for p in big:
        print(f"   {p.stat().st_size / 1e6:10,.0f}MB  {p.relative_to(ROOT)}")
    if a.dry_run:
        print("\n--dry-run 이라 묶지 않았다.")
        return 0

    stamp = time.strftime("%Y%m%d")
    default = f"dist_agent_{'code_' if a.code_only else ''}{stamp}.zip"
    out = Path(a.out) if a.out else (ROOT / default)
    if out.exists():
        raise SystemExit(f"[중단] {out} 이 이미 있다. 지우거나 --out 으로 다른 이름을 줘라")

    root_in_zip = out.stem
    t0 = time.time()
    print(f"\n묶는 중 → {out}  (zip 안 최상위 폴더: {root_in_zip}/)")

    sums = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6,
                         allowZip64=True) as z:
        z.writestr(f"{root_in_zip}/README.md", readme(files, a.code_only))
        for i, p in enumerate(files, 1):
            rel = p.relative_to(ROOT).as_posix()
            # 이미 압축된 형식은 저장만 한다 — 다시 눌러도 안 줄고 시간만 먹는다
            ctype = (zipfile.ZIP_STORED if p.suffix.lower() in STORE_EXT
                     else zipfile.ZIP_DEFLATED)
            zi = zipfile.ZipInfo.from_file(p, f"{root_in_zip}/{rel}")
            zi.compress_type = ctype
            with open(p, "rb") as fh, z.open(zi, "w") as dst:
                while True:
                    b = fh.read(1 << 22)
                    if not b:
                        break
                    dst.write(b)
            if not a.no_sha:
                sums.append(f"{sha256(p)}  {rel}")
            if i % 20 == 0 or p.stat().st_size > 1e8:
                done = sum(x.stat().st_size for x in files[:i])
                print(f"   [{i}/{len(files)}] {done / 1e9:.2f}/{total / 1e9:.2f}GB "
                      f"· {time.time() - t0:.0f}초 · {rel}", flush=True)
        if sums:
            z.writestr(f"{root_in_zip}/SHA256SUMS", "\n".join(sums) + "\n")

    size = out.stat().st_size
    print(f"\n끝. {out}")
    print(f"  {total / 1e9:.2f}GB → {size / 1e9:.2f}GB "
          f"({size / total * 100:.0f}%) · {time.time() - t0:.0f}초")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
