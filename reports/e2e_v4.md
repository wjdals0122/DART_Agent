# e2e 평가 — e2e_v4

실행 2026-09-03T17:05:11 · 문항 40건

## 토큰·비용

| 구분 | 토큰 | 단가(1K) | 추정 비용 |
|---|---|---|---|
| 입력 | 603,101 | 1.25원 | 754원 |
| 출력 | 15,624 | 5.0원 | 78원 |
| **합계** | **618,725** | | **832원** |

- LLM 호출 98회 · 문항당 평균 입력 15,078 + 출력 391 토큰 (21원/문항)
- 상한 3,000원 · 남은 예산 2,168원
- 실행 전 예상 1,056원 → 실제 832원 (79%)

- ⚠ 입력·출력 내역을 알 수 없는 호출 1건. 0 으로 세지 않았으므로 위 합계는 **하한**이다

HCX-005 단가: 입력 1,000토큰 1.25원 · 출력 1,000토큰 5.0원 (출력이 4배 비싸다)

## 비용이 큰 문항 상위 5

| 문항 | 유형 | 입력 | 출력 | 추정 비용 | 도구 | LLM |
|---|---|---|---|---|---|---|
| LG유플러스__complex_closed | complex_closed | 32,869 | 466 | 43원 | 4 | 5 |
| OCI홀딩스__complex_closed | complex_closed | 27,021 | 349 | 36원 | 2 | 4 |
| 삼성생명__multi_open | multi_open | 24,848 | 680 | 34원 | 4 | 6 |
| 미래에셋증권__complex_open | complex_open | 24,685 | 548 | 34원 | 4 | 3 |
| 미래에셋증권__complex_closed | complex_closed | 22,688 | 807 | 32원 | 3 | 3 |

## 낭비 신호

| 항목 | 값 | 무슨 뜻인가 |
|---|---|---|
| 재호출 차단 | 3 | 같은 인자로 또 불렀다. 많으면 모델이 검색어를 안 바꾸는 것 |
| error/not_found 뒤 재시도 | 3 | 스스로 고쳐 다시 불렀다. 많으면 첫 시도의 검색어·필터가 나쁘다 |
| 목록만 보고 답하려다 본문 검색으로 복귀 | 4 | 내용 질문에서 list_disclosures 만 보고 답하려 한 횟수 |
| 도구 6회 한도 도달 문항 | 0/40 | 많으면 계획이 헤맨다 |
| 429 재시도 | 2 | 대기 18초. 요청이 거절된 것이라 낭비 토큰은 **측정 불가** |
| 전체 HTTP 재시도 | 2 | 429 + 5xx + 네트워크 |
| 다시 보낸 프롬프트 토큰(추정) | 22,294 | 재시도가 같은 본문을 다시 보냈다. 서버가 과금했는지는 알 수 없다 |

## 유형별

| 유형 | 문항 | 평균 입력 | 평균 출력 | 평균 비용 | 평균 도구 | 평균 초 | 근거 0건 |
|---|---|---|---|---|---|---|---|
| complex_closed | 7 | 19,409 | 441 | 26원 | 2.3 | 25 | 5 |
| complex_open | 7 | 18,258 | 521 | 25원 | 3.1 | 21 | 4 |
| multi_closed | 7 | 13,002 | 261 | 18원 | 1.4 | 13 | 6 |
| multi_open | 7 | 13,618 | 531 | 20원 | 1.6 | 24 | 6 |
| search_closed | 6 | 12,471 | 166 | 16원 | 1.2 | 10 | 1 |
| search_open | 6 | 13,044 | 392 | 18원 | 1.2 | 14 | 5 |

## 근거를 못 올린 문항

| 통 | 문항 | 뜻 |
|---|---|---|
| 검색 | 24 | 도구는 불렀는데 근거가 하나도 안 올라왔다 |
| 계획 | 3 | 조회를 안 했거나 도구를 헤맸다(한도·전부 실패) |
| 보류(사람) | 13 | 근거는 올렸다. 맞는지는 사람이 본다 |

**자동 채점은 폐기했다.** 정답에 '오답 유형' 반례 숫자가 섞여 있어 숫자 일치로는
맞고 틀림을 가릴 수 없다. 판정은 `reports/e2e_v4_review.md` 에서 한다.

## 유형별 (기계로 잰 것만)

| 유형 | 문항 | 근거 확보 | 근거 확보율 | 평균 근거 | 평균 도구 | 평균 비용 |
|---|---|---|---|---|---|---|
| complex_closed | 7 | 2 | 29% | 0.4 | 2.3 | 26원 |
| complex_open | 7 | 3 | 43% | 3.1 | 3.1 | 25원 |
| multi_closed | 7 | 1 | 14% | 0.4 | 1.4 | 18원 |
| multi_open | 7 | 1 | 14% | 0.9 | 1.6 | 20원 |
| search_closed | 6 | 5 | 83% | 1.0 | 1.2 | 16원 |
| search_open | 6 | 1 | 17% | 0.3 | 1.2 | 18원 |

전체 근거 확보율 **13/40 (32%)**

## 도구별 성적

| 도구 | 호출 | ok | not_found | error |
|---|---|---|---|---|
| calculate | 2 | 2 | 0 | 0 |
| list_disclosures | 13 | 6 | 2 | 5 |
| resolve_company | 1 | 1 | 0 | 0 |
| search_disclosure | 57 | 52 | 2 | 3 |

## 수정 효과 집계

| 항목 | 값 |
|---|---|
| 도구 0회로 답한 문항 | **0**/40  |
| 조회하라고 되돌린 문항(FORCE_TOOL) | 1 |
| 목록에서 본문 검색으로 되돌린 문항 | 4 |
| 타사 데이터를 근거로 단 문항 | **0**  |
| 항목별 거절을 밝힌 문항 | 7 (총 9회) |
| 근거 0건인데 단정한 문항(unsourced_claim) | **0**  |

## 판정 (사람이 채운다)

`reports/e2e_v4_review.md` 에 질문·정답·답변이 나란히 있다.
이 표는 그 파일을 다 채운 뒤 옮겨 적는 자리다.

| 문항 | 유형 | 근거 | 도구 | 정답 | 출처 | 환각 |
|---|---|---|---|---|---|---|
| OCI홀딩스__complex_closed | complex_closed | 0 | 2 | | | |
| 디앤디파마텍__complex_open | complex_open | 4 | 3 | | | |
| 삼성화재해상보험__multi_closed | multi_closed | 0 | 1 | | | |
| 레인보우로보틱스__multi_open | multi_open | 0 | 2 | | | |
| 삼성SDI__search_closed | search_closed | 1 | 1 | | | |
| 삼성바이오로직스__search_open | search_open | 0 | 0 | | | |
| 두산로보틱스__complex_closed | complex_closed | 2 | 2 | | | |
| SK텔레콤__complex_open | complex_open | 0 | 3 | | | |
| 두산로보틱스__multi_closed | multi_closed | 3 | 2 | | | |
| HMM__multi_open | multi_open | 0 | 1 | | | |
| SK하이닉스__search_closed | search_closed | 1 | 2 | | | |
| LIG디펜스앤에어로스페이스__search_open | search_open | 0 | 1 | | | |
| 미래에셋증권__complex_closed | complex_closed | 0 | 3 | | | |
| 시프트업__complex_open | complex_open | 0 | 3 | | | |
| LIG디펜스앤에어로스페이스__multi_closed | multi_closed | 0 | 1 | | | |
| 시프트업__multi_open | multi_open | 6 | 1 | | | |
| LG유플러스__search_closed | search_closed | 1 | 1 | | | |
| LG에너지솔루션__search_open | search_open | 0 | 1 | | | |
| HD현대일렉트릭__complex_closed | complex_closed | 0 | 2 | | | |
| 미래에셋증권__complex_open | complex_open | 0 | 4 | | | |
| LG이노텍__multi_closed | multi_closed | 0 | 1 | | | |
| 삼성SDI__multi_open | multi_open | 0 | 1 | | | |
| OCI홀딩스__search_closed | search_closed | 1 | 1 | | | |
| POSCO홀딩스__search_open | search_open | 0 | 1 | | | |
| LG유플러스__complex_closed | complex_closed | 1 | 4 | | | |
| 삼성E&A__complex_open | complex_open | 11 | 3 | | | |
| OCI홀딩스__multi_closed | multi_closed | 0 | 2 | | | |
| 삼성생명__multi_open | multi_open | 0 | 4 | | | |
| 삼성바이오로직스__search_closed | search_closed | 0 | 1 | | | |
| 대우건설__search_open | search_open | 0 | 2 | | | |
| 두산퓨얼셀__complex_closed | complex_closed | 0 | 1 | | | |
| LG씨엔에스__complex_open | complex_open | 7 | 3 | | | |
| NC__multi_closed | multi_closed | 0 | 1 | | | |
| 두산에너빌리티__multi_open | multi_open | 0 | 1 | | | |
| 두산에너빌리티__search_closed | search_closed | 2 | 1 | | | |
| OCI홀딩스__search_open | search_open | 2 | 2 | | | |
| HD현대중공업__complex_closed | complex_closed | 0 | 2 | | | |
| POSCO홀딩스__complex_open | complex_open | 0 | 3 | | | |
| HMM__multi_closed | multi_closed | 0 | 2 | | | |
| LG생활건강__multi_open | multi_open | 0 | 1 | | | |
