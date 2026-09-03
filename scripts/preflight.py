"""서버 기동 전 점검. **LLM 도 인덱스도 올리지 않는다** (1초 안에 끝난다).

  python scripts/preflight.py            # 점검만
  python scripts/preflight.py --warmup   # 인덱스까지 실제로 올려 본다(35초)

9/4 API 서버가 뜨지 않는 경우는 대부분 아래 넷 중 하나였다.
기동 스크립트가 서버를 띄우기 **전에** 이걸 통과시키면 원인 불명으로 헤매지 않는다.

  1. 임베딩 모델 캐시가 없다 → dense 를 GPU 에 올린 뒤 모델 초기화에서 죽는다
  2. 프록시 환경변수가 잡혀 있다 → HF 가 TLS 검증에 실패한다
  3. GPU 가 남의 프로세스로 차 있다
  4. .env 에 CLOVA_API_KEY 가 없다 → 첫 질문에서야 안다

되돌아오는 코드: 0 통과 / 1 막힘.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

BAR = "=" * 74
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY")
BLOCK: list = []


def ok(name, detail=""):
    print(f"  [통과] {name}" + (f" — {detail}" if detail else ""))


def bad(name, detail="", fix=""):
    BLOCK.append(name)
    print(f"  [막힘] {name}" + (f" — {detail}" if detail else ""))
    if fix:
        print(f"         고치는 법: {fix}")


def warn(name, detail=""):
    print(f"  [주의] {name}" + (f" — {detail}" if detail else ""))


def check_model_cache() -> None:
    from src.search import hf_cache

    st = hf_cache.status()
    print(f"  캐시 위치 {st['cache_root']}")
    if st["cached"]:
        ok("임베딩 모델 캐시", st["snapshot"])
    else:
        bad("임베딩 모델 캐시 없음", st["repo_dir"],
            "python -c \"from huggingface_hub import snapshot_download;"
            " snapshot_download('BAAI/bge-m3')\"  (인터넷 되는 곳에서 한 번)")


def check_proxy() -> None:
    live = {k: os.environ[k] for k in PROXY_VARS if os.environ.get(k)}
    if live:
        bad("프록시 환경변수가 잡혀 있다", str(live),
            "기동 스크립트에서 비운다 — set HTTP_PROXY= / set HTTPS_PROXY=")
    else:
        ok("프록시 환경변수 없음")

    # 로컬에 프록시가 떠 있어도 환경변수가 없으면 우리 프로세스는 안 탄다.
    # 다만 누가 나중에 잡을 수 있으므로 떠 있다는 사실은 알려 준다.
    try:
        # Windows netstat 은 cp949 로 뱉는다. utf-8 로 강제 디코드하면 리더 스레드가
        # UnicodeDecodeError 로 죽고 지저분한 트레이스백만 남는다.
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             errors="replace", timeout=10).stdout or ""
        rows = [l for l in out.splitlines() if ":3128" in l and "LISTENING" in l]
        if rows:
            warn("3128 포트에 프록시가 떠 있다",
                 "환경변수만 안 잡히면 무해하다. mitmdump 자동 시작은 "
                 "시작프로그램 폴더의 start_proxy.bat")
    except Exception:                          # noqa: BLE001 — 점검이 죽으면 안 된다
        pass


def check_index_files() -> None:
    from src.index import paths

    need = [paths.ID_MAP, paths.DENSE, ROOT / "corpus" / "manifest.jsonl",
            ROOT / "data" / "index" / "latest_mask.npz"]
    miss = [p for p in need if not Path(p).exists()]
    if miss:
        bad("인덱스 파일 없음", str([str(p) for p in miss]))
    else:
        gb = sum(Path(p).stat().st_size for p in need) / 1e9
        ok("인덱스 파일", f"{len(need)}개 · {gb:.1f}GB")


def check_gpu() -> None:
    # GPU 가 없어도 돈다(CPU 폴백). 없는 것은 '막힘'이 아니라 '주의'다.
    from src.eval import gpu_guard

    if not gpu_guard.cuda_ok():
        warn("GPU 없음 — CPU 모드로 동작",
             "검색 1회 100~210ms(GPU 70~80ms) · RAM 약 4.4GB 필요 "
             "(dense 2.5GB + 임베딩 모델). 60초 예산에는 넉넉하다")
        return
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=15).stdout
    except Exception as e:                     # noqa: BLE001
        warn("nvidia-smi 실패", f"{type(e).__name__}: {e} — CPU 로 뜨면 매우 느리다")
        return
    rows = [[int(x) for x in l.split(",")] for l in out.strip().splitlines()]
    free0 = rows[0][2] - rows[0][1]
    line = " / ".join(f"GPU{i} {u:,}/{t:,}MiB" for i, u, t in rows)
    if free0 < 4000:
        bad("GPU0 여유 부족", f"{free0:,}MiB — 인덱스+모델에 약 2.8GB 필요",
            "다른 프로세스를 정리하거나 Retriever(gpu=N) 으로 다른 장치를 쓴다")
    else:
        ok("GPU0 여유", f"{free0:,}MiB · {line}")


def check_api_key() -> None:
    from src.agent import clova

    key, err = clova.get_api_key()
    if err:
        bad("CLOVA_API_KEY", err, "리포 루트 .env 에 CLOVA_API_KEY=... 를 넣는다")
    else:
        ok("CLOVA_API_KEY", f"{clova.mask_key(key)} · 모델 {clova.resolve_model()}")


def main() -> int:
    ap = argparse.ArgumentParser(description="서버 기동 전 점검 (LLM 안 부름)")
    ap.add_argument("--warmup", action="store_true",
                    help="인덱스와 모델을 실제로 올려 본다 (약 35초)")
    a = ap.parse_args()

    print(f"{BAR}\n서버 기동 전 점검\n{BAR}")
    check_proxy()
    check_model_cache()
    check_index_files()
    check_gpu()
    check_api_key()

    if a.warmup and not BLOCK:
        print(f"\n{BAR}\n실제 기동 (인덱스 + 임베딩 모델)\n{BAR}")
        from src.agent.loop import warmup

        sec = warmup()
        ok("warmup", f"{sec:.1f}초")

    print(f"\n{BAR}")
    if BLOCK:
        print(f"막힌 항목 {len(BLOCK)}건: {BLOCK}")
        print("서버를 띄우기 전에 위 '고치는 법'을 먼저 처리할 것.")
        return 1
    print("전부 통과 — 서버를 띄워도 된다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
