# e2e 평가 — e2e_v5

실행 2026-09-04T18:37:02 · 문항 40건

## 토큰·비용

| 구분 | 토큰 | 단가(1K) | 추정 비용 |
|---|---|---|---|
| 입력 | 888,056 | 1.25원 | 1,110원 |
| 출력 | 25,862 | 5.0원 | 129원 |
| **합계** | **913,918** | | **1,239원** |

- LLM 호출 148회 · 문항당 평균 입력 22,201 + 출력 647 토큰 (31원/문항)
- 상한 3,000원 · 남은 예산 1,761원
- 실행 전 예상 1,888원 → 실제 1,239원 (66%)

HCX-005 단가: 입력 1,000토큰 1.25원 · 출력 1,000토큰 5.0원 (출력이 4배 비싸다)

## 비용이 큰 문항 상위 5

| 문항 | 유형 | 입력 | 출력 | 추정 비용 | 도구 | LLM |
|---|---|---|---|---|---|---|
| OCI홀딩스__complex_closed | complex_closed | 40,291 | 957 | 55원 | 3 | 4 |
| 두산로보틱스__complex_closed | complex_closed | 40,920 | 556 | 54원 | 3 | 5 |
| 레인보우로보틱스__multi_open | multi_open | 36,350 | 1,176 | 51원 | 4 | 6 |
| HD현대일렉트릭__complex_closed | complex_closed | 32,437 | 700 | 44원 | 2 | 5 |
| LG생활건강__multi_open | multi_open | 29,893 | 1,207 | 43원 | 3 | 3 |

## 낭비 신호

| 항목 | 값 | 무슨 뜻인가 |
|---|---|---|
| 재호출 차단 | 8 | 같은 인자로 또 불렀다. 많으면 모델이 검색어를 안 바꾸는 것 |
| error/not_found 뒤 재시도 | 10 | 스스로 고쳐 다시 불렀다. 많으면 첫 시도의 검색어·필터가 나쁘다 |
| 목록만 보고 답하려다 본문 검색으로 복귀 | 1 | 내용 질문에서 list_disclosures 만 보고 답하려 한 횟수 |
| 도구 6회 한도 도달 문항 | 3/40 | 많으면 계획이 헤맨다 |
| 429 재시도 | 13 | 대기 126초. 요청이 거절된 것이라 낭비 토큰은 **측정 불가** |
| 전체 HTTP 재시도 | 13 | 429 + 5xx + 네트워크 |
| 다시 보낸 프롬프트 토큰(추정) | 129,572 | 재시도가 같은 본문을 다시 보냈다. 서버가 과금했는지는 알 수 없다 |

## 유형별

| 유형 | 문항 | 평균 입력 | 평균 출력 | 평균 비용 | 평균 도구 | 평균 초 | 근거 0건 |
|---|---|---|---|---|---|---|---|
| complex_closed | 7 | 29,421 | 700 | 40원 | 2.7 | 28 | 0 |
| complex_open | 7 | 22,949 | 666 | 32원 | 3.4 | 26 | 0 |
| multi_closed | 7 | 21,533 | 462 | 29원 | 2.3 | 31 | 1 |
| multi_open | 7 | 26,236 | 1,043 | 38원 | 3.0 | 37 | 1 |
| search_closed | 6 | 15,234 | 330 | 21원 | 1.2 | 16 | 0 |
| search_open | 6 | 15,946 | 632 | 23원 | 1.5 | 19 | 0 |

## 근거를 못 올린 문항

| 통 | 문항 | 뜻 |
|---|---|---|
| 검색 | 1 | 도구는 불렀는데 근거가 하나도 안 올라왔다 |
| 계획 | 1 | 조회를 안 했거나 도구를 헤맸다(한도·전부 실패) |
| 보류(사람) | 38 | 근거는 올렸다. 맞는지는 사람이 본다 |

**자동 채점은 폐기했다.** 정답에 '오답 유형' 반례 숫자가 섞여 있어 숫자 일치로는
맞고 틀림을 가릴 수 없다. 판정은 `reports/e2e_v5_review.md` 에서 한다.

## 유형별 (기계로 잰 것만)

| 유형 | 문항 | 근거 확보 | 근거 확보율 | 평균 근거 | 평균 도구 | 평균 비용 |
|---|---|---|---|---|---|---|
| complex_closed | 7 | 7 | 100% | 5.9 | 2.7 | 40원 |
| complex_open | 7 | 7 | 100% | 5.3 | 3.4 | 32원 |
| multi_closed | 7 | 6 | 86% | 2.3 | 2.3 | 29원 |
| multi_open | 7 | 6 | 86% | 2.9 | 3.0 | 38원 |
| search_closed | 6 | 6 | 100% | 2.8 | 1.2 | 21원 |
| search_open | 6 | 6 | 100% | 5.7 | 1.5 | 23원 |

전체 근거 확보율 **38/40 (95%)**

## 도구별 성적

| 도구 | 호출 | ok | not_found | error |
|---|---|---|---|---|
| calculate | 9 | 9 | 0 | 0 |
| list_disclosures | 9 | 5 | 0 | 4 |
| resolve_company | 1 | 1 | 0 | 0 |
| search_disclosure | 77 | 63 | 8 | 6 |

## 수정 효과 집계

| 항목 | 값 |
|---|---|
| 도구 0회로 답한 문항 | **0**/40  |
| 조회하라고 되돌린 문항(FORCE_TOOL) | 1 |
| 목록에서 본문 검색으로 되돌린 문항 | 1 |
| 타사 데이터를 근거로 단 문항 | **0**  |
| 항목별 거절을 밝힌 문항 | 10 (총 17회) |
| 근거 0건인데 단정한 문항(unsourced_claim) | **2** ['레인보우로보틱스__multi_open', 'OCI홀딩스__multi_closed'] |

## 판정 (사람이 채운다)

`reports/e2e_v5_review.md` 에 질문·정답·답변이 나란히 있다.
이 표는 그 파일을 다 채운 뒤 옮겨 적는 자리다.

| 문항 | 유형 | 근거 | 도구 | 정답 | 출처 | 환각 |
|---|---|---|---|---|---|---|
| OCI홀딩스__complex_closed | complex_closed | 6 | 3 | | | |
| 디앤디파마텍__complex_open | complex_open | 7 | 4 | | | |
| 삼성화재해상보험__multi_closed | multi_closed | 1 | 2 | | | |
| 레인보우로보틱스__multi_open | multi_open | 0 | 4 | | | |
| 삼성SDI__search_closed | search_closed | 2 | 1 | | | |
| 삼성바이오로직스__search_open | search_open | 3 | 1 | | | |
| 두산로보틱스__complex_closed | complex_closed | 4 | 3 | | | |
| SK텔레콤__complex_open | complex_open | 10 | 3 | | | |
| 두산로보틱스__multi_closed | multi_closed | 3 | 1 | | | |
| HMM__multi_open | multi_open | 4 | 6 | | | |
| SK하이닉스__search_closed | search_closed | 1 | 1 | | | |
| LIG디펜스앤에어로스페이스__search_open | search_open | 5 | 1 | | | |
| 미래에셋증권__complex_closed | complex_closed | 4 | 2 | | | |
| 시프트업__complex_open | complex_open | 7 | 2 | | | |
| LIG디펜스앤에어로스페이스__multi_closed | multi_closed | 1 | 2 | | | |
| 시프트업__multi_open | multi_open | 2 | 1 | | | |
| LG유플러스__search_closed | search_closed | 4 | 2 | | | |
| LG에너지솔루션__search_open | search_open | 4 | 1 | | | |
| HD현대일렉트릭__complex_closed | complex_closed | 10 | 2 | | | |
| 미래에셋증권__complex_open | complex_open | 2 | 3 | | | |
| LG이노텍__multi_closed | multi_closed | 4 | 1 | | | |
| 삼성SDI__multi_open | multi_open | 4 | 3 | | | |
| OCI홀딩스__search_closed | search_closed | 3 | 1 | | | |
| POSCO홀딩스__search_open | search_open | 11 | 3 | | | |
| LG유플러스__complex_closed | complex_closed | 1 | 3 | | | |
| 삼성E&A__complex_open | complex_open | 1 | 6 | | | |
| OCI홀딩스__multi_closed | multi_closed | 0 | 6 | | | |
| 삼성생명__multi_open | multi_open | 4 | 3 | | | |
| 삼성바이오로직스__search_closed | search_closed | 4 | 1 | | | |
| 대우건설__search_open | search_open | 4 | 1 | | | |
| 두산퓨얼셀__complex_closed | complex_closed | 11 | 3 | | | |
| LG씨엔에스__complex_open | complex_open | 6 | 3 | | | |
| NC__multi_closed | multi_closed | 3 | 2 | | | |
| 두산에너빌리티__multi_open | multi_open | 4 | 1 | | | |
| 두산에너빌리티__search_closed | search_closed | 3 | 1 | | | |
| OCI홀딩스__search_open | search_open | 7 | 2 | | | |
| HD현대중공업__complex_closed | complex_closed | 5 | 3 | | | |
| POSCO홀딩스__complex_open | complex_open | 4 | 3 | | | |
| HMM__multi_closed | multi_closed | 4 | 2 | | | |
| LG생활건강__multi_open | multi_open | 2 | 3 | | | |
