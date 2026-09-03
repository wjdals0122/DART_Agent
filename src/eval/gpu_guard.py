"""GPU 온도·전력 가드.

이 머신은 4x RTX 4090(각 450W)이라 전부 돌리면 1.8kW를 넘겨 전원이 내려간다.
그래서 실험은 (1) GPU 한 장만 쓰고 (2) 전력 상한을 걸고 (3) 온도가 넘으면 쉰다.

  python -m src.eval.gpu_guard                  # 현재 상태 한 번 출력
  python -m src.eval.gpu_guard --watch          # 5초마다 갱신
  python -m src.eval.gpu_guard --cap 300        # 전 GPU 전력 상한 300W (관리자 권한 필요)
  python -m src.eval.gpu_guard --cap default    # 상한 원복

주의: select_gpu()는 torch를 import 하기 전에 불러야 한다.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

# 워커 로그가 cp949 UnicodeEncodeError로 죽은 전례가 있다. 전 진입점에서 막는다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 데스크톱을 물고 있어 우리가 함부로 채우면 안 되는 GPU (memory/gpu3-is-display-gpu 참고)
DISPLAY_GPU = 3

# GPU 가 없는 곳(네이버클라우드 CPU 서버)에서도 그대로 돌아야 한다.
# 예전엔 GPU 0장을 감지해 경고까지 찍고도 그 뒤에서 CUDA 를 불러
# `AssertionError: Torch not compiled with CUDA enabled` 로 죽었다.
# 이제 CUDA 호출은 전부 cuda_ok() 뒤에서만 한다.
FORCE_CPU_ENV = "FORCE_CPU"
_SAID: set = set()


def _say_once(msg: str) -> None:
    """같은 말을 매번 찍지 않는다. 로딩 로그가 이걸로 도배되면 진짜 문제가 묻힌다."""
    if msg not in _SAID:
        _SAID.add(msg)
        print(f"[gpu_guard] {msg}", file=sys.stderr, flush=True)


def force_cpu() -> bool:
    """FORCE_CPU=1 이면 GPU 가 있어도 CPU 로 돈다. 서버 환경을 개발 PC 에서 재현할 때 쓴다."""
    v = (os.environ.get(FORCE_CPU_ENV) or "").strip().lower()
    return v not in ("", "0", "false", "no", "off")


def cuda_ok() -> bool:
    """torch 가 실제로 CUDA 를 쓸 수 있는가. 여기가 유일한 판단 지점이다."""
    if force_cpu():
        _say_once(f"{FORCE_CPU_ENV} 가 켜져 있다 — CPU 모드로 동작")
        return False
    try:
        import torch
    except ImportError:
        _say_once("torch 가 없다 — CPU 모드로 동작")
        return False
    try:
        ok = bool(torch.cuda.is_available()) and torch.cuda.device_count() > 0
    except Exception as e:                     # noqa: BLE001 — 물어보다 죽으면 안 된다
        _say_once(f"CUDA 확인 실패({type(e).__name__}: {e}) — CPU 모드로 동작")
        return False
    if not ok:
        _say_once("GPU 없음 — CPU 모드로 동작")
    return ok


def device_str(want: str = "cuda") -> str:
    """쓰려는 장치를 실제로 쓸 수 있는 장치로 바꿔 준다. 'cuda' 또는 'cpu'."""
    return "cuda" if (str(want).lower().startswith("cuda") and cuda_ok()) else "cpu"

QUERY = (
    "index,temperature.gpu,power.draw,power.limit,"
    "memory.used,memory.total,utilization.gpu"
)


def read_gpus() -> list[dict]:
    """nvidia-smi 한 번 찍어서 GPU별 상태를 dict 리스트로 돌려준다."""
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, errors="replace", check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        # GPU 가 없는 서버에는 nvidia-smi 자체가 없다. 빈 목록이 정답이다.
        _say_once(f"nvidia-smi 를 쓸 수 없다({type(e).__name__}) — GPU 상태를 못 읽는다")
        return []
    gpus = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        gpus.append({
            "index": int(f[0]),
            "temp": float(f[1]),
            "power": float(f[2]),
            "power_limit": float(f[3]),
            "mem_used": float(f[4]),
            "mem_total": float(f[5]),
            "util": float(f[6]),
        })
    return gpus


def format_table(gpus: list[dict]) -> str:
    rows = ["idx  온도   전력          메모리            사용률",
            "---  -----  ------------  ----------------  ------"]
    for g in gpus:
        tag = "  <- 디스플레이" if g["index"] == DISPLAY_GPU else ""
        rows.append(
            f"{g['index']:>3}  {g['temp']:>3.0f}°C  "
            f"{g['power']:>5.0f}/{g['power_limit']:>3.0f}W  "
            f"{g['mem_used']:>6.0f}/{g['mem_total']:>5.0f}MiB  "
            f"{g['util']:>4.0f}%{tag}"
        )
    return "\n".join(rows)


def set_power_cap(watts: int | str) -> None:
    """전 GPU 전력 상한을 건다. watts='default'면 원복. 관리자 권한이 필요하다."""
    arg = "0" if watts == "default" else str(int(watts))
    for g in read_gpus():
        r = subprocess.run(
            ["nvidia-smi", "-i", str(g["index"]), "-pl", arg],
            capture_output=True, text=True,
        )
        ok = r.returncode == 0
        msg = (r.stdout or r.stderr).strip().splitlines()
        print(f"GPU {g['index']}: {'OK' if ok else '실패'} — {msg[-1] if msg else ''}")
        if not ok:
            print("  (관리자 권한 PowerShell에서 실행해야 한다)")


def cooldown(gpu: int, max_temp: float, resume_temp: float, timeout: float = 900.0) -> float:
    """해당 GPU가 max_temp를 넘었으면 resume_temp 아래로 내려올 때까지 막는다.

    반환값은 기다린 초. 넘지 않았으면 즉시 0.0.
    """
    if not cuda_ok():
        return 0.0
    t0 = time.time()
    waited = False
    while True:
        temp = next((g["temp"] for g in read_gpus() if g["index"] == gpu), None)
        if temp is None:                       # 그 번호의 GPU 를 못 읽는다
            return 0.0
        if not waited and temp < max_temp:
            return 0.0
        if waited and temp <= resume_temp:
            break
        if time.time() - t0 > timeout:
            print(f"[guard] {timeout:.0f}초를 기다려도 {temp:.0f}°C — 그대로 진행한다", flush=True)
            break
        if not waited:
            print(f"[guard] GPU {gpu} {temp:.0f}°C > {max_temp:.0f}°C — "
                  f"{resume_temp:.0f}°C까지 대기", flush=True)
            waited = True
        time.sleep(5)
    elapsed = time.time() - t0
    if waited:
        print(f"[guard] 재개 ({elapsed:.0f}초 대기)", flush=True)
    return elapsed


class TempMonitor:
    """백그라운드로 온도를 샘플링해 최고치를 기록한다. with 문으로 쓴다."""

    def __init__(self, gpu: int, interval: float = 2.0):
        self.gpu = gpu
        self.interval = interval
        self.peak_temp = 0.0
        self.peak_power = 0.0
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                g = next(x for x in read_gpus() if x["index"] == self.gpu)
            except Exception:
                continue
            self.peak_temp = max(self.peak_temp, g["temp"])
            self.peak_power = max(self.peak_power, g["power"])
            self.samples += 1

    def __enter__(self) -> "TempMonitor":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1)

    def summary(self) -> str:
        return f"최고 {self.peak_temp:.0f}°C / {self.peak_power:.0f}W ({self.samples}회 샘플)"


def select_gpu(physical: int) -> None:
    """torch import 전에 호출할 것. 이후 torch가 보는 장치는 cuda:0 하나뿐이다."""
    os.environ["CUDA_VISIBLE_DEVICES"] = str(physical)
    # 길이가 제각각인 배치에서 캐싱 할당자가 조각나 예약량이 부풀어 오르는 걸 막는다
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def torch_index(physical: int) -> int:
    """물리 GPU 번호 → torch 가 보는 인덱스.

    select_gpu() 를 먼저 불렀으면 CUDA_VISIBLE_DEVICES 로 한 장만 보이므로 torch
    인덱스는 언제나 0 이다. 안 불렀으면 전부 보이므로 물리 번호가 곧 인덱스다.
    예전엔 0 을 그냥 박아 뒀는데, select_gpu 를 안 거치는 경로(src/search/hybrid.py)
    에서는 gpu=1 을 줘도 0 번에 상한이 걸렸다.
    """
    vis = (os.environ.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if not vis:
        return int(physical)
    ids = [x.strip() for x in vis.split(",") if x.strip()]
    try:
        return ids.index(str(physical))
    except ValueError:
        print(f"[gpu_guard] GPU {physical} 은 CUDA_VISIBLE_DEVICES={vis} 에 없다 — "
              f"보이는 첫 장치에 건다", file=sys.stderr)
        return 0


def apply_memory_fraction(physical: int, fraction: float | None = None) -> float:
    """select_gpu + torch import 이후에 호출. 디스플레이 GPU면 기본으로 상한을 건다.

    CUDA 가 없으면 **아무것도 하지 않고 0.0 을 돌려준다.** 예전엔 GPU 0장을 감지해
    경고를 찍고도 그 아래에서 set_per_process_memory_fraction 을 불러 죽었다.
    """
    if not cuda_ok():
        return 0.0
    import torch

    if fraction is None:
        fraction = 0.8 if physical == DISPLAY_GPU else 0.9
    idx = torch_index(physical)
    n = torch.cuda.device_count()
    if idx >= n:
        print(f"[gpu_guard] torch 가 보는 GPU 는 {n}장인데 인덱스 {idx} 를 요구했다 — "
              f"0 번에 건다", file=sys.stderr)
        idx = 0
    torch.cuda.set_per_process_memory_fraction(fraction, idx)
    return fraction


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="5초마다 갱신")
    ap.add_argument("--cap", help="전 GPU 전력 상한 W, 또는 'default'로 원복")
    args = ap.parse_args()

    if args.cap:
        set_power_cap(args.cap if args.cap == "default" else int(args.cap))
        print()

    if args.watch:
        try:
            while True:
                print(f"\n{time.strftime('%H:%M:%S')}\n{format_table(read_gpus())}", flush=True)
                time.sleep(5)
        except KeyboardInterrupt:
            return 0
    else:
        print(format_table(read_gpus()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
