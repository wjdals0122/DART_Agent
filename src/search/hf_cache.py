"""임베딩 모델을 **네트워크 없이** 올리게 만든다.

  from src.search import hf_cache
  hf_cache.ensure_offline()          # 모델 로드 직전에 한 번

【왜 필요한가 — 실측】
`paths.MODEL_NAME` 은 `"BAAI/bge-m3"`, 즉 로컬 경로가 아니라 Hugging Face 저장소 id 다.
그래서 기동할 때마다 `Fetching 30 files` 로 huggingface.co 를 친다. 지금 도는 이유는
`~/.cache/huggingface/hub` 에 4.59GB 가 이미 받아져 있어서일 뿐이다.

인터넷이 없거나, 사내 프록시가 TLS 를 가로채거나, 캐시가 지워지면
**dense 를 GPU 에 올린 직후 모델 초기화에서 죽는다.** 9/4 서버에서 이게 나면 치명적이다.

【무엇을 하는가】
1. `HF_HUB_OFFLINE=1` 을 건다(이미 값이 있으면 존중한다). 네트워크를 아예 안 친다
2. 캐시에 그 모델이 실제로 있는지 **먼저** 확인한다
3. 없으면 조용히 멈추지 않고 **경로와 고치는 법을 적어** SystemExit 로 죽는다

【환경변수를 늦게 걸면 안 먹는다】
`huggingface_hub.constants.HF_HUB_OFFLINE` 은 그 모듈을 import 할 때 한 번 읽힌다.
이미 import 된 뒤에 `os.environ` 만 바꾸면 무시된다. 그래서 이미 올라와 있으면
모듈 속성까지 같이 바꾼다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from src.index import paths

# 가중치로 인정할 파일. 하나라도 있으면 받아진 것으로 본다.
WEIGHT_FILES = ("pytorch_model.bin", "model.safetensors")
ENV = "HF_HUB_OFFLINE"


def cache_root() -> Path:
    """HF 허브 캐시 위치. huggingface_hub 가 있으면 그쪽 값을 따른다."""
    try:
        from huggingface_hub import constants

        return Path(constants.HF_HUB_CACHE)
    except Exception:                          # noqa: BLE001 — 없으면 규약대로 계산한다
        home = os.environ.get("HF_HOME") or os.path.join(Path.home(), ".cache",
                                                         "huggingface")
        return Path(os.environ.get("HUGGINGFACE_HUB_CACHE") or os.path.join(home, "hub"))


def repo_dir(model_name: str = None) -> Path:
    """`BAAI/bge-m3` → `<캐시>/models--BAAI--bge-m3`."""
    name = model_name or paths.MODEL_NAME
    return cache_root() / ("models--" + str(name).replace("/", "--"))


def find_snapshot(model_name: str = None):
    """가중치가 실제로 들어 있는 스냅샷 경로. 없으면 None."""
    snaps = repo_dir(model_name) / "snapshots"
    if not snaps.is_dir():
        return None
    for d in sorted(snaps.iterdir()):
        if d.is_dir() and any((d / f).exists() for f in WEIGHT_FILES):
            return d
    return None


def _missing_message(model_name: str) -> str:
    return "\n".join([
        f"[hf_cache] {model_name} 를 로컬 캐시에서 찾지 못했다. 모델을 올릴 수 없다.",
        f"  찾은 곳: {repo_dir(model_name)}",
        f"  가중치 파일 중 하나가 있어야 한다: {', '.join(WEIGHT_FILES)}",
        "",
        "  고치는 법",
        "   (a) 인터넷이 되는 곳에서 한 번 받아 둔다:",
        f"       set {ENV}=0",
        f"       python -c \"from huggingface_hub import snapshot_download;"
        f" snapshot_download('{model_name}')\"",
        "   (b) 캐시가 다른 경로에 있으면 그쪽을 가리킨다:",
        "       set HF_HOME=D:\\hf   (또는 HUGGINGFACE_HUB_CACHE 로 hub 폴더 직접 지정)",
        "",
        "  이 프로젝트는 bge-m3 벡터에 묶여 있어 다른 모델로 대체할 수 없다.",
    ])


def ensure_offline(model_name: str = None, strict: bool = True) -> Path:
    """오프라인 모드를 걸고 캐시를 확인한다. 스냅샷 경로를 돌려준다.

    `strict=False` 면 캐시가 없어도 죽이지 않고 경고만 남긴다(온라인으로 받게 둔다).
    """
    name = model_name or paths.MODEL_NAME
    snap = find_snapshot(name)

    if snap is None:
        if strict:
            raise SystemExit(_missing_message(name))
        print(f"[hf_cache] 캐시에 {name} 이 없다 — 온라인으로 내려받는다. "
              f"네트워크가 막히면 여기서 멈춘다", file=sys.stderr)
        os.environ[ENV] = "0"
        return None

    # 캐시가 확인됐을 때만 오프라인을 건다. 없는데 걸면 받을 길이 막힌다.
    os.environ.setdefault(ENV, "1")
    if os.environ.get(ENV) == "1":
        _patch_loaded_constant(True)
    return snap


def _patch_loaded_constant(offline: bool) -> None:
    """이미 import 된 huggingface_hub 는 환경변수를 다시 읽지 않는다."""
    mod = sys.modules.get("huggingface_hub.constants")
    if mod is not None and getattr(mod, "HF_HUB_OFFLINE", None) != offline:
        mod.HF_HUB_OFFLINE = offline


def status(model_name: str = None) -> dict:
    """사람이 읽을 현재 상태. 기동 전 점검에 쓴다."""
    name = model_name or paths.MODEL_NAME
    snap = find_snapshot(name)
    return {
        "model": name,
        "cache_root": str(cache_root()),
        "repo_dir": str(repo_dir(name)),
        "snapshot": str(snap) if snap else None,
        "cached": snap is not None,
        ENV: os.environ.get(ENV),
        "proxy": {k: os.environ.get(k) for k in
                  ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
                  if os.environ.get(k)},
    }
