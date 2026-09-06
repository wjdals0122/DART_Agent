# dis-044 — DART 공시 질의응답 Agent

제10회 미래에셋증권 AI Festival · 팀 인천

---

## API End-point

```
http://49.50.141.125
```

| 경로 | 용도 |
|---|---|
| `GET http://49.50.141.125/answer?question_id={id}&question={질문}` | 질의응답 (JSON 5필드) |
| `GET http://49.50.141.125/health` | 헬스체크 |

호출 예시 (한글은 URL 인코딩):

```bash
curl -s -G "http://49.50.141.125/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=SK하이닉스 2024년 연결 매출액이 얼마야?"

curl -s "http://49.50.141.125/health"
# {"status":"ok","warm_seconds":8.7,"served":11,"uptime":116737.9}
```

실측(2026-09-06 09:44): HTTP 200 · 12,511 bytes · **8.9초**.
자세한 규격·필드 설명·오류 시 동작은 **[docs/API.md](docs/API.md)**.

---

## 프로젝트 개요

DART 공시 코퍼스(**70개사 · 4,204문서 · 2023.01~2026.03**)에 대해 자연어 질문을
받아 근거와 함께 답하는 Agent다. 원문 XML/PDF를 파싱·정규화해 **614,578청크**로
자르고 bge-m3로 임베딩한 뒤, BM25와 dense를 RRF로 융합한 하이브리드 검색기 위에
HyperCLOVA X(HCX-005) function calling 에이전트를 올렸다.

외부 데이터를 쓰지 않는다(뉴스·리포트·위키·OpenDART 실시간 호출 없음).
LLM은 과제 제약에 따라 HyperCLOVA X만 쓰고, 임베딩은 로컬 GPU에서 돌린다.
정정공시는 기본적으로 최신본만 검색 대상이 되도록 마스킹한다.

설계 근거와 평가 결과는 **[docs/proposal.md](docs/proposal.md)**,
단계별 작업 기록은 [docs/WORKLOG.md](docs/WORKLOG.md)에 있다.

---

## 환경 구성

| 항목 | 값 |
|---|---|
| Python | **3.12** |
| 운영 서버 | **Ubuntu 24.04** · 4 vCPU · 16GB RAM · **GPU 없음** (CPU로 동작) |
| 개발/색인 | RTX 4090 24GB × 4 · Xeon Gold 6326 · RAM 384GB |

> **본 프로젝트는 컨테이너를 사용하지 않는다.** 운영 서버는
> Ubuntu 24.04 + Python venv + systemd 로 구성했으며, 재현에 필요한 것은
> `requirements-server.txt` 와 아래 절차다.
> **Dockerfile 은 제공하지 않는다** — 배포 방식이 컨테이너가 아니며,
> 검증하지 못한 빌드 정의를 제출하지 않기 위함이다.

### 의존성 파일이 둘이다

| 파일 | 성격 | 쓰는 곳 |
|---|---|---|
| `requirements.txt` | **하한 제약**(`>=`) | 개발·색인 환경 |
| `requirements-server.txt` | **서버 실측 고정**(`==`) | 평가 서버 재현용 |

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 개발·색인 환경
pip install -r requirements.txt

# 평가 서버를 그대로 재현할 때 (CPU)
pip install torch==2.14.0+cpu --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-server.txt
```

> **개발 PC에 `requirements-server.txt` 를 쓰지 마라.** `pandas` 2.3.0 → 3.0.5 ·
> `torch` 2.7.1+cu118 → 2.14.0+cpu 로 바뀌어 임베딩 환경이 깨진다.

### 실제 서비스 환경 버전 (서버 venv `pip freeze` 실측, 2026-09-06)

**아래가 엔드포인트를 돌리고 있는 실제 버전이다.**

| 패키지 | 운영 서버 | (참고) 개발 PC |
|---|---|---|
| `torch` | **2.14.0+cpu** | 2.7.1+cu118 |
| `pandas` | **3.0.5** | 2.3.0 |
| `scipy` | **1.17.1** | 1.13.1 |
| `numpy` | 1.26.4 | 1.26.0 |
| `pyarrow` | 25.0.1 | 25.0.1 |
| `FlagEmbedding` | 1.4.2 | 1.4.2 |
| `huggingface_hub` | 1.29.0 | 1.28.0 |
| `kiwipiepy` (+ `kiwipiepy_model` 0.23.0) | 0.23.2 | 0.23.2 |
| `lxml` | 6.1.3 | 5.2.1 |
| `PyYAML` | 6.0.3 | 6.0.1 |
| `orjson` | 3.12.0 | 3.11.6 |
| `python-dotenv` | **1.2.3** | 0.21.0 |
| `requests` | 2.34.2 | 2.32.3 |
| `psutil` | 7.2.2 | 5.9.0 |
| `uvicorn` | 0.52.4 | 0.40.0 |
| `fastapi` | 0.141.1 (설치돼 있으나 **쓰이지 않는다** — 아래 참고) | 미설치 |

서버 `pip freeze` 전체는 82개 패키지다. 위 17개가 이 프로젝트가 **직접
import** 하는 것이고(`src/` AST 대조 + `parser/`·`scripts/`의 `lxml`),
나머지는 이들이 끌고 오는 의존성이다(`transformers` 5.16.1 ·
`sentence-transformers` 6.0.1 · `datasets` 5.0.1 · `pydantic` 2.13.5 등).
`requirements-server.txt` 에는 **위 17개만** 고정했다.

> **개발 PC와 서버의 버전이 다르다.** 특히 `pandas`(2.3.0 ↔ 3.0.5) ·
> `scipy`(1.13.1 ↔ 1.17.1) · `torch`(CUDA ↔ CPU)가 메이저/마이너로 갈린다.
> **두 환경 모두에서 동작하는 것을 실측으로 확인했다** — 서버는 실질의에
> 정답을 반환하고(8.9초), 개발 PC는 `FORCE_CPU=1` 에서 서버와 검색 결과가 일치한다.
> `requirements.txt` 는 두 환경을 다 받아들이도록 하한만 걸어 두었다.

### torch — 서버는 CPU 빌드다

임베딩·검색 모두 GPU 없이 돈다. **PyPI 기본 `torch` 는 CUDA 런타임을 함께
끌고 오므로 CPU 서버에서는 CPU 전용 인덱스를 써야 한다.**

```bash
# 운영 서버 (GPU 없음) — 실제 설치된 것: 2.14.0+cpu
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 색인·임베딩 장비 (GPU) — 이 레포로 벡터를 만든 버전: 2.7.1+cu118
pip install torch --index-url https://download.pytorch.org/whl/cu118
```

CPU/GPU 판단은 코드 한 곳(`src/eval/gpu_guard.cuda_ok()`)에서만 한다.
GPU가 없거나 `FORCE_CPU=1` 이면 CPU로 내려앉는다.
CPU 실측: 첫 질의 4.4초(워밍업) · 이후 100~210ms · RAM 4.35GB.

시스템 패키지: 별도로 필요한 것은 없다. `kiwipiepy`(한국어 형태소 분석기)가
휠로 설치되며 컴파일러를 요구하지 않는다. `kiwipiepy_model` 은 `kiwipiepy` 가
자동으로 끌고 온다.

### FastAPI — 설치돼 있어도 쓰이지 않는다

API 서버는 **기본이 uvicorn 위의 순수 ASGI** 다. 서버 venv에 `fastapi 0.141.1` 이
설치돼 있지만 그 경로는 타지 않는다. `DART_API_FASTAPI=1` 을 **명시할 때만**
FastAPI 껍데기를 쓴다.

```bash
curl http://49.50.141.125/
# {"service":"공시 Agent 평가 API", ...}   ← 순수 ASGI (현재 상태 · 정상)
# {"detail":"Not Found"}                   ← FastAPI 경로였다면 이렇게 나온다
```

**왜 기본을 되돌렸나** — 예전에는 "fastapi 가 있으면 그쪽을 쓴다" 였는데,
Starlette이 쿼리를 UTF-8 고정 + `errors=replace` 로 디코딩해서 **cp949로 보내는
클라이언트의 한글 질문이 통째로 깨졌다**(실측: `하이` → `%c7%cf%c0%cc`).
지금은 항상 순수 ASGI로 뜨고, FastAPI 경로를 쓸 때도 쿼리는 같은 `_params()` 로
바이트 단계에서 읽는다. 자세한 것은 `HANDOFF.md` §15.

### 하지 말 것

- **`pip install -U` 금지.** torch/FlagEmbedding 버전에 bge-m3 벡터가 묶여 있다.
- **개발 PC에는 `fastapi` 를 설치하지 않는다.** pydantic 2.8.2 → 2.13.5 를 함께
  올려 임베딩 환경을 흔든다(실측). 서버는 이미 설치돼 있으나 쓰이지 않으므로
  그대로 둔다.

### 임베딩 모델 캐시

기동할 때 `huggingface.co` 를 확인하러 간다. 서버에는 캐시(4.3GB)를 미리 받아 두고
`HF_HUB_OFFLINE=1` 로 띄운다. `src/search/hf_cache.ensure_offline()` 이 캐시를
확인하고, 없으면 조용히 멈추지 않고 경로와 조치를 적고 종료한다.

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-m3')"
```

---

## 환경변수 (`.env`)

리포 루트에 `.env` 를 둔다. **키 값은 이 저장소 어디에도 없다.**

```ini
CLOVA_API_KEY=발급받은_키를_여기에
# 아래는 선택 — 없으면 기본값을 쓴다
CLOVA_MODEL=HCX-005
CLOVA_API_HOST=https://clovastudio.stream.ntruss.com
```

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CLOVA_API_KEY` | (필수) | HyperCLOVA X API 키. 없으면 호출 전에 사유를 반환한다 |
| `CLOVA_MODEL` | `HCX-005` | 모델 id |
| `CLOVA_API_HOST` | CLOVA Studio | API 호스트 |
| `PORT` | `80` | API 서버 포트 |
| `FORCE_CPU` | (없음) | `1` 이면 GPU가 있어도 CPU로 돈다. 서버 재현용 |
| `HF_HUB_OFFLINE` | (없음) | `1` 이면 HF 네트워크를 아예 안 친다. 서버 기동 시 설정 |
| `DART_API_FASTAPI` | (없음) | `1` 일 때만 FastAPI 껍데기를 쓴다. **평소 쓰지 않는다** |

---

## 데이터 준비

`data/` · `corpus/` 는 git에 없다(용량). 아래 순서로 만든다.

```bash
# 0) 원문 파싱 → 청킹
python scripts/03_build_docjson.py            # XML → doc.json
python scripts/05_build_doc_index.py          # 문서 메타 인덱스
python scripts/05b_build_md_index.py          # md 메타 인덱스
python scripts/06_build_chunks.py --jobs 10   # → data/processed/chunks.jsonl.gz
python scripts/07_coverage.py                 # 본문 손실 검증
python scripts/99_validate.py --all           # 검증 골든셋

# 1) 임베딩 (GPU 4장, bge-m3)
python -m src.index.embed_prepare
bash scripts/run_embed.sh --max-length 1024 --batch-tokens 16384
python -m src.index.embed_merge
python -m src.index.verify_vectors            # 행 수·shape 계약 검증

# 2) 검색 인덱스
python -m src.search.bm25_build               # BM25 (scipy CSC 자체 구현)
python -m src.search.build_latest_mask        # 정정본 마스크 latest_mask.npz
python -m src.eval.chunk_store --build        # 원문 seek 오프셋 (1회)

# 3) 확인
python scripts/preflight.py                   # 기동 전 점검(1초, 인덱스 안 올림)
python eval/ask.py "SK하이닉스 2024년 연결 매출액이 얼마야?"
```

산출물 (전부 git 미추적):

| 경로 | 크기 | 내용 |
|---|---|---|
| `data/index/vectors/dense.f32.npy` | 2.5GB | 614,578 × 1024 fp32 |
| `data/index/vectors/id_map.parquet` | 6.9MB | 행↔청크 매핑 (행 순서 계약의 기준) |
| `data/index/vectors/sparse.npz` | 338MB | bge-m3 lexical |
| `data/index/bm25/` | — | scipy CSC 색인 |
| `data/index/latest_mask.npz` | — | 정정본 마스킹 |
| `data/processed/chunks.jsonl.gz` | 272MB | 청크 원문 |

> **행 순서 계약** — `chunks.jsonl` 행 = `dense.f32.npy` 행 = `id_map.parquet` 행 =
> BM25 행. 어느 단계에서도 정렬·재배치하지 않는다. 행 수가 하나라도 다르면
> `verify_vectors` 가 멈춘다.

---

## 서버 실행

### systemd (운영)

운영 서버는 systemd가 관리한다. `serve.sh` 를 직접 부르면 80 포트 충돌이 난다.

```ini
# /etc/systemd/system/dis044.service
[Unit]
Description=dis-044 공시 Agent 평가 API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/dis-044
EnvironmentFile=/root/dis-044/.env
Environment=HF_HUB_OFFLINE=1
Environment=PORT=80
Environment=PYTHONUNBUFFERED=1
ExecStart=/root/dis-044/venv/bin/python -m src.api.server --host 0.0.0.0 --port 80
Restart=always
RestartSec=5
StandardOutput=append:/root/dis-044/logs/api.log
StandardError=append:/root/dis-044/logs/api.log

[Install]
WantedBy=multi-user.target
```

> 위 unit은 현재 서버 설정(작업 경로 `/root/dis-044`, 80 포트, `HF_HUB_OFFLINE=1`,
> 로그 `logs/api.log`)에 맞춘 **재현용 파일**이다. 서버에서 실제로 돌고 있는
> `dis044.service` 원본은 이 저장소에 포함돼 있지 않다.

```bash
systemctl daemon-reload
systemctl enable --now dis044
systemctl status dis044
journalctl -u dis044 -n 40 --no-pager
```

### 배포

```bash
bash scripts/deploy.sh            # 코드 tar → scp → systemctl restart → /health 폴링
bash scripts/deploy.sh --no-restart
```

코드·설정·프롬프트만 보낸다(`src config prompts eval scripts serve.sh requirements.txt`).
`data/` `corpus/` `.env` 는 서버 것을 그대로 둔다.

### systemd 없는 환경 (로컬 시험)

```bash
PORT=8080 bash serve.sh          # nohup 기동 · logs/api.log
bash serve.sh status
bash serve.sh stop
# 또는
python -m src.api.server --host 0.0.0.0 --port 8080
```

`serve.sh` 는 중복 기동 차단 · `HF_HUB_OFFLINE=1` · 프록시 환경변수 비우기를 한다
(프록시가 잡히면 HF가 TLS 검증에 실패해 모델 초기화에서 죽는다).

---

## 디렉터리 구조

```
dis-044/
├── README.md                  이 파일 (API 엔드포인트)
├── CLAUDE.md                  작업 상시 규칙
├── requirements.txt
├── serve.sh                   API 기동 (systemd 없는 환경용)
│
├── src/
│   ├── api/server.py          평가용 API. /answer · /health. 순수 ASGI
│   ├── agent/
│   │   ├── loop.py            에이전트 루프 (도구 6회 한도 · 검문 · 후처리)
│   │   ├── clova.py           HyperCLOVA X 호출 (function calling · 429 백오프)
│   │   ├── rewrite.py         질문 재작성 (회사·연도·하위질문 추출)
│   │   ├── context.py         <document> 조립 · 기수→연도 · 단위 경고
│   │   └── usage.py           토큰·비용 회계
│   ├── tools/                 도구 구현 (예외를 던지지 않고 status JSON 반환)
│   ├── search/
│   │   ├── hybrid.py          하이브리드 검색기 (하드필터 → BM25+dense RRF)
│   │   ├── bm25.py/bm25_build.py   scipy CSC 자체 구현
│   │   ├── build_latest_mask.py    정정본 마스킹
│   │   ├── aliases.py         회사명·별칭 해석
│   │   └── tokenize_ko.py     kiwipiepy 토크나이저
│   ├── index/                 bge-m3 임베딩 (prepare→worker→merge→verify)
│   ├── normalize/             관대한 트리 빌더 · 인코딩 · 표 라우팅 · 헤더
│   ├── extract/               재무·기간·정정·항목코드 추출
│   ├── render/                마크다운 렌더링 (표 헤더 반복)
│   ├── chunk/                 자족 청크 생성
│   └── eval/                  chunk_store · gpu_guard · 검색 평가
│
├── parser/                    이관 전 원본 파서 4종 + PARSING_NOTES.md (대조용)
├── pdf_parser/                pdf+html 문서 경로
│
├── prompts/                   시스템·planner 프롬프트 (형식 규칙은 코드에 있다)
├── config/
│   ├── tools.json             function calling 도구 정의 5개
│   ├── exception_policy.yaml  파싱 예외 정책 15개 (E1~E8 + SWALLOW)
│   └── aliases.yaml           회사 별칭
│
├── eval/
│   ├── ask.py                 단건 질의
│   ├── regression.py          회귀 확인 7문항 (정답 확정)
│   ├── run_eval.py            골든셋 검색 채점 (recall/hit@k)
│   ├── run_e2e.py             40문항 e2e
│   ├── golden.jsonl           검색 골든셋 라벨
│   └── test_*.py              LLM 0회 검증 15종
│
├── scripts/                   00~11 파이프라인 · deploy.sh · preflight.py
├── docs/
│   ├── API.md                 API 명세
│   ├── proposal.md            기술 제안서
│   ├── WORKLOG.md             단계별 작업 기록(누적)
│   └── step6_review.md        주제별 정리
├── reports/                   실측 리포트 (평가·검색·조사)
│
├── corpus/manifest.jsonl      문서 목록 4,204건 (원문은 미추적)
└── data/                      파생 산출물 전부 (미추적)
```

---

## 검증 명령

```bash
# LLM 0회 — 전부 무료
python eval/test_api.py        # API 계약 (503→200 · 예외에도 200 · 한글 왕복)
python eval/test_loop.py       # 루프가 어떤 경우에도 규격 dict 를 내는지
python eval/test_tools.py      # 도구가 예외를 안 던지는지
python eval/test_context.py    # <document> 조립 · 정정 경고
python eval/run_eval.py        # 골든셋 검색 채점

# LLM 사용
python eval/regression.py      # 회귀 7문항 (약 180원 · 2분)
python eval/run_e2e.py --n 40 --yes --max-cost 3000
python eval/token_log.py --summary
```

---

*제10회 미래에셋증권 AI Festival 제출용. Internal research use.*
