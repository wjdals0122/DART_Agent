@echo off
REM 공시 Agent 서버 기동 — 환경을 고정하고 점검을 통과한 뒤에만 띄운다.
REM
REM   scripts\serve.bat                    점검만 (아직 서버 코드가 없다)
REM   scripts\serve.bat python -m src.api  점검 통과 후 그 명령을 실행
REM
REM 왜 이 파일이 있나
REM  - 임베딩 모델을 매번 huggingface.co 에서 확인하러 간다. 캐시가 있으므로
REM    HF_HUB_OFFLINE=1 로 네트워크를 아예 안 치게 막는다.
REM  - 이 PC 는 3128 에 mitmdump 프록시가 자동 시작된다(시작프로그램 start_proxy.bat).
REM    프록시 환경변수가 잡히면 HF 가 TLS 검증에 실패해 모델 초기화에서 죽는다.
REM    그래서 여기서 명시적으로 비운다.
REM  - 파이썬은 anaconda base 하나만 이 프로젝트가 완전히 돈다.

setlocal

set "PY=C:\ProgramData\anaconda3\python.exe"
cd /d "%~dp0.."

REM 네트워크를 안 친다 (캐시가 없으면 preflight 가 먼저 잡아낸다)
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"

REM 프록시를 명시적으로 비운다 — 남이 잡아 둔 값이 상속되지 않게
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
set "NO_PROXY=*"

REM 콘솔 인코딩 (cp949 로 로그가 죽은 전례가 있다)
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo [serve] 기동 전 점검
"%PY%" -X utf8 scripts\preflight.py
if errorlevel 1 (
  echo [serve] 점검에서 막혔다. 서버를 띄우지 않는다.
  exit /b 1
)

if "%~1"=="" (
  echo [serve] 실행할 명령이 없다. 점검만 하고 끝낸다.
  echo         예: scripts\serve.bat "%PY%" -m src.api
  exit /b 0
)

echo [serve] 실행: %*
%*
exit /b %errorlevel%
