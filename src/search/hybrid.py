"""1-C 하이브리드 검색기 — 하드필터 우선 + BM25/dense RRF 융합.

【설계의 핵심: 하드필터를 먼저 건다】
회사·연도·문서로 후보를 좁힌 **뒤** 그 안에서만 점수를 낸다.
전체에서 top-k 를 뽑고 나중에 거르면, 회사 하나로 좁혔을 때 0건이 된다.

【코드 체계 주의 — 치명 A】
id_map.parquet 의 corp_code 컬럼은 실제로 6자리 종목코드다.
universe.csv 의 corp_code 는 8자리 DART 고유번호로 값 체계가 다르다.
여기서는 로드하자마자 stock_code 로 이름을 바꿔 혼동을 코드에서 제거한다.
필터 파라미터로 corp_code(8자리)를 받지 않는다 — 남겨두면 누군가 8자리를 넣고,
그러면 필터가 항상 0건이 되면서 에러도 경고도 나지 않는다.

【base_year 주의】
id_map 의 base_year 는 **접수 연도**(rcept_dt 앞 4자리)다. 사업연도가 아니다.
2024 사업연도 사업보고서는 2025년에 접수되므로 base_year=2025 다.
사업연도로 거르려면 report_nm 의 '(2024.12)' 를 봐야 한다 — fiscal_year 인자를 쓴다.

  from src.search.hybrid import Retriever
  rt = Retriever()
  rt.search("연결 기준 매출액", corp_name="삼성전자", fiscal_year=2024, top_k=5)
"""
from __future__ import annotations

import json
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from src.eval.chunk_store import ChunkStore
from src.index import paths
from src.search.aliases import generic_name_hint, resolve_company
from src.search.bm25 import BM25

MASK = paths.ROOT / "data" / "index" / "latest_mask.npz"
MANIFEST = paths.ROOT / "corpus" / "manifest.jsonl"
RRF_K = 60          # 표준값. 1위가 1/61, 200위가 1/260
TOP_M = 200         # 각 리스트에서 이 순위 안에 든 것만 융합에 기여한다
PER_DOC = 2         # 한 문서가 결과를 독점하지 못하게
POOL = 50           # 리랭커(단계 4) 입력이 될 후보 수
TEXT_CUT = 1500     # 반환 본문 컷
SHORT_CHUNK = 150   # 규칙 2: 이 길이 미만이면 '제목만 있는 청크' 후보
LOOKBACK = 6        # 규칙 1: 몇 칸까지 거슬러 올라갈지.
                    # 2였을 때 '앞 2칸에 캡션 없음' 으로 놓친 것의 46%가 3~8칸 뒤에 있었다.
                    # 넓은 표가 여러 조각으로 쪼개진 경우다(KB금융 자본변동표가 3조각).
                    # 거리가 늘어도 '남의 표를 만나면 중단' 은 그대로라 안전하다.
UNIT_MARK = "(단위"
CAP_TAIL = ("다음과 같습니다", "다음과 같음", "다음과 같다", "현황")
AUG_HEAD = "[인접 청크에서 보강: 단위·제목]"
AUG_NEXT = "[인접 청크에서 보강: 다음 청크의 표]"
AUG_BODY = "[여기부터 검색된 청크 원문]"
_NONDIGIT = re.compile(r"[^0-9]")
# 절 제목 대조용 — 공백·구분자·구두점을 지우고 붙여서 본다
_NORM_TITLE = re.compile(r"[\s>·\-—_()\[\]{}.,:;'\"]+")
_BUILT = 0          # 이 프로세스에서 Retriever 를 몇 번 만들었나
_FY = re.compile(r"\((\d{4})[.\-/](\d{1,2})\)")


# ---------------------------------------------------------------- 인접 청크 보강
#
# 【왜 필요한가】
# 청커가 표 제목과 표 본문을 다른 청크로 갈라놓는 일이 많다. 전량 조사 결과
#   - 제목 없이 곧바로 표 행으로 시작하는 청크 361,954개 (58.9%)
#   - 제목만 있고 표는 다음 청크에 있는 청크 17,081개 (2.8%)
# 앞의 것이 특히 위험하다. 표 안에 '(단위 : 백만원)' 이 없으면 LLM 이 단위를 추측하고,
# 실제로 KB금융 자기자본을 백만원 표에서 읽고 십억원을 붙여 1000배 틀린 적이 있다.
#
# 【왜 앞뒤를 무조건 붙이지 않는가】
# 다른 표의 단위가 섞이면 지금보다 나빠진다. 그래서 규칙을 좁게 잡았다.
#   규칙 1  표로 시작하고 단위 표기가 없을 때만, 최대 2칸 거슬러 올라가
#           단위·제목 **줄만** 뽑아 앞에 붙인다. 중간에 다른 표를 만나면 중단한다
#   규칙 2  짧고(150자 미만) 표가 없을 때만, 바로 다음 청크가 표일 때 이어 붙인다
# 둘은 조건이 배타적이라(하나는 표로 시작, 하나는 표가 없음) 동시에 걸리지 않는다.
# doc_id 경계는 절대 넘지 않는다.


def _lines(text: str) -> list:
    return [l for l in (text or "").splitlines() if l.strip()]


def _starts_with_table(text: str) -> bool:
    ls = _lines(text)
    return bool(ls) and ls[0].lstrip().startswith("|")


def _has_table(text: str) -> bool:
    return any(l.lstrip().startswith("|") for l in (text or "").splitlines())


def _caption_end(line: str) -> bool:
    t = (line or "").rstrip()
    if not t:
        return False
    if t.endswith(")") or t.endswith("）"):
        return True
    return any(t.endswith(w) or t.endswith(w + ".") for w in CAP_TAIL)


CAP_MAX = 120       # 단위 없는 '제목' 줄로 인정할 최대 길이


def _pick(lines: list) -> list:
    """단위·제목 줄만 고른다. 표 데이터 행은 캡션이 아니다.

    '|' 가 2개 이상인 줄을 빼는 이유: 표 안의 데이터 행이 '(단위: 원)' 을 품고 있는
    경우가 있어서, 그걸 캡션으로 착각하면 남의 표 단위를 끌어온다.
    """
    out = []
    for i, raw in enumerate(lines):
        t = raw.strip()
        if t.count("|") >= 2:
            continue
        if UNIT_MARK in t:
            out.append(t)
        elif i == len(lines) - 1 and len(t) <= CAP_MAX and _caption_end(t):
            out.append(t)
    return out[-2:]


def _caption_lines(prev_text: str, cur_head: str):
    """앞 청크에서 붙일 줄을 고른다. (줄 목록, 계속 거슬러 올라가도 되는가).

    앞 청크가 표를 품고 있으면 위험하다 — 그 표의 단위를 우리 표에 붙일 수 있다.
    그래서 두 경우만 허용한다.
      (a) 그 표의 마지막 행 **뒤**에 붙은 꼬리 캡션 -> 우리 표를 여는 제목이다
      (b) 우리 청크가 그 표의 이어짐(첫 표 행이 글자 그대로 같다) -> 표 앞 단위가 맞다
    둘 다 아니면 빈 목록 + '멈춰라' 를 돌려준다.
    """
    ls = _lines(prev_text)
    if not ls:
        return [], True
    tidx = [i for i, l in enumerate(ls) if l.lstrip().startswith("|")]
    if not tidx:
        # 표가 없는 청크. 여기서 캡션을 못 찾으면 **더 올라가지 않는다.**
        # 산문 한 덩이가 사이에 끼어 있으면 그 위는 다른 이야기다. 실제로 거리를 6으로
        # 늘렸을 때 CJ제일제당 신용평가 표에 3칸 위의 '(첨부 이미지: 대표이사 확인서)'
        # 가 붙었다. 사슬은 '같은 표의 조각' 으로만 이어야 한다.
        return _pick(ls), False

    tail = _pick(ls[tidx[-1] + 1:])                  # (a) 꼬리 캡션
    if tail:
        return tail, False
    if cur_head and ls[tidx[0]].strip() == cur_head:  # (b) 같은 표의 이어짐
        # 표 앞에 아무것도 없으면 **더 거슬러 올라가도 된다.** 같은 표라는 게 확인됐으니
        # 그 위의 제목 청크에 있는 단위가 이 표의 단위다.
        # (KB금융 자본변동표가 제목 / 표1 / 표2 세 청크로 쪼개져 있어 실제로 필요했다)
        return _pick(ls[:tidx[0]]), True
    return [], False                                 # 남의 표 — 붙이지 않고 멈춘다


def _join_note(*parts) -> str:
    return " · ".join(p for p in parts if p)


def _ymd(v):
    """'2024-03-17' / '20240317' 어느 쪽으로 와도 'YYYYMMDD' 로 통일한다."""
    if v is None:
        return None
    s = _NONDIGIT.sub("", str(v))
    return s or None


def _as_list(v):
    if v is None:
        return None
    return [v] if isinstance(v, (str, int, np.integer)) else list(v)


# report_type 은 manifest 의 doc_subtype 을 그대로 쓴다.
# periodic 의 doc_subtype 은 annual / half / quarter 세 값뿐이다(전량 확인).
# LLM 이 한국어 이름으로 넣는 일이 잦아 별칭만 받아 준다. 없는 값은 지어내지 않는다.
REPORT_TYPE_ALIAS = {
    "annual": "annual", "half": "half", "quarter": "quarter",
    "사업보고서": "annual", "반기보고서": "half", "분기보고서": "quarter",
}


def _norm_report_type(v):
    """(doc_subtype 값 리스트, 모르는 값 리스트). 모르는 값은 버리지 않고 알린다.

    모르는 값을 조용히 무시하면 필터가 안 걸린 채 결과가 나가고, 왜 이상한지
    아무도 모른다(CLAUDE.md 규칙 9). 그래서 그대로 넣어 0건이 되게 하고 note 로 알린다.
    """
    if v is None:
        return None, []
    want, bad = [], []
    for x in _as_list(v):
        t = str(x).strip()
        if not t:
            continue
        k = REPORT_TYPE_ALIAS.get(t)
        if k is None:
            bad.append(t)
            want.append(t)
        else:
            want.append(k)
    return (want or None), bad


class Retriever:
    def __init__(self, device: str = "cuda", gpu: int = 0, verbose: bool = True) -> None:
        # GPU 가 없거나 FORCE_CPU=1 이면 여기서 한 번에 CPU 로 내려앉는다.
        # 아래 코드는 self.device 만 보고 갈라지므로 판단은 이 한 줄이면 된다.
        from src.eval import gpu_guard

        device = gpu_guard.device_str(device)
        # 한 프로세스에서 두 번 만들면 dense 와 임베딩 모델이 통째로 두 벌 올라간다
        # (실측: GPU 1.26GB → 2.52GB, 모델 로드 2회). 서버는 get_searcher() 만 써야 한다.
        # _INSTANCE 가 아니라 만든 횟수를 센다 — 직접 만든 뒤 get_searcher() 를 부르는
        # 순서에서도 잡아야 한다(그때 _INSTANCE 는 아직 None 이다).
        global _BUILT
        _BUILT += 1
        if _BUILT > 1:
            print(f"[hybrid] Retriever 를 {_BUILT}번째 만들고 있다. 인덱스와 임베딩 "
                  f"모델이 그만큼 올라간다 — get_searcher() 로 하나만 쓸 것",
                  file=sys.stderr)
        self.device = device
        self.gpu = gpu
        self._verbose = verbose
        t0 = time.time()

        idm = pd.read_parquet(paths.ID_MAP)
        # C1 — 이름 충돌을 여기서 없앤다
        idm = idm.rename(columns={"corp_code": "stock_code", "company": "corp_name_idx"})

        # C4 — dtype 명시 캐스팅 + 결측률 로그
        idm["stock_code"] = idm["stock_code"].astype("string").str.zfill(6)
        idm["base_year"] = idm["base_year"].astype("Int64")
        for c in ("doc_group", "doc_id", "chunk_id", "receipt_no",
                  "rcept_dt", "corp_name_idx", "embed_sha1"):
            idm[c] = idm[c].astype("string")
        miss = {c: int(idm[c].isna().sum()) for c in idm.columns}
        bad = {c: v for c, v in miss.items() if v}
        self._log(f"[idmap] {len(idm):,}행 · 결측 " + (f"{bad}" if bad else "없음"))

        man = pd.DataFrame([json.loads(l) for l in open(MANIFEST, encoding="utf-8")])
        man = man[["rcept_no", "report_nm", "doc_subtype"]].rename(
            columns={"rcept_no": "receipt_no"})
        man["receipt_no"] = man["receipt_no"].astype("string")
        idm = idm.merge(man, on="receipt_no", how="left")
        if idm["report_nm"].isna().any():
            raise SystemExit("manifest 조인 실패 — report_nm 결측")

        self.n = len(idm)
        self.stock_code = idm["stock_code"].to_numpy(dtype=object)
        self.company = idm["corp_name_idx"].to_numpy(dtype=object)
        self.doc_group = idm["doc_group"].to_numpy(dtype=object)
        self.base_year = idm["base_year"].to_numpy(dtype=np.int32)
        self.rcept_dt = idm["rcept_dt"].to_numpy(dtype=object)
        self.receipt_no = idm["receipt_no"].to_numpy(dtype=object)
        self.receipt_i = idm["receipt_no"].astype(np.int64).to_numpy()
        self.doc_id = idm["doc_id"].to_numpy(dtype=object)
        self.chunk_id = idm["chunk_id"].to_numpy(dtype=object)
        self.embed_sha1 = idm["embed_sha1"].to_numpy(dtype=object)
        self.report_nm = idm["report_nm"].to_numpy(dtype=object)
        self.doc_subtype = idm["doc_subtype"].to_numpy(dtype=object)
        # report_nm 의 '(2024.12)' 에서 사업연도를 뽑아둔다 (base_year 는 접수연도라 다르다)
        fy = idm["report_nm"].str.extract(_FY)[0]
        self.fiscal_year = fy.fillna(-1).astype(np.int32).to_numpy()

        # C5 — is_latest 는 latest_mask.npz 가 정본이다.
        # manifest 의 is_correction 으로 거르면 정확히 반대로 작동한다(정정본이 최신본이다).
        z = np.load(MASK)
        self.is_latest = z["is_latest"]
        self.preserved = z["preserved"]
        self.series_code = z["series_code"]
        for name, arr in (("is_latest", self.is_latest), ("preserved", self.preserved),
                          ("series_code", self.series_code)):
            if arr.shape[0] != self.n:
                raise SystemExit(f"[mask] {name} 길이 {arr.shape[0]:,} != id_map {self.n:,}")
        n_excl = int((~self.is_latest).sum())
        self._log(f"[correction] 구본 제외 {n_excl:,}행 · 보존 {int(self.preserved.sum()):,}행")
        if n_excl == 0:
            raise SystemExit("[correction] 제외 0건 — latest_mask 가 비었다. 중단")

        self.bm25 = BM25()
        if self.bm25.n != self.n:
            raise SystemExit(f"[bm25] 행 수 {self.bm25.n:,} != id_map {self.n:,}")
        self.store = ChunkStore()
        self._load_dense()
        self._load_model()
        self._log(f"[load] 총 {time.time() - t0:.1f}초")

    def _log(self, m: str) -> None:
        if self._verbose:
            print(m, flush=True)

    def _load_dense(self) -> None:
        t = time.time()
        arr = np.load(paths.DENSE, mmap_mode="r")
        if arr.shape != (self.n, paths.DIM):
            raise SystemExit(f"dense shape {arr.shape} != {(self.n, paths.DIM)}")
        if self.device == "cuda":
            import torch

            from src.eval import gpu_guard

            gpu_guard.apply_memory_fraction(self.gpu, None)
            self.torch = torch
            self.dense = torch.empty((self.n, paths.DIM), dtype=torch.float16, device="cuda")
            for i in range(0, self.n, 50_000):
                blk = np.asarray(arr[i:i + 50_000], dtype=np.float32)
                self.dense[i:i + 50_000] = torch.from_numpy(blk).to("cuda", torch.float16)
            self._log(f"[load] dense -> GPU fp16 "
                      f"{torch.cuda.memory_allocated() / 1e9:.2f}GB ({time.time() - t:.1f}초)")
        else:
            self.torch = None
            self.dense = np.asarray(arr, dtype=np.float32)
            self._log(f"[load] dense -> CPU fp32 ({time.time() - t:.1f}초)")

    def _load_model(self) -> None:
        t = time.time()
        # 네트워크를 치지 않게 먼저 막는다. 캐시가 없으면 여기서 이유를 적고 죽는다 —
        # dense 를 GPU 에 올린 뒤 모델 초기화에서 조용히 멈추는 것이 제일 나쁘다.
        from src.search import hf_cache

        snap = hf_cache.ensure_offline(paths.MODEL_NAME)
        self._log(f"[load] 모델 캐시 {snap}")

        from FlagEmbedding import BGEM3FlagModel

        # CPU 에서 fp16 은 지원되지 않거나 오히려 느리다. 장치에 맞춰 정한다.
        on_gpu = self.device == "cuda"
        self.model = BGEM3FlagModel(
            paths.MODEL_NAME, use_fp16=on_gpu, normalize_embeddings=True,
            devices="cuda:0" if on_gpu else "cpu")
        self._log(f"[load] {paths.MODEL_NAME} ({time.time() - t:.1f}초)")

    # ---------------------------------------------------------------- 1. 하드필터

    def _candidates(self, stock_code=None, base_year=None, fiscal_year=None,
                    doc_group=None, rcept_no=None, rcept_dt=None,
                    rcept_dt_from=None, rcept_dt_to=None,
                    include_superseded=False, report_type=None) -> np.ndarray:
        m = np.ones(self.n, dtype=bool)
        if not include_superseded:                       # C6
            m &= self.is_latest
        if stock_code:                                   # C2 — 6자리만 받는다
            m &= np.isin(self.stock_code, [str(c).zfill(6) for c in _as_list(stock_code)])
        if base_year is not None:
            m &= np.isin(self.base_year, [int(y) for y in _as_list(base_year)])
        if fiscal_year is not None:
            m &= np.isin(self.fiscal_year, [int(y) for y in _as_list(fiscal_year)])
        if doc_group:
            m &= np.isin(self.doc_group, _as_list(doc_group))
        if report_type:                                  # 사업/반기/분기 보고서 구분
            m &= np.isin(self.doc_subtype, _as_list(report_type))
        if rcept_no:                                     # C3 — 문서 특정
            m &= np.isin(self.receipt_no, [str(v) for v in _as_list(rcept_no)])
        if rcept_dt:                                     # C3 — 접수일 특정
            m &= np.isin(self.rcept_dt, [_ymd(v) for v in _as_list(rcept_dt)])
        f, t = _ymd(rcept_dt_from), _ymd(rcept_dt_to)
        if f:
            m &= self.rcept_dt >= f
        if t:
            m &= self.rcept_dt <= t
        return np.flatnonzero(m)

    # ---------------------------------------------------------------- 2~3. 점수

    def _dense_scores(self, query: str, cand: np.ndarray) -> np.ndarray:
        out = self.model.encode([query], batch_size=1, max_length=512,
                                return_dense=True, return_sparse=False,
                                return_colbert_vecs=False)
        q = np.asarray(out["dense_vecs"][0], dtype=np.float32)
        # C8 — 후보가 넓으면 전량 곱하고 뒤에서 뽑는다. 좁으면 후보만 잘라 곱한다.
        #      팬시 인덱싱은 후보가 넓을 때 행렬 전체를 복사한다.
        wide = cand.size > 0.3 * self.n
        if self.torch is not None:
            qt = self.torch.from_numpy(q).to("cuda", self.torch.float16)
            if wide:
                s = (self.dense @ qt).float().cpu().numpy()[cand]
            else:
                idx = self.torch.from_numpy(np.ascontiguousarray(cand)).to("cuda")
                s = (self.dense[idx] @ qt).float().cpu().numpy()
        else:
            s = (self.dense @ q)[cand] if wide else self.dense[cand] @ q
        return s

    # ---------------------------------------------------------------- 4. RRF

    def _section_scores(self, cand: np.ndarray, query: str, vsc: np.ndarray,
                        bsc: np.ndarray, top_m: int) -> np.ndarray:
        """절 제목이 질의어를 얼마나 담고 있는지. 0~1. 나머지는 0.

        【왜 후보 전체가 아니라 top-M 합집합만 보는가】
        절 제목은 id_map 에 없고 청크 저장소를 행마다 seek 해서 읽어야 한다.
        후보가 수천~수십만이면 그걸 다 읽는 값이 검색 시간을 통째로 잡아먹는다.
        어차피 RRF 는 각 리스트 top-M 안에 든 것만 쓰므로(C7), 두 리스트의
        top-M 합집합 밖은 융합에 기여할 수 없다. 그 밖은 읽지 않고 0 으로 둔다.

        【점수는 '질의어 몇 개가 제목에 있나' 뿐이다】
        BM25 를 제목에 다시 돌리지 않는다. 제목은 짧은 명사구라 길이 정규화가
        의미 없고, 새 색인을 만들면 행 순서 계약(CLAUDE.md 3)에 물건이 하나 더 는다.
        """
        from src.search.tokenize_ko import tokenize

        n = len(cand)
        out = np.zeros(n, dtype=np.float32)
        toks = {t for t in tokenize(query or "") if len(t) >= 2}
        if not toks:
            return out

        M = min(top_m, n)
        pick = set()
        for sc in (vsc, bsc):
            top = np.argpartition(-sc, M - 1)[:M] if M < n else np.arange(n)
            pick.update(int(i) for i in top)

        for i in pick:
            rec = self.store.get(int(cand[i])) or {}
            title = " ".join(rec.get("section_path") or [])
            if not title:
                continue
            flat = _NORM_TITLE.sub("", title).lower()
            hit = sum(1 for t in toks if t.lower() in flat)
            if hit:
                out[i] = hit / len(toks)
        return out

    def _rrf(self, vsc: np.ndarray, bsc: np.ndarray, w_vec: float = 1.0,
             w_bm25: float = 1.0, top_m: int = TOP_M, K: int = RRF_K,
             ssc: np.ndarray | None = None, w_section: float = 0.0) -> np.ndarray:
        """C7 — 각 리스트의 top-M 안에 든 후보만 기여한다.

        후보 전체에 순위를 매겨 융합하면, 질의어를 하나도 포함하지 않은 문서가
        argsort 에서 임의의 중간 순위를 배정받아 그 순위가 점수에 기여한다.
        BM25 0점은 '그 리스트에 없음' 으로 취급해 버린다.

        `ssc`(절 제목 점수)는 **선택**이다. w_section=0 이면 리스트 자체가 없다 —
        기본 동작은 vec/bm25 두 리스트 그대로다.
        """
        n = len(vsc)
        out = np.zeros(n, dtype=np.float32)
        M = min(top_m, n)
        lists = [(vsc, w_vec, False), (bsc, w_bm25, True)]
        if ssc is not None and w_section > 0:
            lists.append((ssc, w_section, True))
        for sc, w, drop_zero in lists:
            if M < n:
                top = np.argpartition(-sc, M - 1)[:M]
            else:
                top = np.arange(n)
            top = top[np.argsort(-sc[top])]
            if drop_zero:
                top = top[sc[top] > 0]
            out[top] += w / (K + np.arange(len(top), dtype=np.float32) + 1)
        return out

    # ---------------------------------------------------------------- 5~6. 조립

    def search(self, query: str, corp_name=None, stock_code=None, base_year=None,
               fiscal_year=None, doc_group=None, report_type=None,
               rcept_no=None, rcept_dt=None,
               rcept_dt_from=None, rcept_dt_to=None, top_k: int = 5, pool: int = POOL,
               top_m: int = TOP_M, per_doc: int = PER_DOC, w_vec: float = 1.0,
               w_bm25: float = 1.0, w_section: float = 0.0,
               include_superseded: bool = False,
               **_ignored) -> dict:
        t0 = time.time()
        rtype, rtype_bad = _norm_report_type(report_type)
        filters = {"corp_name": corp_name, "stock_code": stock_code,
                   "base_year": base_year, "fiscal_year": fiscal_year,
                   "doc_group": doc_group, "report_type": rtype, "rcept_no": rcept_no,
                   "rcept_dt": _ymd(rcept_dt),
                   "rcept_dt_from": _ymd(rcept_dt_from), "rcept_dt_to": _ymd(rcept_dt_to),
                   "include_superseded": include_superseded}
        empty = {"query": query, "filters": filters, "results": [], "pool": [],
                 "n_candidates": 0, "n_pool": 0, "n_preserved_dropped": 0}

        # C11 — 0건은 예외가 아니다
        if corp_name:
            r = resolve_company(corp_name)
            if r["status"] == "not_found":
                note = f"코퍼스 70개사에 '{corp_name}' 이 없다"
                # '전체' 류 낱말이면 corp_name 을 빼라는 해결책을 덧붙인다.
                # 판단은 aliases.generic_name_hint 한 곳에서만 한다 — 두 곳에서
                # 따로 판단하면 안내가 갈리고, 갈린 안내는 모델이 못 고친다.
                hint = generic_name_hint(corp_name)
                if hint:
                    note += " · " + hint
                return {**empty, "status": "not_found", "note": note}
            if r["status"] == "ambiguous":
                return {**empty, "status": "ambiguous", "candidates": r["candidates"],
                        "note": r.get("note", "회사가 특정되지 않는다")}
            stock_code = r["stock_code"]                  # C2
            filters["stock_code"] = stock_code

        cand = self._candidates(stock_code, base_year, fiscal_year, doc_group,
                                rcept_no, rcept_dt, rcept_dt_from, rcept_dt_to,
                                include_superseded, report_type=rtype)
        if cand.size == 0:
            note = "필터 조건에 맞는 문서가 없다"
            if rtype_bad:
                note += (f" · report_type={rtype_bad} 를 모른다"
                         " — annual(사업보고서)/half(반기보고서)/quarter(분기보고서) 중 하나여야 한다")
            elif rtype:
                note += f" · report_type={rtype} 를 빼고 다시 호출하면 잡힐 수 있다"
            return {**empty, "status": "not_found", "note": note}

        bsc = self.bm25.score(query, rows=cand)
        vsc = self._dense_scores(query, cand)
        # w_section=0 이 기본이다. 0 이면 절 제목을 읽지도 않는다(청크 저장소 seek 없음).
        #
        # 【미채택 — 2026-09-03 측정. 기본 0 을 유지한다. 지우지 말 것】
        # 골든셋 18문항 recall@5: 0.0=0.361 · 0.3=0.306 · 0.5=0.361 · 1.0=0.278.
        # 어느 값도 0 을 넘지 못했다. 0.3 에서 recall@20 만 0.722→0.778 로 오르는데
        # recall@5 는 떨어진다 — 위쪽 순위를 흔들면서 아래만 채운 것이라 쓸모가 없다.
        # 기대와 달리 **짧은 질의(2어절)에서 더 나빴다**(recall@20 0.500→0.333).
        # 절 제목은 재무제표 절에서 문서마다 거의 같은 문자열이라 변별력이 아니라
        # 잡음으로 들어간다. 표본이 18·3문항이라 "확실히 나쁘다" 까지는 못 말하고
        # **"좋아진다는 근거가 없다"** 까지만 말할 수 있다.
        # 재측정: python eval/sweep_section.py · 표: reports/sweep_section.md
        ssc = (self._section_scores(cand, query, vsc, bsc, top_m)
               if w_section > 0 else None)
        rrf = self._rrf(vsc, bsc, w_vec, w_bm25, top_m, ssc=ssc, w_section=w_section)

        order = np.flatnonzero(rrf > 0)
        if order.size == 0:
            return {**empty, "status": "not_found", "n_candidates": int(cand.size),
                    "note": "질의어를 포함하거나 의미가 가까운 청크가 없다"}
        # 정렬 키: RRF 내림차순, 동점이면 접수번호 큰 쪽(최신본) 우선 — C9(b)
        order = sorted(order.tolist(),
                       key=lambda i: (-float(rrf[i]), -int(self.receipt_i[cand[i]])))

        # C9 — (a) 문서당 상한 → (b) sha1 중복 제거 → pool 확보
        picked: list[tuple[int, int]] = []               # (전체 row, 후보 인덱스)
        seen_sha: set = set()
        per: dict = {}
        for i in order:
            row = int(cand[i])
            d = self.doc_id[row]
            if per.get(d, 0) >= per_doc:
                continue
            sha = self.embed_sha1[row]
            if sha in seen_sha:
                continue
            seen_sha.add(sha)
            per[d] = per.get(d, 0) + 1
            picked.append((row, i))
            if len(picked) >= pool:
                break

        # C10 — preserved 조건부 드롭. pool 을 다 모은 뒤, top_k 로 자르기 전에 적용한다.
        # preserved 는 정정본에 재수록되지 않은 구본 첨부(감사보고서·첨부재무제표)다.
        #
        # 【사양과 다른 점 — 승인 필요】
        # C10 의 코드는 "같은 계열 최신본이 pool 에 있으면 버린다"(순위 무관)인데,
        # 그러면 같은 사양의 E6b 가 깨진다. 실측:
        #   '매출액'   계열9 preserved 최초 9위 · 최신본 최초 1위 → 버리는 게 맞다(같은 수치의 중복)
        #   '감사의견' 계열9 preserved 최초 1위 · 최신본 최초 8위 → 1~4위 근거가 전부 사라진다
        #     (8위는 정정본의 'V. 회계감사인의 감사의견 등' 요약표이지 감사보고서 본문이 아니다)
        # 그래서 **그보다 위 순위에** 최신본이 이미 뽑혔을 때만 버린다. E6a·E6b 둘 다 통과한다.
        latest_series: set = set()
        kept: list[tuple[int, int]] = []
        for row, i in picked:
            ser = int(self.series_code[row])
            if self.preserved[row]:
                if ser in latest_series:
                    continue
            else:
                latest_series.add(ser)
            kept.append((row, i))
        n_dropped = len(picked) - len(kept)

        results = [self._render(r, i, rrf, vsc, bsc, k + 1)
                   for k, (r, i) in enumerate(kept[:top_k])]
        pool_out = [{"chunk_id": str(self.chunk_id[r]),
                     "text": (self.store.get(r).get("content") or "")[:TEXT_CUT]}
                    for r, _ in kept[:pool]]
        return {"status": "ok" if results else "not_found",
                "query": query, "filters": filters,
                "n_candidates": int(cand.size), "n_pool": len(kept),
                "n_preserved_dropped": n_dropped,
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
                "results": results, "pool": pool_out,
                "note": _join_note("" if results else "융합 후 남은 결과가 없다",
                                   f"report_type={rtype_bad} 를 모른다" if rtype_bad else "")}

    # ---------------------------------------------------------------- [F] 반환 스키마

    def _augment(self, row: int, text: str):
        """(보강된 본문, 무엇을 붙였는지). 조건이 안 맞으면 원문 그대로 돌려준다."""
        doc = self.doc_id[row]

        # --- 규칙 1: 표로 시작하는데 단위 표기가 없다 -> 앞에서 단위·제목을 찾아온다
        if _starts_with_table(text) and UNIT_MARK not in text:
            head = next((l.strip() for l in text.splitlines() if l.strip()), "")
            for back in range(1, LOOKBACK + 1):
                r = row - back
                if r < 0 or self.doc_id[r] != doc:      # doc_id 경계를 넘지 않는다
                    break
                prev = self.store.get(r).get("content") or ""
                caps, may_go_on = _caption_lines(prev, head)
                if caps:
                    return ("\n".join([AUG_HEAD, *caps, AUG_BODY, text]), "unit_header")
                if not may_go_on:
                    break                               # 남의 표를 만났다 -> 중단
            return text, None

        # --- 규칙 2: 짧고 표가 없다 -> 바로 다음 청크가 표일 때만 이어 붙인다
        if len(text) < SHORT_CHUNK and not _has_table(text):
            r = row + 1
            if r < self.n and self.doc_id[r] == doc:
                nxt = self.store.get(r).get("content") or ""
                if _has_table(nxt):
                    return "\n".join([text, AUG_NEXT, nxt]), "next_table"
        return text, None

    def _render(self, row: int, ci: int, rrf, vsc, bsc, rank: int) -> dict:
        rec = self.store.get(row)
        src = {
            "corp_name": str(self.company[row]),
            "report_nm": str(self.report_nm[row]),   # C12 — manifest 값. document_title 이 아니다
            "rcept_dt": str(self.rcept_dt[row]),
            "rcept_no": str(self.receipt_no[row]),
            "section": " > ".join(rec.get("section_path") or []),
            "note": "",
            # 아래는 스키마 밖 부가 정보
            "stock_code": str(self.stock_code[row]),
            "doc_group": str(self.doc_group[row]),
            "doc_subtype": str(self.doc_subtype[row]),
            "base_year": int(self.base_year[row]),
            "fiscal_year": int(self.fiscal_year[row]),
        }
        if self.preserved[row]:                      # C12
            d = src["rcept_dt"]
            src["note"] = (f"정정 전 첨부본(접수 {d[:4]}-{d[4:6]}-{d[6:8]}). "
                           "이후 정정본이 존재하나 이 섹션은 정정본에 없음")
        # 표 제목과 표 본문이 다른 청크로 갈린 경우만 좁게 보강한다. 자세한 규칙은
        # 파일 위쪽 '인접 청크 보강' 주석 참고. 자른 뒤가 아니라 자르기 **전에** 붙여야
        # 규칙 2에서 붙인 표가 통째로 날아가지 않는다.
        text, aug = self._augment(row, rec.get("content") or "")
        return {
            "chunk_id": str(self.chunk_id[row]),
            "text": text[:TEXT_CUT],
            "augmented": aug,
            "score": round(float(rrf[ci]), 6),
            # 원점수 그대로. 정규화하지 않는다 — 답 없는 질문을 점수로 거르는 데 쓴다
            "score_detail": {"vec": round(float(vsc[ci]), 6),
                             "bm25": round(float(bsc[ci]), 6)},
            "source": src,
            "rank": rank, "row": row, "doc_id": str(self.doc_id[row]),
            "preserved": bool(self.preserved[row]),
        }


# 이전 이름으로 부르는 코드를 위해 남긴다
HybridSearch = Retriever

_INSTANCE = None


def get_searcher(**kw) -> Retriever:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Retriever(**kw)
    return _INSTANCE


def search_disclosure(query: str, **kw) -> dict:
    return get_searcher().search(query, **kw)
