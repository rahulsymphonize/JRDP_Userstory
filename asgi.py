"""Single deployable app: the REST API plus the Streamlit UI at /. Vercel serves this (see pyproject.toml).

API routes (/api/v1/..., /docs, /healthz, /readyz) match first; everything else goes to Streamlit.
Run locally with: uvicorn asgi:app
"""
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

# Streamlit writes to ~/.streamlit while starting a session; serverless homes (Vercel) are read-only
if not os.access(Path.home(), os.W_OK):
    os.environ["HOME"] = tempfile.gettempdir()

import streamlit as st  # noqa: E402  (after HOME is fixed)

from backend.main import app, lifespan as api_lifespan  # noqa: E402

ui = st.App(Path(__file__).with_name("frontend") / "app.py")
_ui_lifespan = ui.lifespan()


@asynccontextmanager
async def lifespan(a):
    async with api_lifespan(a), _ui_lifespan(a):
        yield


app.router.lifespan_context = lifespan
app.mount("/", ui)
