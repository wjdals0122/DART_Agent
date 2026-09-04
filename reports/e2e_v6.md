# e2e 평가 — e2e_v6

실행 2026-09-04T22:08:00 · 문항 40건

## 토큰·비용

| 구분 | 토큰 | 단가(1K) | 추정 비용 |
|---|---|---|---|
| 입력 | 1,219,078 | 1.25원 | 1,524원 |
| 출력 | 31,431 | 5.0원 | 157원 |
| **합계** | **1,250,509** | | **1,681원** |

- LLM 호출 170회 · 문항당 평균 입력 30,477 + 출력 786 토큰 (42원/문항)
- 상한 3,500원 · 남은 예산 1,819원
- 실행 전 예상 3,127원 → 실제 1,681원 (54%)

HCX-005 단가: 입력 1,000토큰 1.25원 · 출력 1,000토큰 5.0원 (출력이 4배 비싸다)

## 비용이 큰 문항 상위 5

| 문항 | 유형 | 입력 | 출력 | 추정 비용 | 도구 | LLM |
|---|---|---|---|---|---|---|
| 두산로보틱스__multi_closed | multi_closed | 78,854 | 1,061 | 104원 | 4 | 5 |
| 삼성생명__multi_open | multi_open | 50,197 | 1,305 | 69원 | 4 | 6 |
| HMM__multi_closed | multi_closed | 44,261 | 614 | 58원 | 2 | 5 |
| OCI홀딩스__multi_closed | multi_closed | 41,345 | 721 | 55원 | 2 | 5 |
| 미래에셋증권__complex_open | complex_open | 41,446 | 650 | 55원 | 3 | 4 |

## 낭비 신호

| 항목 | 값 | 무슨 뜻인가 |
|---|---|---|
| 재호출 차단 | 2 | 같은 인자로 또 불렀다. 많으면 모델이 검색어를 안 바꾸는 것 |
| error/not_found 뒤 재시도 | 4 | 스스로 고쳐 다시 불렀다. 많으면 첫 시도의 검색어·필터가 나쁘다 |
| 목록만 보고 답하려다 본문 검색으로 복귀 | 2 | 내용 질문에서 list_disclosures 만 보고 답하려 한 횟수 |
| 도구 6회 한도 도달 문항 | 0/40 | 많으면 계획이 헤맨다 |
| 429 재시도 | 11 | 대기 106초. 요청이 거절된 것이라 낭비 토큰은 **측정 불가** |
| 전체 HTTP 재시도 | 11 | 429 + 5xx + 네트워크 |
| 다시 보낸 프롬프트 토큰(추정) | 153,929 | 재시도가 같은 본문을 다시 보냈다. 서버가 과금했는지는 알 수 없다 |

## 유형별

| 유형 | 문항 | 평균 입력 | 평균 출력 | 평균 비용 | 평균 도구 | 평균 초 | 근거 0건 |
|---|---|---|---|---|---|---|---|
| complex_closed | 7 | 26,798 | 780 | 37원 | 1.9 | 39 | 2 |
| complex_open | 7 | 32,679 | 733 | 45원 | 2.7 | 29 | 0 |
| multi_closed | 7 | 40,435 | 624 | 54원 | 2.0 | 36 | 0 |
| multi_open | 7 | 32,850 | 1,276 | 47원 | 1.6 | 50 | 1 |
| search_closed | 6 | 21,769 | 342 | 29원 | 1.3 | 15 | 0 |
| search_open | 6 | 26,520 | 915 | 38원 | 1.5 | 32 | 0 |

## 근거를 못 올린 문항

| 통 | 문항 | 뜻 |
|---|---|---|
| 검색 | 1 | 도구는 불렀는데 근거가 하나도 안 올라왔다 |
| 계획 | 2 | 조회를 안 했거나 도구를 헤맸다(한도·전부 실패) |
| 보류(사람) | 37 | 근거는 올렸다. 맞는지는 사람이 본다 |

**자동 채점은 폐기했다.** 정답에 '오답 유형' 반례 숫자가 섞여 있어 숫자 일치로는
맞고 틀림을 가릴 수 없다. 판정은 `reports/e2e_v6_review.md` 에서 한다.

## 유형별 (기계로 잰 것만)

| 유형 | 문항 | 근거 확보 | 근거 확보율 | 평균 근거 | 평균 도구 | 평균 비용 |
|---|---|---|---|---|---|---|
| complex_closed | 7 | 5 | 71% | 1.3 | 1.9 | 37원 |
| complex_open | 7 | 7 | 100% | 6.6 | 2.7 | 45원 |
| multi_closed | 7 | 7 | 100% | 2.4 | 2.0 | 54원 |
| multi_open | 7 | 6 | 86% | 3.6 | 1.6 | 47원 |
| search_closed | 6 | 6 | 100% | 1.3 | 1.3 | 29원 |
| search_open | 6 | 6 | 100% | 6.2 | 1.5 | 38원 |

전체 근거 확보율 **37/40 (92%)**

## 도구별 성적

| 도구 | 호출 | ok | not_found | error |
|---|---|---|---|---|
| calculate | 3 | 3 | 0 | 0 |
| get_progress_table | 4 | 4 | 0 | 0 |
| list_disclosures | 7 | 5 | 1 | 1 |
| search_disclosure | 60 | 52 | 3 | 5 |

## 수정 효과 집계

| 항목 | 값 |
|---|---|
| 도구 0회로 답한 문항 | **2**/40 ['OCI홀딩스__complex_closed', '두산퓨얼셀__complex_closed'] |
| 조회하라고 되돌린 문항(FORCE_TOOL) | 3 |
| 목록에서 본문 검색으로 되돌린 문항 | 2 |
| 타사 데이터를 근거로 단 문항 | **0**  |
| 항목별 거절을 밝힌 문항 | 14 (총 23회) |
| 근거 0건인데 단정한 문항(unsourced_claim) | **2** ['OCI홀딩스__complex_closed', '두산퓨얼셀__complex_closed'] |

## 판정 (사람이 채운다)

`reports/e2e_v6_review.md` 에 질문·정답·답변이 나란히 있다.
이 표는 그 파일을 다 채운 뒤 옮겨 적는 자리다.

| 문항 | 유형 | 근거 | 도구 | 정답 | 출처 | 환각 |
|---|---|---|---|---|---|---|
| OCI홀딩스__complex_closed | complex_closed | 0 | 0 | | | |
| 디앤디파마텍__complex_open | complex_open | 7 | 2 | | | |
| 삼성화재해상보험__multi_closed | multi_closed | 2 | 1 | | | |
| 레인보우로보틱스__multi_open | multi_open | 0 | 2 | | | |
| 삼성SDI__search_closed | search_closed | 1 | 1 | | | |
| 삼성바이오로직스__search_open | search_open | 7 | 1 | | | |
| 두산로보틱스__complex_closed | complex_closed | 1 | 1 | | | |
| SK텔레콤__complex_open | complex_open | 10 | 3 | | | |
| 두산로보틱스__multi_closed | multi_closed | 1 | 4 | | | |
| HMM__multi_open | multi_open | 1 | 1 | | | |
| SK하이닉스__search_closed | search_closed | 1 | 1 | | | |
| LIG디펜스앤에어로스페이스__search_open | search_open | 5 | 1 | | | |
| 미래에셋증권__complex_closed | complex_closed | 5 | 3 | | | |
| 시프트업__complex_open | complex_open | 6 | 2 | | | |
| LIG디펜스앤에어로스페이스__multi_closed | multi_closed | 3 | 1 | | | |
| 시프트업__multi_open | multi_open | 5 | 1 | | | |
| LG유플러스__search_closed | search_closed | 1 | 2 | | | |
| LG에너지솔루션__search_open | search_open | 8 | 2 | | | |
| HD현대일렉트릭__complex_closed | complex_closed | 1 | 5 | | | |
| 미래에셋증권__complex_open | complex_open | 8 | 3 | | | |
| LG이노텍__multi_closed | multi_closed | 1 | 2 | | | |
| 삼성SDI__multi_open | multi_open | 3 | 1 | | | |
| OCI홀딩스__search_closed | search_closed | 1 | 2 | | | |
| POSCO홀딩스__search_open | search_open | 9 | 3 | | | |
| LG유플러스__complex_closed | complex_closed | 1 | 2 | | | |
| 삼성E&A__complex_open | complex_open | 6 | 3 | | | |
| OCI홀딩스__multi_closed | multi_closed | 3 | 2 | | | |
| 삼성생명__multi_open | multi_open | 5 | 4 | | | |
| 삼성바이오로직스__search_closed | search_closed | 1 | 1 | | | |
| 대우건설__search_open | search_open | 4 | 1 | | | |
| 두산퓨얼셀__complex_closed | complex_closed | 0 | 0 | | | |
| LG씨엔에스__complex_open | complex_open | 5 | 3 | | | |
| NC__multi_closed | multi_closed | 3 | 2 | | | |
| 두산에너빌리티__multi_open | multi_open | 5 | 1 | | | |
| 두산에너빌리티__search_closed | search_closed | 3 | 1 | | | |
| OCI홀딩스__search_open | search_open | 4 | 1 | | | |
| HD현대중공업__complex_closed | complex_closed | 1 | 2 | | | |
| POSCO홀딩스__complex_open | complex_open | 4 | 3 | | | |
| HMM__multi_closed | multi_closed | 4 | 2 | | | |
| LG생활건강__multi_open | multi_open | 6 | 1 | | | |
