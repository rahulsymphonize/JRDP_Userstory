@echo off
rem Local dev: SQLite, dev auth (X-User-Id), API on :8000, UI on :8501. Ctrl+C stops both.
rem For a production-shaped stack use: docker compose up --build
cd /d "%~dp0"
if not exist .venv (
  uv venv --python 3.12 .venv || exit /b 1
  uv pip install --python .venv -r requirements-dev.txt || exit /b 1
)
if not exist data mkdir data
if not exist data\dev.kek .venv\Scripts\python -m backend.manage gen-kek > data\dev.kek
set /p DEV_KEK=<data\dev.kek
set APP_ENV=development
set AUTH_MODE=dev
set ADMIN_USERS=admin
set KEKS=dev:%DEV_KEK%
.venv\Scripts\python -m backend.manage migrate || exit /b 1
start "" /b .venv\Scripts\uvicorn backend.main:app --port 8000 --no-access-log
set LLM_AGENT_AUTH=dev
.venv\Scripts\streamlit run frontend\app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
