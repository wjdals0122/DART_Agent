# e2e 평가 — e2e_v2

실행 2026-09-02T17:10:42 · 문항 40건

## 토큰·비용

| 구분 | 토큰 | 단가(1K) | 추정 비용 |
|---|---|---|---|
| 입력 | 428,648 | 1.25원 | 536원 |
| 출력 | 12,732 | 5.0원 | 64원 |
| **합계** | **441,380** | | **599원** |

- LLM 호출 86회 · 문항당 평균 입력 10,716 + 출력 318 토큰 (15원/문항)
- 상한 4,000원 · 남은 예산 3,401원
- 실행 전 예상 899원 → 실제 599원 (67%)

- ⚠ 입력·출력 내역을 알 수 없는 호출 1건. 0 으로 세지 않았으므로 위 합계는 **하한**이다

HCX-005 단가: 입력 1,000토큰 1.25원 · 출력 1,000토큰 5.0원 (출력이 4배 비싸다)

## 비용이 큰 문항 상위 5

| 문항 | 유형 | 입력 | 출력 | 추정 비용 | 도구 | LLM |
|---|---|---|---|---|---|---|
| 디앤디파마텍__complex_open | complex_open | 29,945 | 371 | 39원 | 4 | 3 |
| 대우건설__search_open | search_open | 20,138 | 544 | 28원 | 2 | 2 |
| POSCO홀딩스__search_open | search_open | 19,532 | 484 | 27원 | 2 | 3 |
| POSCO홀딩스__complex_open | complex_open | 17,898 | 610 | 25원 | 3 | 2 |
| 미래에셋증권__complex_open | complex_open | 18,132 | 436 | 25원 | 3 | 2 |

## 낭비 신호

| 항목 | 값 | 무슨 뜻인가 |
|---|---|---|
| 재호출 차단 | 0 | 같은 인자로 또 불렀다. 많으면 모델이 검색어를 안 바꾸는 것 |
| error/not_found 뒤 재시도 | 2 | 스스로 고쳐 다시 불렀다. 많으면 첫 시도의 검색어·필터가 나쁘다 |
| 도구 6회 한도 도달 문항 | 0/40 | 많으면 계획이 헤맨다 |
| 429 재시도 | 0 | 대기 0초. 요청이 거절된 것이라 낭비 토큰은 **측정 불가** |
| 전체 HTTP 재시도 | 0 | 429 + 5xx + 네트워크 |
| 다시 보낸 프롬프트 토큰(추정) | 0 | 재시도가 같은 본문을 다시 보냈다. 서버가 과금했는지는 알 수 없다 |

## 유형별

| 유형 | 문항 | 평균 입력 | 평균 출력 | 평균 비용 | 평균 도구 | 평균 초 | 근거 0건 |
|---|---|---|---|---|---|---|---|
| complex_closed | 7 | 5,780 | 169 | 8원 | 1.6 | 11 | 7 |
| complex_open | 7 | 15,524 | 420 | 22원 | 2.6 | 21 | 0 |
| multi_closed | 7 | 12,536 | 269 | 17원 | 1.7 | 13 | 0 |
| multi_open | 7 | 8,217 | 419 | 12원 | 1.0 | 16 | 3 |
| search_closed | 6 | 9,116 | 140 | 12원 | 1.0 | 9 | 0 |
| search_open | 6 | 13,259 | 492 | 19원 | 1.7 | 17 | 0 |

## 실패 3분류 (기계 판정 · 잠정)

| 통 | 문항 | 뜻 |
|---|---|---|
| 검색 | 9 | 근거를 하나도 못 올렸다 |
| 계획 | 1 | 도구를 헤맸다(한도 도달·전부 실패·조회 안 함) |
| 작문 | 29 | 근거는 있는데 답이 안 맞는다 |
| 통과 | 1 | 정답 숫자의 절반 이상이 답변에 있다 |

틀린 것으로 분류된 문항 39/40.
**서술형은 숫자가 없어 기계로 가릴 수 없다** — 통과/실패를 그대로 믿지 말고
`reports/e2e_v2_review.md` 를 사람이 읽어야 한다.

## 유형별 (잠정 정확도)

| 유형 | 문항 | 숫자 잴 수 있음 | 통과 | 통과율 | 못 재는 문항 |
|---|---|---|---|---|---|
| complex_closed | 7 | 7 | 0 | 0% | 0 |
| complex_open | 7 | 7 | 0 | 0% | 0 |
| multi_closed | 7 | 7 | 0 | 0% | 0 |
| multi_open | 7 | 7 | 1 | 14% | 0 |
| search_closed | 6 | 6 | 0 | 0% | 0 |
| search_open | 6 | 6 | 0 | 0% | 0 |

## 수정 효과 집계

| 항목 | 값 |
|---|---|
| 도구 0회로 답한 문항 | **0**/40  |
| 조회하라고 되돌린 문항(FORCE_TOOL) | 0 |
| 타사 데이터를 근거로 단 문항 | **0**  |
| 항목별 거절을 밝힌 문항 | 10 (총 14회) |

## 채점

`verdict` 는 비어 있다. 이 스크립트는 채점하지 않는다 — 자동 채점 방식이
아직 정해지지 않았다. `numbers_hit` 은 정답의 숫자가 답변에 몇 개 들어 있는지를
센 것으로 **닫힌 문항의 힌트일 뿐 점수가 아니다**(서술형은 숫자가 없다).

| 문항 | 유형 | 숫자 일치 | 근거 | 비고 |
|---|---|---|---|---|
| OCI홀딩스__complex_closed | complex_closed | 1/9 | 0 |  |
| 디앤디파마텍__complex_open | complex_open | 5/16 | 6 |  |
| 삼성화재해상보험__multi_closed | multi_closed | 0/11 | 4 |  |
| 레인보우로보틱스__multi_open | multi_open | 2/21 | 0 |  |
| 삼성SDI__search_closed | search_closed | 1/8 | 1 |  |
| 삼성바이오로직스__search_open | search_open | 1/8 | 4 |  |
| 두산로보틱스__complex_closed | complex_closed | 0/14 | 0 |  |
| SK텔레콤__complex_open | complex_open | 2/9 | 10 |  |
| 두산로보틱스__multi_closed | multi_closed | 1/13 | 4 |  |
| HMM__multi_open | multi_open | 1/20 | 0 |  |
| SK하이닉스__search_closed | search_closed | 1/10 | 1 |  |
| LIG디펜스앤에어로스페이스__search_open | search_open | 0/15 | 5 |  |
| 미래에셋증권__complex_closed | complex_closed | 1/21 | 0 | LLM 호출 실패: HTTP 400: {"status":{"code":" |
| 시프트업__complex_open | complex_open | 3/12 | 4 |  |
| LIG디펜스앤에어로스페이스__multi_closed | multi_closed | 4/10 | 3 |  |
| 시프트업__multi_open | multi_open | 6/7 | 7 |  |
| LG유플러스__search_closed | search_closed | 0/6 | 1 |  |
| LG에너지솔루션__search_open | search_open | 1/4 | 5 |  |
| HD현대일렉트릭__complex_closed | complex_closed | 1/11 | 0 |  |
| 미래에셋증권__complex_open | complex_open | 4/18 | 5 |  |
| LG이노텍__multi_closed | multi_closed | 4/10 | 1 |  |
| 삼성SDI__multi_open | multi_open | 10/22 | 7 |  |
| OCI홀딩스__search_closed | search_closed | 0/12 | 4 |  |
| POSCO홀딩스__search_open | search_open | 0/25 | 7 |  |
| LG유플러스__complex_closed | complex_closed | 1/14 | 0 |  |
| 삼성E&A__complex_open | complex_open | 1/18 | 12 |  |
| OCI홀딩스__multi_closed | multi_closed | 6/16 | 4 |  |
| 삼성생명__multi_open | multi_open | 0/23 | 0 |  |
| 삼성바이오로직스__search_closed | search_closed | 1/7 | 1 |  |
| 대우건설__search_open | search_open | 6/17 | 8 |  |
| 두산퓨얼셀__complex_closed | complex_closed | 0/20 | 0 |  |
| LG씨엔에스__complex_open | complex_open | 5/12 | 3 |  |
| NC__multi_closed | multi_closed | 1/10 | 3 |  |
| 두산에너빌리티__multi_open | multi_open | 5/23 | 5 |  |
| 두산에너빌리티__search_closed | search_closed | 1/8 | 4 |  |
| OCI홀딩스__search_open | search_open | 1/15 | 4 |  |
| HD현대중공업__complex_closed | complex_closed | 0/13 | 0 |  |
| POSCO홀딩스__complex_open | complex_open | 3/10 | 6 |  |
| HMM__multi_closed | multi_closed | 2/9 | 3 |  |
| LG생활건강__multi_open | multi_open | 0/8 | 4 |  |
