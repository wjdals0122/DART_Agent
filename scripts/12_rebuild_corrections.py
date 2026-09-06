# -*- coding: utf-8 -*-
"""corpus/corrections/ 5개 파일을 원문에서 다시 만든다 (재현용).

왜 이 스크립트가 필요한가
─────────────────────────
DART 가 XML(document.xml)을 안 주는 문서가 3건 있다(OpenAPI status 014,
manifest 의 file_format == "pdf+html").

    KB금융              20260619000667   [기재정정]사업보고서 (2025.12)
    한화오션            20240514001522   [기재정정]분기보고서 (2024.03)
    한화에어로스페이스  20260513000860   분기보고서 (2026.03)

상시 파이프라인(scripts/03_build_docjson.py)은 corpus/raw/ 에서 XML 을 찾는데
이 3건은 XML 이 없어 no_xml 로 빠진다. 그래서 이 3건의 마크다운은 별도 경로로
만들어 corpus/corrections/ 에 둔다. 그 별도 경로를 리포 안에서 다시 돌릴 수
있게 한 것이 이 파일이다.

기존 pdf_parser/build_viewer_html.py · pdf_parser/correction_notice_KB_Hanhwa.py
는 작성자 개인 바탕화면 경로가 하드코딩돼 있어 이 리포에서 돌지 않는다.
그 두 스크립트를 고치지 않고, 같은 일을 리포 상대경로로 다시 엮었다.

무엇이 진짜 재현이고 무엇이 아닌가 (중요)
──────────────────────────────────────────
  (1) 한화에어로스페이스 1.27MB 본문
      → **원문에서 완전 재현된다.** corpus/raw/ 의 뷰어 HTML 을
        viewer_html_parser + periodic_parser 로 다시 돌려 SHA-256 이 같다.

  (2) KB금융 · 한화오션 정정사항 델타 + 마커본
      → **부분 재현이다.** 정정 내용(어느 줄, 무슨 사유, 정정후 값)은 사람이
        정정공시를 읽고 옮겨 적은 표이고, 아래 KB_FIXES / HW_FIXES 상수가
        그 표다. 스크립트는 그 표를 원본 md 에 얹어 두 파일을 만든다.
        정정신고 서식이 회사마다 달라 자동 파싱은 하지 않았다 —
        원래 스크립트의 판단을 그대로 따랐다.

  (3) 한화오션 정정 #1 의 정정후 본문(HW_AFTER1)
      → **재유도할 수 없다.** 원래는 20240514001522.pdf 를 docling 으로 돌린
        중간 산출물(output_fixed.md)에서 잘라 왔는데, 그 중간 파일이 리포에
        없고 docling 도 의존성에 없다. 값 자체는 배포본과 같지만 PDF 에서
        다시 뽑은 것이 아니라 **전사(轉寫)한 상수**다. 이 한 덩어리만
        원문→산출물 사슬이 끊겨 있다.

PDF 에서 글자를 뽑는 코드는 이 리포에 없다. 3건 중 2건(KB·한화오션)은 정정
대상 원본에 XML 이 있어 본문을 PDF 에서 뽑을 이유가 없었고, 1건(한화에어로)은
뷰어 HTML 전문이 있어 그쪽이 더 정확했다(pdf_parser/README_viewer_html.md).

쓰는 법
───────
    python scripts/12_rebuild_corrections.py
        → data/verify/corrections_rebuilt/ 에 5개를 만들고 배포본과 대조한다.
          corpus/ 는 안 건드린다. 하나라도 어긋나면 종료코드 1.

    python scripts/12_rebuild_corrections.py --write
        → 대조 후 corpus/corrections/ 에 덮어쓴다.

대조 기준이 파일마다 다르다
───────────────────────────
델타 2개 + 한화에어로 본문 1개는 배포본과 **SHA-256 이 같아야** 한다.

마커본 2개는 다르다. 배포본 마커본은 구버전 파서 산출물(data_processed_v4)
위에 만들어졌는데, 그 뒤 파서에 ISO 날짜 병기가 들어갔다
("2008.10.10" → "2008.10.10 (2008-10-10)"). 그래서 지금 파서로 다시 만들면
본문이 KB 242줄 / 한화오션 88줄 달라진다 — 전부 이 한 가지 이유이고, 확인해
보면 ISO 꼬리표만 떼면 배포본과 글자 단위로 같아진다. 배포본 바이트를
맞추자고 폐기된 파서를 얼려 둘 이유가 없어서, 마커본은 SHA 대신
① 마커 줄 번호·문구가 배포본과 같은가 ② 마커를 빼면 현재 md 와 같은가
③ 배포본과 남은 차이가 ISO 날짜 병기뿐인가 — 셋을 본다(_check_marked).
"""
import argparse
import glob
import hashlib
import io
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline_paths as P                                  # noqa: E402

# 뷰어 HTML 경로의 부품들. pdf_parser/ 가 먼저여야 periodic_parser 가
# 그쪽(뷰어 HTML 을 위해 쓰인 사본)으로 잡힌다. rag_pipeline 이 상단에서
# 파서 4종을 전부 import 하는데 pdf_parser/ 에는 periodic 하나뿐이라
# 나머지 3종은 parser/ 에서 가져온다 (pdf_parser/README_viewer_html.md).
sys.path.insert(0, os.path.join(P.REPO_ROOT, 'pdf_parser'))
sys.path.append(P.PARSER_DIR)

CORRECTIONS_DIR = os.path.join(P.CORPUS_DIR, 'corrections')
MD_DIR = os.path.join(P.DATA_DIR, 'verify', 'md')
OUT_DIR_DEFAULT = os.path.join(P.DATA_DIR, 'verify', 'corrections_rebuilt')


# ══════════════════════════════════════════════════════════════════════
# 0. 파일 입출력 — 디스크가 NFD 로 저장돼 있을 수 있다
# ══════════════════════════════════════════════════════════════════════

def read_text(path):
    real = P.on_disk(path)
    if real is None:
        raise SystemExit('입력 파일이 없다: %s' % path)
    with io.open(real, encoding='utf-8') as fh:
        return fh.read()


def read_lines(path):
    return read_text(path).split('\n')


def write_text(path, text):
    with io.open(path, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text)


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# ══════════════════════════════════════════════════════════════════════
# 1. 한화에어로스페이스 — 뷰어 HTML 전문 → 마크다운 (원문에서 완전 재현)
# ══════════════════════════════════════════════════════════════════════

AERO_RCEPT = '20260513000860'
AERO_SRC = os.path.join(P.RAW_ROOT, 'periodic', '한화에어로스페이스',
                        AERO_RCEPT + '_quarter_2026_03',
                        AERO_RCEPT + '_viewer.html')
AERO_OUT = '한화에어로스페이스_periodic_%s.md' % AERO_RCEPT


def manifest_row(rcept_no):
    import json
    for line in read_lines(P.MANIFEST_PATH):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get('rcept_no') == rcept_no:
            return row
    raise SystemExit('manifest 에 %s 가 없다' % rcept_no)


def sibling_aliases(corp_name):
    """같은 회사의 기존 periodic md 가 쓰는 동의어 줄을 그대로 따른다.

    동의어에는 manifest 만으로는 알 수 없는 변형('한화에어로스페이스(주)')이
    섞여 있다. CompanyAliasRegistry 가 raw 트리 전체를 훑어 모은 것인데
    한 건 만들자고 1,054개 폴더를 다시 훑을 이유가 없다. 이미 같은 회사
    파일 19개가 합의된 목록을 갖고 있으니 그걸 쓴다.
    """
    found = []
    for path in sorted(glob.glob(os.path.join(MD_DIR, '*periodic*.md'))):
        base = unicodedata.normalize('NFC', os.path.basename(path))
        if not base.startswith('%s_periodic_' % corp_name):
            continue
        for line in read_lines(path):
            if line.startswith('> 동의어/검색어:'):
                found.append([a.strip() for a in line.split(':', 1)[1].split(',')])
                break
    if not found:
        raise SystemExit('%s 의 기존 periodic md 를 %s 에서 못 찾았다'
                         % (corp_name, MD_DIR))
    return tuple(max(found, key=len))     # 가장 긴 목록을 기준으로 삼는다


def build_aero():
    import periodic_parser as pp
    import rag_pipeline
    import viewer_html_parser as vh

    src = read_text(AERO_SRC)
    if not vh.looks_like_viewer_html(src):
        raise SystemExit('본문이 없는 껍데기 뷰어 HTML 이다: %s' % AERO_SRC)

    meta = manifest_row(AERO_RCEPT)
    doc = vh.parse_viewer_html(src, AERO_RCEPT, meta['corp_name'])
    body = pp.to_markdown(doc, with_header=False)
    extra = sibling_aliases(meta['corp_name'])
    header = rag_pipeline.build_rag_header(meta, extra)
    return AERO_OUT, header + '\n\n' + body


# ══════════════════════════════════════════════════════════════════════
# 2. 정정사항 델타 + 마커본 — 사람이 옮겨 적은 정정 표를 원본 md 에 얹는다
# ══════════════════════════════════════════════════════════════════════

def render_delta(head, fixes, orig_name, tail=None):
    """정정된 부분만 담은 작은 파일."""
    out = list(head) + ['']
    for f in fixes:
        out += ['---', '', '## 정정 #%d' % f['no'], '',
                '| 항목 | 값 |', '|---|---|',
                '| 위치 | `%s` %d줄 |' % (orig_name, f['line']),
                '| 경로 | %s |' % f['path'].replace('|', '\\|'),
                '| 사유 | %s |' % f['reason']]
        if f.get('diff'):
            out.append('| 변경 | %s |' % f['diff'].replace('|', '\\|'))
        if f.get('anchor'):
            out.append('| 앵커 | `%s...` |' % f['anchor'].replace('|', '\\|'))
        out += ['', '### 정정후 내용 (유효)', '']
        if f.get('title'):
            out += [f['title'], '']
        if f.get('unit'):
            out += [f['unit'], '']
        out += list(f['after']) + ['']
    if tail:
        out += list(tail)
    return '\n'.join(out)


def render_marked(orig_lines, fixes, rcept, patch_name):
    """원본 사본 + 정정 위치 주석 마커. 원본 파일은 건드리지 않는다.

    마커는 줄 번호가 작은 것부터 넣는다. 그래서 k 번째 마커가 실제로 앉는
    1-based 줄 번호는 (원본 줄 번호 + 이미 넣은 마커 수)가 된다 — 배포본의
    KB 마커가 6377 / 23868 / 29398 줄에 있는 이유다(원본 6377 / 23867 /
    29396).
    """
    out = list(orig_lines)
    for k, f in enumerate(sorted(fixes, key=lambda x: x['line'])):
        bits = ['<!-- ⚠ 정정있음 #%d' % f['no'], '%s 로 정정됨' % rcept]
        if f.get('diff'):
            bits.append(f['diff'])
        bits.append('정정후 내용: %s -->' % patch_name)
        out.insert(f['line'] - 1 + k, ' | '.join(bits))
    return '\n'.join(out)


# ─── KB금융 ───────────────────────────────────────────────────────────
# 20260619000667 [기재정정]사업보고서 → 원본 20260313001191 의 3곳
# 값은 정정신고서를 사람이 읽고 옮겨 적은 것이다(정정신고 서식이 회사마다
# 달라 자동 파싱하지 않았다). 줄 번호는 원본 md 기준.

KB_ORIG_NAME = 'KB금융_periodic_20260313001191.md'
KB_PATCH_NAME = 'KB금융_정정사항_20260619000667.md'
KB_MARKED_NAME = 'KB금융_periodic_20260313001191_마커.md'

KB_HEAD = [
    '# [KB금융] 제18기 사업보고서 (2025.12) 정정사항',
    '',
    '> 표준사명: KB금융 | 종목코드: 105560 | 업종: 금융  ',
    '> 정정공시: 20260619000667 (2026-06-19 [기재정정])  ',
    '> 정정대상: 20260313001191 (2026-03-13 최초제출)  ',
    '> 이 파일은 **정정된 부분만** 담습니다. 나머지 내용은 원본 문서를 참조하세요.  ',
    '> 아래 표가 해당 부분의 **유효한 최신 값**이며, 원본의 같은 위치 값은 폐기되었습니다.',
]

KB_BIS_AFTER = [
    '| 구분 | 2025년 | 2024년 | 2023년 |',
    '| --- | --- | --- | --- |',
    '| 자기자본(a=b+e) | 57,846 | 56,849 | 53,744 |',
    '| 기본자본(b=c+d) | 54,292 | 52,477 | 49,391 |',
    '| 보통주자본(c) | 49,353 | 46,794 | 43,664 |',
    '| 기타기본자본(d) | 4,939 | 5,683 | 5,727 |',
    '| 보완자본(e) | 3,553 | 4,372 | 4,353 |',
    '| 위험가중자산(f) | 356,996 | 345,981 | 321,319 |',
    '| 총자본비율(a/f) | 16.20 | 16.43 | 16.73 |',
    '| 기본자본비율(b/f) | 15.21 | 15.17 | 15.37 |',
    '| 보통주자본비율(c/f) | 13.82 | 13.53 | 13.59 |',
]

KB_TREND_AFTER = [
    '| 관련지표 | 2025년 1분기 | 2025년 2분기 | 2025년 3분기 | 2025년 4분기 |',
    '| --- | --- | --- | --- | --- |',
    '| 총자본합계 | 57,668 | 57,969 | 58,269 | 57,846 |',
    '| 기본자본계 | 53,731 | 54,398 | 54,868 | 54,292 |',
    '| 보통주자본 | 47,661 | 48,668 | 49,512 | 49,353 |',
    '| 위험가중자산 | 347,855 | 353,481 | 358,042 | 356,996 |',
    '| 총자본비율 | 16.58 | 16.40 | 16.27 | 16.20 |',
    '| 기본자본비율 | 15.45 | 15.39 | 15.32 | 15.21 |',
    '| 보통주자본비율 | 13.70 | 13.77 | 13.83 | 13.82 |',
    '| 고정이하여신비율 | 1.15 | 1.09 | 1.08 | 0.99 |',
    '| 고정이하 여신에 대한 대손충당금 적립률 | 108.17 | 111.34 | 109.20 | 115.45 |',
]

_KB_BIS_DIFF = '자기자본(a=b+e) 57,824 → 57,846 · 보완자본(e) 3,532 → 3,553'
_KB_BIS_REASON = '2025년 자기자본, 보완자본 오류기재'

KB_FIXES = [
    dict(no=1, line=6377,
         title='1) BIS기준 자기자본비율',
         unit='(단위: 십억원, %, %p)',
         path=('II. 사업의 내용 > 5. 재무건전성 등 기타 참고사항 '
               '> 가. 그 밖에 투자의사결정에 필요한 사항 '
               '> [지배회사에 관한 사항][KB금융지주] > (1) 자본적정성 관련 지표 '
               '> 1) BIS기준 자기자본비율'),
         reason=_KB_BIS_REASON, diff=_KB_BIS_DIFF, after=KB_BIS_AFTER),
    dict(no=2, line=23867,
         title='* 그룹 연결자기자본비율',
         unit='(단위: 십억원, %)',
         path=('IV. 이사의 경영진단 및 분석의견 > 3. 재무상태 및 영업실적 '
               '> 가. 재무상태 및 영업실적 > (4) 자본적정성 > * 그룹 연결자기자본비율'),
         reason=_KB_BIS_REASON, diff=_KB_BIS_DIFF, after=KB_BIS_AFTER),
    dict(no=3, line=29396,
         title='1) 자본적정성 및 자산건전성 지표 추이',
         unit='(단위 : 십억원,%)',
         path=('XI. 그 밖에 투자자 보호를 위하여 필요한 사항 '
               '> 4. 작성기준일 이후 발생한 주요사항 등 기타사항 '
               '> 사. 조건부자본증권의 전환, 채무재조정 사유 등의 변동현황 '
               '> (2) 관련 지표의 변동현황 > 1) 자본적정성 및 자산건전성 지표 추이'),
         reason='2025년 4분기 총자본합계 오류기재',
         diff='총자본합계 2025년 4분기 57,824 → 57,846',
         after=KB_TREND_AFTER),
]


# ─── 한화오션 ─────────────────────────────────────────────────────────
# 20240514001522 [기재정정]분기보고서 → 원본 20240513000844 의 2곳
# + 본문 밖 XBRL 공시기본정보 4건

HW_ORIG_NAME = '한화오션_periodic_20240513000844.md'
HW_PATCH_NAME = '한화오션_정정사항_20240514001522.md'
HW_MARKED_NAME = '한화오션_periodic_20240513000844_마커.md'

HW_HEAD = [
    '# [한화오션] 제25기 분기보고서 (2024.03) 정정사항',
    '',
    '> 정정공시: 20240514001522 (2024-05-14 [기재정정])  ',
    '> 정정대상: 20240513000844 (2024-05-13 최초제출)  ',
    '> 이 파일은 **정정된 부분만** 담습니다. 나머지 내용은 원본 문서를 참조하세요.  ',
    '> 아래 내용이 해당 부분의 **유효한 최신 값**이며, 원본의 같은 위치 내용은 폐기되었습니다.',
]

# ⚠ 원문에서 다시 뽑을 수 없는 유일한 덩어리.
#   원래는 20240514001522.pdf → docling → output_fixed.md 에서 '(주2) 정정 후'
#   블록을 잘라 왔고, 어절 중간에 낀 공백만 되돌렸다("있습 니다"→"있습니다" 등).
#   그 중간 파일이 리포에 없고 docling 도 의존성에 없어 여기서는 전사한
#   상수로 둔다. 아래 텍스트에 남은 어절 중간 공백("고 려하여", "습 니다")은
#   PDF 좌표 추출의 흔적이며 배포본과 글자 단위로 같다 — 고치면 안 된다.
HW_AFTER1 = [
    '당사는 미래에 대하여 추정 및 가정을 하고 있습니다. 추정 및 가정은 지속적으로 평가되며, 과거 경험과 현재의 상황에서 합리적으로 예측가능한 미래의 사건과 같은 다른 요소들을 고 려하여 이루어집니다. 이러한 회계추정은 실제 결과와 다를 수도 있습니다.',
    '요약분기재무제표 작성시 사용된 중요한 회계추정 및 가정은 법인세비용을 결정하는데 사용 된 추정의 방법을 제외하고는 전기 재무제표 작성시 적용된 회계추정 및 가정과 동일합니다.',
    '- 러시아 제재 영향',
    '2022년 2월 발생한 러시아-우크라이나 간 분쟁으로 인하여 미국, EU 등의 국가에서 러시아 에 대한 국제 제재가 이루어졌으며, 2022년 3월 1일 대한민국 정부도 국제 제재에 동참하였 습니다. 이로 인해, 러시아 기업은 유로나 달러와 같은 통화로 표시된 미수금을 지불하기 위 한 외화에 대한 접근이 제한될 수 있으며, 외환 시장의 유동성 부족과 루블 가치의 현저한 하 락으로 해당 채권에 대한 신용 위험이 증가할 수 있습니다. 또한, 러시아 은행의 SWIFT 시스 템에 대한 액세스 제한으로 고객이 미수금을 결제하는 능력을 제한하여 당사의 대금의 수령 시기에 영향을 미칠 수 있습니다.',
    '당분기말 현재 당사가 체결하고 있는 선박 계약 및 약정 중 일부가 관련 국가의 기업 또는 은 행과 관계가 있으며, 이러한 제재의 영향이 재무상태 및 영업성과에 미칠 영향의 범위를 예 측하는 것은 현재 시점에는 매우 불확실하며, 당사의 재무제표는 이로 인한 영향이 반영되지 아니하였습니다. 한편 동 계약 중 선주의 건조대금 지급이 기한 내 이루어지지 않은 계약에 대하여는 계약해지를 통지 하였으며 이에 따라 당분기말 현재 재고자산으로 인식하고 있습 니다',
]

HW_AFTER2 = ['(주1) 부여일자는 계약서상 기재된 양도제한조건부주식 부여일임']

# 정정 위치는 줄 번호를 박아 두지 않고 원본 md 에서 앵커 문장을 찾아 잡는다.
# 원본이 다시 파싱돼 줄이 밀려도 따라간다.
HW_ANCHOR1 = '당사는 미래에 대하여 추정 및 가정을 하고 있습니다'
HW_ANCHOR1_AFTER_LINE = 3700          # 같은 문장이 앞에도 나온다 — 뒤엣것을 쓴다
HW_ANCHOR2 = '(주1) 부여일자는 양도제한조건부주식'

HW_XBRL_TAIL = [
    '---', '',
    '## 정정 #3~6 — XBRL 공시기본정보 (본문 외)', '',
    '보고서 본문에는 존재하지 않는 항목입니다. DART 제출 시 별도 입력하는',
    "공시기본정보(XBRL)의 값으로, 분기보고서는 '감사'가 아니라 '검토' 대상인데",
    '이를 잘못 기재한 것을 정정한 것입니다.', '',
    '| # | 구분 | 항목 | 정정 전 | 정정 후 |',
    '|---|---|---|---|---|',
    '| 3 | 개별 | 감사구분 | 감사 | 검토 |',
    '| 4 | 개별 | 감사의견 | 적정의견 | 예외사항없음 |',
    '| 5 | 연결 | 감사구분 | 감사 | 검토 |',
    '| 6 | 연결 | 감사의견 | 적정의견 | 예외사항없음 |',
]


def _find_line(lines, prefix, after=0):
    for i, line in enumerate(lines):
        if i > after and line.startswith(prefix):
            return i
    raise SystemExit('원본 md 에서 앵커를 못 찾았다: %r' % prefix)


def hw_fixes(orig_lines):
    i1 = _find_line(orig_lines, HW_ANCHOR1, after=HW_ANCHOR1_AFTER_LINE)
    i2 = _find_line(orig_lines, HW_ANCHOR2)
    return [
        dict(no=1, line=i1 + 1,
             path='III. 재무에 관한 사항 > 5. 재무제표 주석 > 3. 중요한 회계추정 및 가정',
             reason='단순기재오류', anchor=orig_lines[i1][:40], after=HW_AFTER1),
        dict(no=2, line=i2 + 1,
             path=('X. 대주주 등과의 거래내용 > 4. 대주주에 대한 주식기준보상 거래 '
                   '> 라. 부여/지급 현황'),
             reason='단순기재오류', anchor=orig_lines[i2][:40], after=HW_AFTER2),
    ]


# ══════════════════════════════════════════════════════════════════════
# 3. 실행
# ══════════════════════════════════════════════════════════════════════

def build_all():
    """{파일명: 본문} 5개."""
    out = {}

    name, text = build_aero()
    out[name] = text

    kb_lines = read_lines(os.path.join(MD_DIR, KB_ORIG_NAME))
    out[KB_PATCH_NAME] = render_delta(KB_HEAD, KB_FIXES, KB_ORIG_NAME)
    out[KB_MARKED_NAME] = render_marked(kb_lines, KB_FIXES,
                                        '20260619000667', KB_PATCH_NAME)

    hw_lines = read_lines(os.path.join(MD_DIR, HW_ORIG_NAME))
    fixes = hw_fixes(hw_lines)
    out[HW_PATCH_NAME] = render_delta(HW_HEAD, fixes, HW_ORIG_NAME,
                                      tail=HW_XBRL_TAIL)
    out[HW_MARKED_NAME] = render_marked(hw_lines, fixes,
                                        '20240514001522', HW_PATCH_NAME)
    return out


# 배포본 마커본 2개는 **구버전 파서 산출물** 위에 만들어졌다. 그 뒤 파서에
# ISO 날짜 병기가 들어가면서("2008.10.10" → "2008.10.10 (2008-10-10)")
# 본문이 달라졌다 — KB 242줄 / 한화오션 88줄, 전부 이 한 가지 이유다.
# 그래서 마커본은 SHA 대조 대신 아래 세 가지를 본다.
#   ① 마커가 앉은 줄 번호와 마커 문구가 배포본과 같은가
#   ② 마커를 빼면 현재 파서의 md 와 정확히 같은가
#   ③ 배포본과 남은 차이가 ISO 날짜 병기뿐인가
_ISO_SUFFIX = re.compile(r' \(\d{4}-\d{2}-\d{2}\)')

EXACT = 'exact'      # SHA-256 이 배포본과 같아야 한다
MARKED = 'marked'    # 위 ①②③ 을 본다

CHECK_MODE = {
    AERO_OUT: EXACT,
    KB_PATCH_NAME: EXACT,
    HW_PATCH_NAME: EXACT,
    KB_MARKED_NAME: MARKED,
    HW_MARKED_NAME: MARKED,
}

MARKED_BASE = {
    KB_MARKED_NAME: KB_ORIG_NAME,
    HW_MARKED_NAME: HW_ORIG_NAME,
}


def _check_marked(name, text):
    """마커본 검증. (통과여부, 한 줄 설명)"""
    shipped_path = P.on_disk(os.path.join(CORRECTIONS_DIR, name))
    if shipped_path is None:
        return False, '배포본 없음'
    rebuilt = text.split('\n')
    shipped = read_text(shipped_path).split('\n')
    base = read_lines(os.path.join(MD_DIR, MARKED_BASE[name]))

    r_mark = [(i, l) for i, l in enumerate(rebuilt) if '정정있음' in l]
    s_mark = [(i, l) for i, l in enumerate(shipped) if '정정있음' in l]
    if r_mark != s_mark:
        return False, '마커 위치/문구가 배포본과 다르다'

    r_body = [l for l in rebuilt if '정정있음' not in l]
    if r_body != base:
        return False, '마커를 빼도 현재 md 와 다르다'

    s_body = [l for l in shipped if '정정있음' not in l]
    if len(r_body) != len(s_body):
        return False, '배포본과 줄 수가 다르다 (%d ≠ %d)' % (len(r_body), len(s_body))
    drift = [(x, y) for x, y in zip(r_body, s_body) if x != y]
    other = [1 for x, y in drift if _ISO_SUFFIX.sub('', x) != y]
    if other:
        return False, ('배포본과 %d줄 다르고 그중 %d줄은 ISO 날짜 병기로 설명 안 된다'
                       % (len(drift), len(other)))
    return True, ('마커 %d개 일치 · 배포본과 %d줄 차이는 전부 ISO 날짜 병기'
                  % (len(r_mark), len(drift)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='corpus/corrections/ 5개 파일 재생성 + 배포본 대조')
    ap.add_argument('--out', default=OUT_DIR_DEFAULT,
                    help='재생성본을 쓸 디렉터리 (기본 data/verify/corrections_rebuilt)')
    ap.add_argument('--write', action='store_true',
                    help='대조 후 corpus/corrections/ 에도 덮어쓴다 '
                         '(마커본 2개는 현재 파서 산출물로 바뀐다)')
    a = ap.parse_args(argv)

    P.ensure_dirs(a.out)
    built = build_all()

    print('재생성 %d개 → %s' % (len(built), a.out))
    ok = True
    for name in sorted(built):
        text = built[name]
        write_text(os.path.join(a.out, name), text)
        size = format(len(text.encode('utf-8')), ',')

        if CHECK_MODE[name] == EXACT:
            shipped_path = P.on_disk(os.path.join(CORRECTIONS_DIR, name))
            if shipped_path is None:
                good, note = False, '배포본 없음'
            else:
                good = sha(text) == sha(read_text(shipped_path))
                note = 'SHA-256 일치' if good else 'SHA-256 불일치'
        else:
            good, note = _check_marked(name, text)

        print('  %s %-46s %11s bytes  %s'
              % ('OK  ' if good else 'FAIL', name, size, note))
        ok = ok and good

    print()
    if not ok:
        print('검증 실패. corpus/corrections/ 는 건드리지 않았다.')
        return 1

    print('5개 모두 검증 통과.')
    if a.write:
        for name, text in built.items():
            write_text(P.on_disk(os.path.join(CORRECTIONS_DIR, name))
                       or os.path.join(CORRECTIONS_DIR, name), text)
        print('corpus/corrections/ 에 덮어썼다.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
