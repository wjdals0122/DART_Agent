"""검색 결과 -> LLM 에 넣을 컨텍스트 문자열.

한 청크는 이렇게 조립된다.

  ※ 자동 주석(공시 원문 아님): 제17기=2023년, 제16기=2022년 · 단위 천원 · 연결기준
  <document id="1" source="[CJ제일제당 분기보고서 (2023.03), 2023-05-15, 접수번호 20230515002270]"
            section="III. 재무에 관한 사항 > 1. 요약재무정보">
  ...본문...
  </document>

정정 전 자료(preserved)면 그 위에 경고 줄이 한 줄 더 붙는다.

  ※ 자동 주석(공시 원문 아님): 정정 전 자료(접수 2026-03-13).
    이후 정정본(접수 2026-06-19, 접수번호 20260619000667)이 제출되었으므로
    이 수치는 최종값이 아닐 수 있다

【자동 주석이 태그 바깥에 있는 이유】
공시 원문에서 유도한 값이 아니라 **메타데이터에서 계산한 추정**이다. 태그 안에 넣으면
모델이 원문과 구분할 수 없다. 바깥 윗줄에 두고, system 프롬프트에서 "원문과 어긋나면
원문을 따른다" 고 못박는다.

【태그 탈출 차단】
본문에 "</document>" 가 들어 있으면 그 뒤부터는 모델이 공시 원문이 아니라 지시문으로
읽을 수 있다. "[/document]" 로 치환한다.

【기수 -> 달력연도 환산은 '문서 단위' 로 한다 — 2026-08-31 변경】
예전 규칙은 "청크에 나온 가장 큰 기수 = 당기" 였다. 청크가 문서의 당기를 담고 있지
않으면 그대로 어긋난다. 정기보고서 40개 실측에서 주석 1,085건 중 232건(21.4%)이
기준을 잘못 잡았고, 'VI. 이사회 등' 절은 89.9% 가 틀렸다.
(예: CJ제일제당 분기보고서(2023.09)는 당기가 제17기인데, 제16기만 실린 청크에
 "제16기=2023년" 이 붙었다. 실제로는 2022년이다.)

그래서 문서마다 **재무제표 절에서 당기 기수를 한 번 구해** 표로 만들어 두고
(`data/index/doc_fiscal_map.json`), 청크의 기수를 그 표에 대고 환산한다.

  python -m src.agent.context --build

이 표는 git 에 안 들어간다(data/ 가 .gitignore 다). 새 환경에서 만드는 걸 잊으면
주석이 통째로 사라지는데 에러가 안 나서 알아채기 어렵다. 그래서 **표가 없으면
처음 쓸 때 자동으로 만든다**(`ensure_doc_map()`). 끄려면 DART_FISCAL_MAP_AUTOBUILD=0.
표가 있는데 읽히지 않으면 덮어쓰지 않고 경고만 띄운다(규칙 6).

실패하면 지어내지 않고 주석을 생략한다.
  - 문서가 표에 없다            -> 생략 (접수 연도로 때우지 않는다)
  - 청크의 기수 > 표의 당기 기수 -> 생략 + reports/annotate_era_overflow.jsonl 에 기록
  - 이벤트 공시(major/exchange/holding) -> 애초에 표에 넣지 않는다

【당기 '연도' 는 fiscal_year 다 — 사양과 다른 점, 보고서에 명시함】
사양은 "base_year 를 당기 연도로 삼는다" 였는데, 이 리포에서 `base_year` 는 **접수 연도**다
(id_map 기준. CLAUDE.md 와 hybrid.py 주석에도 같은 경고가 있다).
2024 사업연도 사업보고서는 2025년에 접수되므로 base_year=2025 이고, 이걸 당기로 쓰면
"제56기=2025년" 이라는 틀린 주석이 나간다. 그래서 report_nm 의 '(2024.12)' 에서 뽑은
`fiscal_year` 를 쓴다. 이 값도 문서 표에 같이 넣어 둔다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path

# 경고는 stderr 로 나가는데, 윈도우 콘솔 기본 코드페이지(cp949)에서는 한글이 깨진다.
# 이 리포의 다른 모듈이 stdout 에 하는 것과 같은 처리를 stderr 에도 한 번 해 둔다.
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[2]
DOC_MAP_PATH = ROOT / "data" / "index" / "doc_fiscal_map.json"
MASK_PATH = ROOT / "data" / "index" / "latest_mask.npz"
MANIFEST_PATH = ROOT / "corpus" / "manifest.jsonl"
OVERFLOW_LOG = ROOT / "reports" / "annotate_era_overflow.jsonl"
MAP_STATS = ROOT / "reports" / "doc_fiscal_map.json"
ERA_ROWS = ROOT / "reports" / "era_rows.npz"

# '제17기', '제 8 기', '제 17 기' 를 모두 잡는다
_ERA = re.compile(r"제\s*(\d{1,3})\s*기")
# '(단위 : 천원)', '(단위: 백만원, %)'
_UNIT = re.compile(r"\(\s*단위\s*[:：]\s*([^)]{1,20}?)\s*\)")
# report_nm 의 '(2024.12)'
_FY = re.compile(r"\((\d{4})[.\-/](\d{1,2})\)")
_SEPARATE_STMT = ("재무상태표", "손익계산서", "현금흐름표", "자본변동표", "대차대조표")
_CLOSE_TAG = re.compile(r"</\s*document\s*>", re.IGNORECASE)
_WS = re.compile(r"\s+")

# 문서의 '당기 기수' 를 어느 절에서 뽑는가. 측정 스크립트(sweep_era_error.py)와 같은 목록.
FIN_SECTIONS = ("연결재무제표", "재무제표", "요약재무정보",
                "재무상태표", "손익계산서", "현금흐름표")
# 절 판정은 section_path 의 **1~2번 칸(장·절 제목)** 만 본다.
# 측정 스크립트는 경로 전체를 이어붙여 봤는데, 그러면 각주 문구가 섞인다. 실제로
# LG생활건강 'II. 사업의 내용 > 3. 원재료 및 생산설비' 의 각주가
# "…'3. 연결재무제표 주석' 중 '16. 유형자산'을 참조" 라서 재무제표 절로 오인됐고,
# 그 표의 '예상 투자액(제24기)'(내년 계획) 때문에 당기 기수가 1 커졌다.
FIN_DEPTH = slice(1, 3)

UNIT_SCAN = 400        # 본문 앞 몇 자에서 단위를 찾을지
MAX_ERAS = 6           # 주석에 나열할 기수 상한
MAX_OVERFLOW_LOG = 5000   # 한 프로세스에서 남길 로그 줄 상한
REQUIRED = ("corp_name", "report_nm", "rcept_dt", "rcept_no")


def _norm(s: str) -> str:
    return _WS.sub("", (s or "")).lower()


def _attr(s: str) -> str:
    """속성값 이스케이프. '>' 는 속성 안에서 합법이라 그대로 둔다 —
    section 구분자 ' > ' 가 '&gt;' 로 바뀌면 읽기만 나빠진다."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _as_items(results) -> list:
    """search() 반환 dict 도, results 리스트도 받는다."""
    if isinstance(results, dict):
        return list(results.get("results") or [])
    return list(results or [])


def _section(src: dict) -> str:
    """section_path 첫 칸은 문서 제목이라 중복이다. 제목과 같으면 떼어낸다."""
    segs = [s for s in (src.get("section") or "").split(" > ") if s.strip()]
    rn = _norm(src.get("report_nm"))
    if segs and rn and rn in _norm(segs[0]):
        segs = segs[1:]
    return " > ".join(segs)


def _dash_date(d) -> str:
    d = str(d or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def _source_label(src: dict) -> str:
    return (f"[{src['corp_name']} {src['report_nm']}, {_dash_date(src.get('rcept_dt'))}, "
            f"접수번호 {src['rcept_no']}]")


# 근거 표기의 표준 포매터. 바깥에서 쓰는 이름은 이것이다.
fmt_source = _source_label


def _unit(section: str, text: str):
    for hay in (section, text[:UNIT_SCAN]):
        m = _UNIT.search(hay or "")
        if m:
            return _WS.sub(" ", m.group(1)).strip()
    return None


def _basis(section: str):
    if "연결" in section:
        return "연결기준"
    if "별도" in section:
        return "별도기준"
    if any(k in section for k in _SEPARATE_STMT):
        return "별도기준"
    return None


def fiscal_year_of(report_nm: str) -> int:
    """report_nm 의 '(2024.12)' 에서 사업연도. 없으면 -1."""
    m = _FY.search(report_nm or "")
    return int(m.group(1)) if m else -1


# ---------------------------------------------------------------- 문서 -> 당기 기수 표


class _DocFiscalMap:
    """doc_fiscal_map.json 을 doc_id / rcept_no 두 갈래로 찾을 수 있게 감싼다.

    검색 결과에는 doc_id 가 top-level 로 들어 있지만(hybrid._render), 단위 테스트처럼
    doc_id 없이 만든 결과도 있어서 rcept_no 로도 찾을 수 있게 해 둔다.
    같은 접수번호에 값이 어긋나는 항목이 둘 이상이면 그 접수번호는 **안 쓴다**(None).
    """

    def __init__(self, data: dict) -> None:
        self.by_doc: dict[str, dict] = {}
        self.by_rcept: dict[str, dict | None] = {}
        for doc_id, e in (data or {}).items():
            if not isinstance(e, dict) or "current_ki" not in e:
                continue
            self.by_doc[str(doc_id)] = e
            rc = str(e.get("rcept_no") or "")
            if not rc:
                continue
            prev = self.by_rcept.get(rc, "missing")
            if prev == "missing":
                self.by_rcept[rc] = e
            elif prev is not None and (
                    prev["current_ki"] != e["current_ki"]
                    or prev["fiscal_year"] != e["fiscal_year"]):
                self.by_rcept[rc] = None       # 충돌 — 지어내지 않고 포기한다

    def __len__(self) -> int:
        return len(self.by_doc)

    def lookup(self, doc_id=None, rcept_no=None):
        if doc_id:
            e = self.by_doc.get(str(doc_id))
            if e:
                return e
        if rcept_no:
            return self.by_rcept.get(str(rcept_no))
        return None


_MAP = None
_MAP_LOADED = False
_MAP_WARNED = False


def _banner(lines) -> None:
    """놓치면 안 되는 경고. 한 줄짜리 print 는 인덱스 로딩 로그에 묻힌다."""
    w = max(len(l) for l in lines) + 2
    print(chr(10) + "!" * (w + 2), file=sys.stderr)
    for l in lines:
        print("! " + l, file=sys.stderr)
    print("!" * (w + 2) + chr(10), file=sys.stderr, flush=True)


def _autobuild_on() -> bool:
    return (os.environ.get("DART_FISCAL_MAP_AUTOBUILD", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def ensure_doc_map(auto_build=None) -> tuple[bool, str]:
    """표가 쓸 수 있는 상태인지 보장한다. (성공여부, 설명) 을 돌려준다 — 예외 없음.

    표가 **없으면** 자동으로 만든다(약 14초, 청크 전량 1회 스캔).
    git 에 안 들어가는 파일이라 새 환경에서 --build 를 잊기 쉬운데,
    잊으면 기수 주석이 통째로 사라지면서 아무 에러도 안 난다. 조용한 실패라 위험하다.

    표가 **있는데 읽히지 않으면** 덮어쓰지 않는다(CLAUDE.md 규칙 6).
    사람이 보고 지우든 다시 만들든 결정할 일이다. 경고만 크게 띄운다.

    자동 빌드를 끄려면 DART_FISCAL_MAP_AUTOBUILD=0.
    호출 시점을 앞당기고 싶으면(인덱스 30초 올리기 전에) 진입점에서 직접 부른다.
    """
    if auto_build is None:
        auto_build = _autobuild_on()

    if DOC_MAP_PATH.exists():
        try:
            json.loads(DOC_MAP_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return False, (f"{DOC_MAP_PATH} 가 있는데 읽히지 않는다 "
                           f"({type(e).__name__}: {e}). 덮어쓰지 않는다 — "
                           f"파일을 지운 뒤 python -m src.agent.context --build")
        # 낡았는지도 본다. manifest 나 청크가 표보다 새 것이면 표가 뒤처진 것이다.
        stale = _stale_reason()
        return True, (f"표 정상 ({stale})" if stale else "표 정상")

    if not auto_build:
        return False, (f"{DOC_MAP_PATH} 가 없다. 자동 빌드가 꺼져 있다"
                       f"(DART_FISCAL_MAP_AUTOBUILD=0). "
                       f"python -m src.agent.context --build 로 만든다")

    _banner([f"{DOC_MAP_PATH.name} 가 없다 — 지금 만든다 (청크 전량 1회 스캔, 약 14초).",
             "이 표가 없으면 '제17기=2023년' 같은 기수 주석이 통째로 빠진다.",
             "끄려면 DART_FISCAL_MAP_AUTOBUILD=0"])
    try:
        st = build_doc_fiscal_map(verbose=True)
    except Exception as e:      # noqa: BLE001 — 여기서 터지면 도구 전체가 죽는다
        return False, (f"자동 빌드 실패 ({type(e).__name__}: {e}). "
                       f"청크 파일({paths_hint()})이 있는지 확인해라")
    return True, f"자동 빌드 완료 — doc_id {st.get('n_doc_ids', 0):,}건"


def paths_hint() -> str:
    try:
        from src.index import paths
        return str(paths.CHUNKS_DIR)
    except Exception:           # noqa: BLE001 — 안내 문구용이라 실패해도 그만
        return "data/processed/chunks_by_10_companies"


def _stale_reason():
    """표보다 새 입력 파일이 있으면 그 이름. 없으면 None. (덮어쓰지는 않는다)"""
    try:
        t = DOC_MAP_PATH.stat().st_mtime
        newer = [p.name for p in (MANIFEST_PATH,) if p.exists() and p.stat().st_mtime > t]
        from src.index import paths
        newer += [p.name for p in paths.chunk_files() if p.stat().st_mtime > t]
    except Exception:           # noqa: BLE001 — 판단이 안 서면 조용히 넘어간다
        return None
    if not newer:
        return None
    return ("주의: 표보다 새 입력이 있다 — " + ", ".join(newer[:3])
            + " · 다시 만들려면 python -m src.agent.context --build")


# ---------------------------------------------------------------- 기수 → 연도 역조회
#
# 【왜 — 2026-09-04 e2e_v5 실측 4건】
# "제50기" 질문에서 재작성·planner 가 기수를 **짐작으로** 연도 환산했다
# (fiscal_year=50 으로 검색해 6회 전부 0건, "제76기"→2023 으로 오환산 등).
# 표(doc_fiscal_map)가 있는데 안 썼다. 도구가 여기서 결정적으로 환산한다.
# 사전 확인(step39): 회사별 ki→연도는 70개사 전부에서 **충돌 0건**(1:1).
_KI_TABLE = None       # stock_code -> {current_ki: fiscal_year}


def _ki_table() -> dict:
    """doc_fiscal_map + manifest 를 조인해 회사별 (기수 → 회계연도) 표를 만든다."""
    global _KI_TABLE
    if _KI_TABLE is not None:
        return _KI_TABLE
    tbl: dict = {}
    try:
        ok, _msg = ensure_doc_map()
        if ok:
            d = json.loads(DOC_MAP_PATH.read_text(encoding="utf-8"))
            code_of = {}
            with open(MANIFEST_PATH, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    m = json.loads(line)
                    code_of[str(m.get("rcept_no"))] = str(m.get("stock_code") or "")
            for v in d.values():
                code = code_of.get(str(v.get("rcept_no")))
                ki, fy = v.get("current_ki"), v.get("fiscal_year")
                if code and ki and fy:
                    tbl.setdefault(code, {})[int(ki)] = int(fy)
    except Exception:           # noqa: BLE001 — 표가 없으면 환산 불가로 처리하면 된다
        traceback.print_exc(file=sys.stderr)
    _KI_TABLE = tbl
    return tbl


def fiscal_year_of_ki(corp_name, ki):
    """(회계연도|None, 안내문). 회사와 기수로 표를 역조회한다. 예외 없음.

    성공: (2025, "제50기를 2025 회계연도로 환산했다")
    실패: (None, "제50기 환산 불가, 연도 필터 없이 검색") — 짐작으로 채우지 않는다.
    """
    try:
        n = int(str(ki).strip())
    except (TypeError, ValueError):
        return None, f"ki={ki!r} 환산 불가(정수가 아니다), 연도 필터 없이 검색"
    try:
        from src.search.aliases import resolve_company
        r = resolve_company(str(corp_name or ""))
        if r.get("status") != "ok":
            return None, f"제{n}기 환산 불가(회사를 특정하지 못함), 연도 필터 없이 검색"
        y = _ki_table().get(str(r["stock_code"]), {}).get(n)
        if y is None:
            return None, f"제{n}기 환산 불가(표에 없음), 연도 필터 없이 검색"
        return y, f"제{n}기를 {y} 회계연도로 환산했다"
    except Exception as e:      # noqa: BLE001 — 환산 실패가 검색을 막으면 안 된다
        traceback.print_exc(file=sys.stderr)
        return None, f"제{n}기 환산 불가({type(e).__name__}), 연도 필터 없이 검색"


def _doc_map():
    """표를 한 번만 읽어 캐시한다. 없으면 None (예외 없음)."""
    global _MAP, _MAP_LOADED, _MAP_WARNED
    if not _MAP_LOADED:
        _MAP_LOADED = True
        ok, msg = ensure_doc_map()
        if ok:
            try:
                _MAP = _DocFiscalMap(json.loads(DOC_MAP_PATH.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                ok, msg = False, f"표를 읽는 중 실패 ({type(e).__name__}: {e})"
        if ok and msg.startswith("표 정상 ("):
            print(f"[context] {msg[len('표 정상 ('):-1]}", file=sys.stderr, flush=True)
        if not ok:
            _MAP = None
            if not _MAP_WARNED:
                _MAP_WARNED = True
                _banner(["기수 주석을 전부 생략한다 — '제17기=2023년' 힌트가 안 나간다.",
                         msg])
    return _MAP


def reset_caches() -> None:
    """테스트용. 표와 계열 색인을 다시 읽게 만든다."""
    global _MAP, _MAP_LOADED, _MAP_WARNED, _SERIES, _SERIES_LOADED, _OVERFLOW_N
    _MAP = None
    _MAP_LOADED = False
    _MAP_WARNED = False
    _SERIES = None
    _SERIES_LOADED = False
    _OVERFLOW_N = 0


_OVERFLOW_N = 0


def _log_overflow(res: dict, chunk_ki: int, entry: dict, why: str) -> None:
    """예외를 삼키지 않는다(규칙 9). 주석을 생략한 이유를 파일에 남긴다."""
    global _OVERFLOW_N
    if _OVERFLOW_N >= MAX_OVERFLOW_LOG:
        return
    _OVERFLOW_N += 1
    src = res.get("source") or {}
    rec = {"why": why, "chunk_id": res.get("chunk_id"), "doc_id": res.get("doc_id"),
           "rcept_no": src.get("rcept_no"), "corp_name": src.get("corp_name"),
           "report_nm": src.get("report_nm"), "section": src.get("section"),
           "chunk_max_ki": chunk_ki,
           "doc_current_ki": entry.get("current_ki") if entry else None,
           "doc_fiscal_year": entry.get("fiscal_year") if entry else None}
    try:
        OVERFLOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(OVERFLOW_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[context] 주석 생략 로그를 쓰지 못했다: {type(e).__name__}: {e}", flush=True)


def annotate(res: dict):
    """자동 주석 한 줄. 근거가 모자라면 None (예외 없음).

    기수 -> 연도 환산은 문서 표(`doc_fiscal_map.json`)의 당기 기수를 기준으로 한다.
    표에 없는 문서는 주석을 통째로 생략한다 — 틀린 연도를 붙이는 것보다 없는 게 낫다.
    """
    src = res.get("source") or {}
    text = res.get("text") or ""
    section = _section(src)

    eras = sorted({int(m) for m in _ERA.findall(text)}, reverse=True)
    if not eras:
        return None

    m = _doc_map()
    entry = m.lookup(res.get("doc_id") or src.get("doc_id"), src.get("rcept_no")) if m else None
    if entry is None:
        return None                # 표에 없다 — 접수 연도로 때우지 않는다

    cur = int(entry["current_ki"])
    fy = int(entry["fiscal_year"])
    if fy <= 0 or cur <= 0:
        return None

    # 표가 낡았는지 여기서 잡힌다. 검색 결과의 사업연도와 표의 사업연도가 다르면 못 믿는다.
    try:
        src_fy = int(src.get("fiscal_year") or -1)
    except (TypeError, ValueError):
        src_fy = -1
    if src_fy > 0 and src_fy != fy:
        _log_overflow(res, eras[0], entry, "fiscal_year 불일치")
        return None

    if eras[0] > cur:
        # 문서의 당기보다 큰 기수가 본문에 있다. 환산 기준이 흔들리므로 생략하고 기록한다.
        _log_overflow(res, eras[0], entry, "청크 기수 > 문서 당기 기수")
        return None

    years = ", ".join(f"제{e}기={fy - (cur - e)}년" for e in eras[:MAX_ERAS])

    parts = [years]
    unit = _unit(section, text)
    if unit:
        parts.append(f"단위 {unit}")
    basis = _basis(section)
    if basis:
        parts.append(basis)
    return "※ 자동 주석(공시 원문 아님): " + " · ".join(parts)


# ---------------------------------------------------------------- 정정 경고


class _SeriesLatest:
    """계열(series_code) -> 그 계열의 최신 접수번호·접수일.

    latest_mask.npz 의 series_code 가 정본이다(= (회사, 접두 뗀 report_nm) 로 묶은 계열).
    id_map 의 receipt_no 와 행이 1:1 이므로 둘을 겹쳐 계열별 최대 접수번호를 구하고,
    접수일은 manifest 에서 가져온다.
    """

    def __init__(self) -> None:
        import numpy as np
        import pandas as pd

        from src.index import paths

        z = np.load(MASK_PATH)
        series = z["series_code"]
        rc = pd.read_parquet(paths.ID_MAP, columns=["receipt_no"])["receipt_no"]
        rc = rc.astype(str).to_numpy()
        if len(rc) != len(series):
            raise ValueError(f"행 수 불일치: id_map {len(rc):,} vs latest_mask {len(series):,}")

        df = pd.DataFrame({"rc": rc, "ser": series})
        self.latest_of = {int(k): str(v) for k, v in df.groupby("ser")["rc"].max().items()}
        self.series_of = dict(zip(df["rc"].tolist(), (int(x) for x in df["ser"].tolist())))

        self.dt_of: dict[str, str] = {}
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                self.dt_of[str(d.get("rcept_no"))] = str(d.get("rcept_dt") or "")

    def latest_for(self, rcept_no: str):
        """(최신 접수번호, 최신 접수일). 자기 자신이 최신이거나 모르면 None."""
        ser = self.series_of.get(str(rcept_no))
        if ser is None:
            return None
        latest = self.latest_of.get(ser)
        if not latest or latest == str(rcept_no):
            return None
        return latest, self.dt_of.get(latest, "")


_SERIES = None
_SERIES_LOADED = False


def _series_latest():
    global _SERIES, _SERIES_LOADED
    if not _SERIES_LOADED:
        _SERIES_LOADED = True
        try:
            _SERIES = _SeriesLatest()
        except (OSError, ValueError, ImportError, KeyError) as e:
            _SERIES = None
            print(f"[context] 정정본 계열 색인을 만들지 못했다 — 정정 경고에서 "
                  f"정정본 접수정보를 뺀다. ({type(e).__name__}: {e})", flush=True)
    return _SERIES


def correction_note(res: dict):
    """preserved(정정 전 자료) 청크에 붙는 경고 줄. 아니면 None.

    검색 메타(`source.note`)에만 있던 경고를 프롬프트 본문으로 끌어올린 것이다.
    메타는 LLM 이 보지 못한다.
    """
    if not res.get("preserved"):
        return None
    src = res.get("source") or {}
    own = _dash_date(src.get("rcept_dt"))
    head = f"※ 자동 주석(공시 원문 아님): 정정 전 자료(접수 {own})."

    s = _series_latest()
    later = s.latest_for(src.get("rcept_no")) if s else None
    if later:
        no, dt = later
        tail = (f"이후 정정본(접수 {_dash_date(dt)}, 접수번호 {no})이 제출되었으므로 "
                f"이 수치는 최종값이 아닐 수 있다")
    else:
        # 모르는 걸 지어내지 않는다. preserved 라는 사실 자체는 확실하다.
        tail = "이후 정정본이 제출되었으므로 이 수치는 최종값이 아닐 수 있다"
    return head + "\n  " + tail


# ---------------------------------------------------------------- 보고기간 경고

# report_nm 앞의 '[기재정정]' '[첨부정정]' 같은 대괄호 접두를 떼어낸다
_NM_PREFIX = re.compile(r"^\s*(?:\[[^\]]{0,20}\]\s*)+")
# 반기·분기보고서만 경고한다. 사업보고서는 그 자체가 연간 확정치다.
_PART_YEAR = ("반기보고서", "분기보고서")


def period_note(res: dict):
    """반기·분기보고서 청크 위에 붙는 경고 줄. 아니면 None (예외 없음).

    【왜 필요한가 — 2026-09-03 API 실측】
    "SK하이닉스 2024년 연결 매출액" 에 **반기보고서(2024.06)의 상반기 매출
    12,394,044백만원**을 답했다. 연간 확정치가 아니다. 문서가 반기보고서라는 사실은
    `source` 라벨("[SK하이닉스 반기보고서 (2024.06), …]")에 분명히 있었는데도
    모델이 그냥 썼다 — **라벨은 출처 표기로 읽히지 경고로 읽히지 않는다.**

    정정 경고(`correction_note`)에서 통했던 방법을 그대로 쓴다. 고칠 지점
    **바로 윗줄**, `<document>` 태그 바깥에 한 줄로 적는다.

    검색 결과를 거르거나 순위를 바꾸지 않는다. 안내만 붙인다 —
    '2024년 상반기 매출' 처럼 반기 수치를 **일부러** 묻는 질문도 있기 때문이다.
    그래서 문장을 "연간 확정치를 묻는 질문이면" 으로 조건부로 쓴다.
    """
    src = (res or {}).get("source") or {}
    name = str(src.get("report_nm") or "")
    kind = next((k for k in _PART_YEAR if _NM_PREFIX.sub("", name).startswith(k)), None)
    if kind is None:
        return None
    fy = fiscal_year_of(name)
    year = f"{fy}년" if fy > 0 else "해당 사업연도"
    return ("※ 자동 주석(공시 원문 아님): "
            f"{kind}다. 여기 실린 손익·현금흐름 수치는 {year} 한 해 전체가 아니라 "
            f"보고기간까지의 값이다." + "\n"
            f'  {year} 연간 확정치를 묻는 질문이면 report_type="annual" 로 다시 검색할 것.')


# ---------------------------------------------------------------- 진행상황 표 경고

# 「XI. 그 밖에 … > 공시내용 진행 및 변경사항」 절을 알아보는 조건.
# 절 이름의 띄어쓰기·번호·조사가 회사마다 다르므로 공백을 지우고 세 조각을 모두 본다.
_PROGRESS_PARTS = ("공시내용", "진행", "변경")


def progress_note(res: dict):
    """정기보고서의 '공시내용 진행 및 변경사항' 청크 위에 붙는 경고 줄. 아니면 None.

    【왜 필요한가 — 2026-09-03 조사, docs/WORKLOG.md step8-C】
    HD현대일렉트릭 사업보고서(2024.12, 접수 2025-03-20)의 이 표에
    계약금액총액 **583억원**, 계약기간 **'24.1.30 ~ '26.1.30** 이 실려 있었다.
    그 계약은 2025-02-25 정정공시로 **644억원 / 2026-08-31** 로 바뀐 뒤였다.
    **정정보다 뒤에 접수된 보고서인데도 표가 갱신되지 않았다.** 원문끼리 어긋난 것이다.

    그 표에는 '정정 전' · '정정 후' 열이 **없다**. 값이 하나뿐이고 그게 옛것이다.
    그래서 "정정 후 열을 써라" 같은 규칙으로는 못 고친다. 표 바로 위에서
    **이 표가 요약이라는 사실과, 확정치를 어디서 봐야 하는지**를 알려 준다.

    period_note 와 같은 방식이다 — 검색 결과를 거르거나 순위를 바꾸지 않는다.

    【문구를 고쳤다 — 2026-09-04】
    옛 문구는 "이 표는 요약이며 개별 계약의 최신 확정치가 **아닐 수 있다** …
    거래소공시에서 확인하라" 였다. **표에서 멀어지게 만든다.**
    e2e 2번(HD현대중공업 MGC 2척)의 정답이 바로 이 표에 있었는데 모델은 안 봤다.

    두 사례가 정반대다. 그래서 어느 한쪽으로 단정하면 안 된다.

      HD현대중공업  | 2023.03.10(**변경공시2023.10.16**) … | 193,700 |  ← 변경 반영값
      HD현대일렉트릭 | '24.1.30 (변경공시 표기 없음)      … | 583억원 |  ← 정정 전 값

    가르는 것은 **신고일자 칸의 '(변경공시 …)' 표기**다. 있으면 그 값은 변경이
    반영된 뒤의 금액이고, 없으면 별도 정정공시가 있어도 표에 안 실렸을 수 있다.
    그래서 "쓰지 마라" 가 아니라 **"어떻게 읽어라"** 로 바꿨다.
    """
    src = (res or {}).get("source") or {}
    flat = _WS.sub("", str(src.get("section") or ""))
    if not flat or not all(p in flat for p in _PROGRESS_PARTS):
        return None
    return ("※ 자동 주석(공시 원문 아님): 이 표는 회사가 계약 진행상황을 정리한 것이다. "
            "신고일자 칸에 '(변경공시 …)' 표기가 있으면 그 계약금액총액은 변경이 "
            "반영된 값이다." + "\n"
            "  표기가 없어도 별도의 정정공시가 있을 수 있으니, 최종 확정치를 "
            "못 박아야 하면 해당 거래소공시(단일판매ㆍ공급계약 체결/정정)와 대조하라.")


# ---------------------------------------------------------------- 조립


def build_context(results) -> str:
    """검색 결과를 <document> 블록 문자열로 조립한다.

    source 에 있어야 할 필드가 없으면 기본값으로 채우지 않고 ValueError 로 멈춘다
    (CLAUDE.md 절대규칙 1).
    """
    items = _as_items(results)
    blocks = []
    for i, res in enumerate(items, 1):
        src = res.get("source") or {}
        missing = [k for k in REQUIRED if not src.get(k)]
        if missing:
            raise ValueError(
                f"검색 결과 {i}번(chunk_id={res.get('chunk_id')}) 의 source 에 "
                f"{missing} 가 없다. 기본값으로 채우지 않고 멈춘다")
        if "text" not in res:
            raise ValueError(f"검색 결과 {i}번(chunk_id={res.get('chunk_id')}) 에 text 가 없다")

        body = _CLOSE_TAG.sub("[/document]", res.get("text") or "")
        head = (f'<document id="{i}" '
                f'source="{_attr(_source_label(src))}"\n'
                f'          section="{_attr(_section(src))}">')
        notes = [n for n in (correction_note(res), period_note(res),
                             progress_note(res), annotate(res)) if n]
        prefix = "".join(n + "\n" for n in notes)
        blocks.append(prefix + head + "\n" + body + "\n</document>")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 표 만들기 (--build)


def build_doc_fiscal_map(out_path: Path = DOC_MAP_PATH, dump_rows: bool = False,
                         verbose: bool = True) -> dict:
    """정기보고서 문서마다 재무제표 절에서 당기 기수를 구해 표로 만든다.

    계산 로직은 측정 스크립트(sweep_era_error.py)와 같다 —
    **재무제표 계열 절에 나온 기수의 최댓값** 이 그 문서의 당기 기수다.

    문서 하나가 doc_id 여럿으로 쪼개지는 경우가 있다(본문 + 첨부 감사보고서 등, 210건).
    같은 접수번호면 같은 보고서이므로 접수번호 단위로 당기 기수를 모아 그 접수번호에
    속한 doc_id 전부에 같은 값을 준다. 그래야 재무제표가 첨부 쪽에만 있는 문서의
    본문 청크도 주석을 받는다.

    이벤트 공시(major/exchange/holding)와 report_nm 에 '(YYYY.MM)' 이 없는 문서는
    표에 넣지 않는다 — 사업연도를 모르는 채로 환산할 수는 없다.
    """
    import time
    from collections import Counter, defaultdict

    from src.search.source import iter_chunks_canonical

    t0 = time.time()

    man: dict[str, tuple[str, str]] = {}
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            man[str(d.get("rcept_no"))] = (str(d.get("report_nm") or ""),
                                           str(d.get("doc_group") or ""))

    fin_ki: dict[str, int] = {}            # rcept_no -> 재무제표 절 최대 기수
    docs_of: dict[str, set] = defaultdict(set)   # rcept_no -> {doc_id}
    fy_of: dict[str, int] = {}             # rcept_no -> fiscal_year
    cnt = Counter()
    # 검증용 원자료 (--dump-rows)
    rows_doc: list[str] = []
    rows_rc: list[str] = []
    rows_sec: list[str] = []
    rows_max: list[int] = []
    evid: list[tuple] = []                 # (rcept_no, 기수, 본문에 적힌 연도)
    # '제 17 기 1분기 (2023년 3월말)' 처럼 기수 옆에 연도가 같이 적힌 자리 — 독립 대조용
    ev_pat = re.compile(r"제\s*(\d{1,3})\s*기[^|\n]{0,24}?\(\s*(\d{4})년")

    for n, rec in enumerate(iter_chunks_canonical(), 1):
        rc = str(rec.get("receipt_no") or "")
        meta = man.get(rc)
        if meta is None:
            cnt["manifest 결측"] += 1
            continue
        report_nm, doc_group = meta
        sp = rec.get("section_path") or []
        sec = " > ".join(sp)
        fin_head = " > ".join(sp[FIN_DEPTH])
        content = rec.get("content") or ""
        eras = [int(x) for x in _ERA.findall(content)]
        mx = max(eras) if eras else -1

        if dump_rows:
            rows_doc.append(str(rec.get("doc_id") or ""))
            rows_rc.append(rc)
            rows_sec.append(sec)
            rows_max.append(mx)

        if doc_group != "periodic":
            cnt["이벤트 공시"] += 1
            continue
        fy = fiscal_year_of(report_nm)
        if fy <= 0:
            cnt["사업연도 없음"] += 1
            continue
        fy_of[rc] = fy
        docs_of[rc].add(str(rec.get("doc_id") or ""))
        if eras and any(k in fin_head for k in FIN_SECTIONS):
            fin_ki[rc] = max(fin_ki.get(rc, 0), mx)
            for a, b in ev_pat.findall(content):
                evid.append((rc, int(a), int(b)))
        if verbose and n % 100_000 == 0:
            print(f"  {n:,}행 · 접수번호 {len(fy_of):,}건 · 당기 확정 {len(fin_ki):,}건 "
                  f"({time.time() - t0:.0f}초)", flush=True)

    out: dict[str, dict] = {}
    for rc, ki in fin_ki.items():
        for doc_id in docs_of.get(rc, ()):
            out[doc_id] = {"current_ki": int(ki), "fiscal_year": int(fy_of[rc]),
                           "rcept_no": rc}

    no_fin = sorted(set(fy_of) - set(fin_ki))
    stats = {
        "n_periodic_receipts": len(fy_of),
        "n_receipts_with_current_ki": len(fin_ki),
        "n_receipts_without_fin_section": len(no_fin),
        "n_doc_ids": len(out),
        "receipts_without_fin_section": no_fin[:50],
        "skipped": dict(cnt),
        "seconds": round(time.time() - t0, 1),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    MAP_STATS.parent.mkdir(parents=True, exist_ok=True)
    MAP_STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    if dump_rows:
        import numpy as np

        np.savez_compressed(
            ERA_ROWS,
            doc_id=np.asarray(rows_doc), rcept_no=np.asarray(rows_rc),
            section=np.asarray(rows_sec), chunk_max_ki=np.asarray(rows_max, dtype=np.int16),
            evid=np.asarray(evid, dtype=object) if evid else np.zeros((0, 3)),
        )
        stats["era_rows"] = str(ERA_ROWS)

    if verbose:
        for k, v in stats.items():
            if k == "receipts_without_fin_section":
                continue
            print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")
        print(f"[doc_fiscal_map] {out_path} 기록 — doc_id {len(out):,}건")
    return stats


def main() -> int:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="문서별 당기 기수 표 만들기")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--dump-rows", action="store_true",
                    help="검증용 원자료를 reports/era_rows.npz 에 함께 남긴다")
    a = ap.parse_args()
    if not a.build:
        ap.print_help()
        return 0
    build_doc_fiscal_map(dump_rows=a.dump_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
