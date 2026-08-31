"""검색 결과 -> LLM 에 넣을 컨텍스트 문자열.

한 청크는 이렇게 조립된다.

  ※ 자동 주석(공시 원문 아님): 제17기=2023년, 제16기=2022년 · 단위 천원 · 연결기준
  <document id="1" source="[CJ제일제당 분기보고서 (2023.03), 2023-05-15, 접수번호 20230515002270]"
            section="III. 재무에 관한 사항 > 1. 요약재무정보">
  ...본문...
  </document>

【자동 주석이 태그 바깥에 있는 이유】
공시 원문에서 유도한 값이 아니라 **메타데이터에서 계산한 추정**이다. 태그 안에 넣으면
모델이 원문과 구분할 수 없다. 바깥 윗줄에 두고, system 프롬프트에서 "원문과 어긋나면
원문을 따른다" 고 못박는다.

【태그 탈출 차단】
본문에 "</document>" 가 들어 있으면 그 뒤부터는 모델이 공시 원문이 아니라 지시문으로
읽을 수 있다. "[/document]" 로 치환한다.

【당기 연도를 무엇으로 잡는가 — 사양과 다른 점, 보고서에 명시함】
사양은 "base_year 를 당기 연도로 삼는다" 였는데, 이 리포에서 `base_year` 는 **접수 연도**다
(id_map 기준. CLAUDE.md 와 hybrid.py 주석에도 같은 경고가 있다).
2024 사업연도 사업보고서는 2025년에 접수되므로 base_year=2025 이고, 이걸 당기로 쓰면
"제56기=2025년" 이라는 틀린 주석이 나간다. 그래서 report_nm 의 '(2024.12)' 에서 뽑은
`fiscal_year` 를 당기 연도로 쓴다. fiscal_year 가 없으면(이벤트 공시 등) 접수 연도로
때우지 않고 **주석을 통째로 생략한다**. 추정 연도를 지어내는 것보다 없는 게 낫다.
"""
from __future__ import annotations

import re

# '제17기', '제 8 기', '제 17 기' 를 모두 잡는다
_ERA = re.compile(r"제\s*(\d{1,3})\s*기")
# '(단위 : 천원)', '(단위: 백만원, %)'
_UNIT = re.compile(r"\(\s*단위\s*[:：]\s*([^)]{1,20}?)\s*\)")
_SEPARATE_STMT = ("재무상태표", "손익계산서", "현금흐름표", "자본변동표", "대차대조표")
_CLOSE_TAG = re.compile(r"</\s*document\s*>", re.IGNORECASE)
_WS = re.compile(r"\s+")

UNIT_SCAN = 400        # 본문 앞 몇 자에서 단위를 찾을지
MAX_ERAS = 6           # 주석에 나열할 기수 상한
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


def _source_label(src: dict) -> str:
    d = str(src.get("rcept_dt") or "")
    dt = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d
    return (f"[{src['corp_name']} {src['report_nm']}, {dt}, "
            f"접수번호 {src['rcept_no']}]")


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


def annotate(res: dict):
    """자동 주석 한 줄. 기수 표기가 없거나 당기 연도를 모르면 None (예외 없음)."""
    src = res.get("source") or {}
    text = res.get("text") or ""
    section = _section(src)

    eras = sorted({int(m) for m in _ERA.findall(text)}, reverse=True)
    if not eras:
        return None
    try:
        fy = int(src.get("fiscal_year") or -1)
    except (TypeError, ValueError):
        fy = -1
    if fy <= 0:
        return None            # 접수 연도로 때우지 않는다

    cur = eras[0]              # 최대 기수 = 당기 (결산월 12월 가정)
    years = ", ".join(f"제{e}기={fy - (cur - e)}년" for e in eras[:MAX_ERAS])

    parts = [years]
    unit = _unit(section, text)
    if unit:
        parts.append(f"단위 {unit}")
    basis = _basis(section)
    if basis:
        parts.append(basis)
    if res.get("preserved"):
        parts.append("정정 전 첨부본")
    return "※ 자동 주석(공시 원문 아님): " + " · ".join(parts)


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
        note = annotate(res)
        blocks.append((note + "\n" if note else "") + head + "\n" + body + "\n</document>")
    return "\n\n".join(blocks)
