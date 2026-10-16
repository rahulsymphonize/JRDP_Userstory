#!/usr/bin/env bash
# Local dev: SQLite, dev auth (X-User-Id), API on :8000, UI on :8501.
# Ctrl+C stops both.
# For a production-shaped stack use: docker compose up --build
set -e
cd "$(dirname "$0")"
[ -d .venv ] || {
  uv venv --python 3.12 .venv &&
    uv pip install --python .venv -r requirements-dev.txt
}
mkdir -p data
[ -f data/dev.kek ] || .venv/bin/python -m backend.manage gen-kek > data/dev.kek
export APP_ENV=development AUTH_MODE=dev KEKS="dev:$(cat data/dev.kek)"
.venv/bin/python -m backend.manage migrate
.venv/bin/uvicorn backend.main:app --port 8000 --no-access-log &
trap 'kill $!' EXIT
LLM_AGENT_AUTH=dev .venv/bin/streamlit run frontend/app.py --server.port 8501 \
  --server.headless true --browser.gatherUsageStats false
