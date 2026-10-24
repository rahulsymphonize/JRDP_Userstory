"""Single deployable app: the REST API plus the Streamlit UI at /. Vercel serves this (see pyproject.toml).

API routes (/api/v1/..., /docs, /healthz, /readyz) match first; everything else goes to Streamlit.
Run locally with: uvicorn asgi:app
"""
from contextlib import asynccontextmanager
from pathlib import Path

import streamlit as st

from backend.main import app, lifespan as api_lifespan

ui = st.App(Path(__file__).with_name("frontend") / "app.py")
_ui_lifespan = ui.lifespan()


@asynccontextmanager
async def lifespan(a):
    async with api_lifespan(a), _ui_lifespan(a):
        yield


app.router.lifespan_context = lifespan
app.mount("/", ui)
