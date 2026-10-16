"""REST API. Every /users/me route and every query is scoped to the
authenticated caller.
"""

import logging
import re
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import delete, select, text, update
from sqlalchemy.sql import func
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import config, crypto, db, logs, prompts, providers
from backend.auth import current_user
from backend.errors import AppError

log = logging.getLogger("llm_agent")

MODEL_CACHE_TTL = 15 * 60
MAX_CACHE_ENTRIES = 10000
# chunked bodies bypass this; cap them at the proxy too
MAX_BODY_BYTES = 1_000_000
# ponytail: per-process cache and rate limits; move both to Redis if one
# instance's view isn't enough
_model_cache = {}  # (user_id, provider_id) -> (expires_at, models)
_hits = {}  # (bucket, user_id) -> deque of timestamps
_hits_lock = threading.Lock()
_last_sweep = 0.0


@asynccontextmanager
async def lifespan(app):
    config.validate()
    logs.setup()
    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        config.THREADPOOL_SIZE
    )
    try:
        with db.engine.connect() as conn:
            prompts.current_base(conn)
    except Exception as e:
        log.critical("database not ready: %s", type(e).__name__)
        raise SystemExit(
            "Database unreachable or not migrated. Run: python -m "
            "backend.manage migrate"
        )
    log.info(
        "started",
        extra={"fields": {"env": config.ENV, "auth": config.AUTH_MODE}},
    )
    yield


_docs = config.ENV != "production"
app = FastAPI(
    title="Multi-Provider LLM Agent",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)


# ---------- cross-cutting ----------

_CID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


@app.middleware("http")
async def correlate(request: Request, call_next):
    incoming = request.headers.get("x-correlation-id", "")
    cid = incoming if _CID.match(incoming) else str(uuid.uuid4())
    token = logs.correlation_id.set(cid)
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        logs.correlation_id.reset(token)
        return JSONResponse(
            status_code=413,
            content={
                "code": "PAYLOAD_TOO_LARGE",
                "message": "Request is too large.",
                "correlationId": cid,
            },
        )
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except (
        Exception
    ) as e:  # handled here, while the correlation id is still set
        log.error("unhandled error", exc_info=e)
        response = _error(500, "INTERNAL_ERROR", "Something went wrong.")
    finally:
        logs.correlation_id.reset(token)
    response.headers["X-Correlation-ID"] = cid
    response.headers["Cache-Control"] = "no-store"
    if request.url.path not in ("/healthz", "/readyz"):
        log.info(
            "request",
            extra={
                "fields": {
                    "correlationId": cid,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round((time.perf_counter() - start) * 1000),
                }
            },
        )
    return response


def _error(status, code, message):
    body = {
        "code": code,
        "message": message,
        "correlationId": logs.correlation_id.get(),
    }
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(AppError)
async def _app_error(request, e):
    return _error(e.status, e.code, e.message)


@app.exception_handler(RequestValidationError)
async def _validation_error(request, e):
    # field names only: pydantic's default body echoes the input, which may be
    # an API key
    if any(err["type"] == "json_invalid" for err in e.errors()):
        return _error(
            400, "VALIDATION_ERROR", "The request body is not valid JSON."
        )
    fields = ", ".join(
        ".".join(str(p) for p in err["loc"][1:]) for err in e.errors()
    )
    return _error(
        400, "VALIDATION_ERROR", f"Invalid request: {fields or 'body'}."
    )


@app.exception_handler(StarletteHTTPException)
async def _http_error(request, e):
    codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
    return _error(
        e.status_code,
        codes.get(e.status_code, "HTTP_ERROR"),
        "Request could not be handled.",
    )


@app.exception_handler(Exception)
async def _unhandled(request, e):
    log.error("unhandled error", exc_info=e)
    return _error(500, "INTERNAL_ERROR", "Something went wrong.")


def rate_limit(bucket, user, per_minute):
    global _last_sweep
    now = time.monotonic()
    with _hits_lock:
        if len(_hits) > MAX_CACHE_ENTRIES and now - _last_sweep > 10:
            _last_sweep = now
            for k in [k for k, q in _hits.items() if not q or q[-1] < now - 60]:
                del _hits[k]
        q = _hits.setdefault((bucket, user), deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= per_minute:
            raise AppError(
                429, "RATE_LIMITED", "Too many requests. Try again in a minute."
            )
        q.append(now)


# ---------- helpers ----------


def _load_key(conn, user_id, provider_id):
    c = db.credentials.c
    row = conn.execute(
        select(c.encrypted_key, c.wrapped_dek, c.kek_id, c.status).where(
            c.user_id == user_id, c.provider_id == provider_id
        )
    ).one_or_none()
    if not row:
        raise AppError(
            400, "NO_API_KEY", "Add an API key for this provider first."
        )
    if row.status != "Valid":
        raise AppError(
            400,
            "NO_VALID_KEY",
            "Your API key for this provider is invalid. Replace it to "
            "continue.",
        )
    key = crypto.decrypt(
        row.encrypted_key, row.wrapped_dek, row.kek_id, user_id, provider_id
    )
    return key, row.encrypted_key


def _get_key(user_id, provider_id):
    """(plaintext key, its stored ciphertext). The ciphertext identifies which
    saved key a call used.
    """
    with db.engine.connect() as conn:
        return _load_key(conn, user_id, provider_id)


def _mark_invalid(user_id, provider_id, ciphertext):
    """Mark the key Invalid only if it is still the one that failed; a key saved
    meanwhile stays Valid.
    """
    c = db.credentials.c
    with db.engine.begin() as conn:
        n = conn.execute(
            update(db.credentials)
            .where(
                c.user_id == user_id,
                c.provider_id == provider_id,
                c.encrypted_key == ciphertext,
            )
            .values(status="Invalid")
        ).rowcount
    if n:
        _model_cache.pop((user_id, provider_id), None)


def _provider_call(user_id, provider_id, ciphertext, op, *args):
    try:
        return providers.call(provider_id, op, *args)
    except AppError as e:
        if e.code == "INVALID_KEY":
            _mark_invalid(user_id, provider_id, ciphertext)
        raise


def _list_models(user_id, provider_id, key, ciphertext=None):
    """Cached catalogue. Without a ciphertext, `key` is a candidate being
    validated: a rejection marks nothing.
    """
    ck = (user_id, provider_id)
    hit = _model_cache.get(ck)
    if hit and hit[0] > time.time() and ciphertext is not None:
        return hit[1]
    if ciphertext is None:
        models = providers.call(provider_id, "list_models", key)
    else:
        models = _provider_call(
            user_id, provider_id, ciphertext, "list_models", key
        )
    providers.tag_free_tier(provider_id, models)
    if len(_model_cache) >= MAX_CACHE_ENTRIES:
        _model_cache.pop(next(iter(_model_cache)), None)
    _model_cache[ck] = (time.time() + MODEL_CACHE_TTL, models)
    return models


def _context_window(user_id, provider_id, model_id):
    """From the cached catalogue only, never a provider call; None if unknown.
    """
    hit = _model_cache.get((user_id, provider_id))
    if hit:
        return next(
            (m["context_window"] for m in hit[1] if m["id"] == model_id), None
        )
    return None


def _preference(conn, user_id):
    p = db.preferences.c
    return conn.execute(
        select(p.provider_id, p.model_id, p.updated_at).where(
            p.user_id == user_id
        )
    ).one_or_none()


def _user_window(conn, user_id):
    pref = _preference(conn, user_id)
    return (
        _context_window(user_id, pref.provider_id, pref.model_id)
        if pref
        else None
    )


# ---------- health ----------


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz():
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        raise AppError(503, "NOT_READY", "Database unavailable.")
    return {"status": "ready"}


# ---------- Phase 1: providers, keys, models ----------


@app.get("/api/v1/providers")
def list_providers(user=Depends(current_user)):
    c, p = db.credentials.c, db.providers.c
    with db.engine.connect() as conn:
        creds = {
            r.provider_id: r
            for r in conn.execute(
                select(c.provider_id, c.key_last4, c.status).where(
                    c.user_id == user
                )
            )
        }
        rows = conn.execute(
            select(p.id, p.name).where(p.is_active).order_by(p.name)
        ).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "key_status": creds[r.id].status if r.id in creds else "Not Set",
            "key_last4": creds[r.id].key_last4 if r.id in creds else None,
        }
        for r in rows
    ]


@app.get("/api/v1/providers/{provider_id}/models")
def list_models(provider_id: str, user=Depends(current_user)):
    providers.get(provider_id)
    return _list_models(user, provider_id, *_get_key(user, provider_id))


class KeyIn(BaseModel):
    api_key: SecretStr = Field(max_length=512)


@app.put("/api/v1/users/me/credentials/{provider_id}")
def save_key(provider_id: str, body: KeyIn, user=Depends(current_user)):
    key = body.api_key.get_secret_value().strip()
    providers.check_key_format(provider_id, key)
    rate_limit(
        "key", user, config.KEY_SAVES_PER_MINUTE
    )  # stops use as a key-checking oracle
    _list_models(
        user, provider_id, key
    )  # live validation; INVALID_KEY (422) if rejected
    ciphertext, wrapped, kek_id = crypto.encrypt(key, user, provider_id)
    with db.engine.begin() as conn:
        db.upsert(
            conn,
            db.credentials,
            {
                "user_id": user,
                "provider_id": provider_id,
                "encrypted_key": ciphertext,
                "wrapped_dek": wrapped,
                "kek_id": kek_id,
                "key_last4": key[-4:],
                "status": "Valid",
                "validated_at": func.now(),
            },
            ["user_id", "provider_id"],
        )
    log.info(
        "api key saved",
        extra={"fields": {"user": user, "provider": provider_id}},
    )
    return {
        "provider_id": provider_id,
        "key_status": "Valid",
        "key_last4": key[-4:],
    }


@app.delete("/api/v1/users/me/credentials/{provider_id}")
def delete_key(provider_id: str, user=Depends(current_user)):
    providers.get(provider_id)
    c = db.credentials.c
    with db.engine.begin() as conn:
        n = conn.execute(
            delete(db.credentials).where(
                c.user_id == user, c.provider_id == provider_id
            )
        ).rowcount
    _model_cache.pop((user, provider_id), None)
    if not n:
        raise AppError(
            404, "NOT_FOUND", "No API key is saved for this provider."
        )
    log.info(
        "api key deleted",
        extra={"fields": {"user": user, "provider": provider_id}},
    )
    return {"provider_id": provider_id, "key_status": "Not Set"}


class PreferenceIn(BaseModel):
    provider_id: str = Field(max_length=32)
    model_id: str = Field(min_length=1, max_length=255)


@app.get("/api/v1/users/me/model-preference")
def get_preference(user=Depends(current_user)):
    with db.engine.connect() as conn:
        r = _preference(conn, user)
    return (
        r._asdict()
        if r
        else {"provider_id": None, "model_id": None, "updated_at": None}
    )


@app.put("/api/v1/users/me/model-preference")
def save_preference(body: PreferenceIn, user=Depends(current_user)):
    providers.get(body.provider_id)
    models = _list_models(
        user, body.provider_id, *_get_key(user, body.provider_id)
    )
    if body.model_id not in {m["id"] for m in models}:
        raise AppError(
            400,
            "MODEL_UNAVAILABLE",
            "The selected model is not available for this provider.",
        )
    with db.engine.begin() as conn:
        db.upsert(
            conn,
            db.preferences,
            {
                "user_id": user,
                "provider_id": body.provider_id,
                "model_id": body.model_id,
                "updated_at": func.now(),
            },
            ["user_id"],
        )
    return {"provider_id": body.provider_id, "model_id": body.model_id}


# ---------- Phase 2: system prompt ----------


class SectionsIn(BaseModel):
    sections: dict[str, str] = Field(max_length=20)


@app.get("/api/v1/users/me/system-prompt")
def get_system_prompt(user=Depends(current_user)):
    with db.engine.connect() as conn:
        return prompts.effective(conn, user)


@app.post("/api/v1/users/me/system-prompt/preview")
def preview(body: SectionsIn, user=Depends(current_user)):
    with db.engine.connect() as conn:
        base = prompts.current_base(conn)
        current = prompts.effective(conn, user)["prompt"]
        window = _user_window(conn, user)
    patched = prompts.apply_patch(
        base, prompts.build_patch(base, body.sections, window)
    )
    resolved = prompts.render(patched, base["protected"])
    return {
        "current": current,
        "resolved": resolved,
        "diff": prompts.diff(current, resolved),
    }


@app.post("/api/v1/users/me/system-prompt/patches", status_code=201)
def apply_patch(body: SectionsIn, user=Depends(current_user)):
    with db.engine.begin() as conn:
        base = prompts.current_base(conn)
        patch = prompts.build_patch(
            base, body.sections, _user_window(conn, user)
        )
        version = prompts.save_patch(conn, user, base["version"], patch)
        result = prompts.effective(conn, user)
    log.info(
        "prompt patch applied",
        extra={"fields": {"user": user, "version": version}},
    )
    return result


@app.get("/api/v1/users/me/system-prompt/patches")
def patch_history(user=Depends(current_user)):
    p = db.patches.c
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(
                p.version,
                p.base_version,
                p.patch_content,
                p.is_active,
                p.created_at,
            )
            .where(p.user_id == user)
            .order_by(p.version.desc())
            .limit(200)
        ).all()
    return [
        {
            "version": r.version,
            "base_version": r.base_version,
            "is_active": r.is_active,
            "created_at": r.created_at,
            "sections": {k: v["to"] for k, v in r.patch_content.items()},
        }
        for r in rows
    ]


@app.post(
    "/api/v1/users/me/system-prompt/patches/{version}/revert", status_code=201
)
def revert(version: int, user=Depends(current_user)):
    p = db.patches.c
    with db.engine.begin() as conn:
        r = conn.execute(
            select(p.base_version, p.patch_content).where(
                p.user_id == user, p.version == version
            )
        ).one_or_none()
        if not r:
            raise AppError(
                404, "NOT_FOUND", "That prompt version does not exist."
            )
        base = prompts.current_base(conn)
        if prompts.apply_patch(base, r.patch_content) is None:
            raise AppError(
                409,
                "PATCH_CONFLICT",
                "That version no longer fits the current base prompt. Edit and"
                " apply it again.",
            )
        # re-check under today's rules: an old version must not bring back text
        # that is now disallowed
        patch = prompts.build_patch(
            base,
            {k: v["to"] for k, v in r.patch_content.items()},
            _user_window(conn, user),
        )
        new_version = prompts.save_patch(conn, user, base["version"], patch)
        result = prompts.effective(conn, user)
    log.info(
        "prompt patch reverted",
        extra={
            "fields": {"user": user, "from": version, "version": new_version}
        },
    )
    return result


@app.delete("/api/v1/users/me/system-prompt/patches")
def reset(user=Depends(current_user)):
    with db.engine.begin() as conn:
        prompts.reset(conn, user)
        result = prompts.effective(conn, user)
    log.info("prompt reset to base", extra={"fields": {"user": user}})
    return result


# ---------- Execution ----------


class TaskIn(BaseModel):
    task: str = Field(min_length=1, max_length=50000)


@app.post("/api/v1/agent/execute")
def execute(body: TaskIn, user=Depends(current_user)):
    rate_limit("execute", user, config.EXECUTIONS_PER_MINUTE)
    # read phase; no connection is held during the provider call
    with db.engine.connect() as conn:
        pref = _preference(conn, user)
        if not pref:
            raise AppError(
                400,
                "NO_MODEL_PREFERENCE",
                "Select a provider and model before running a task.",
            )
        key, ciphertext = _load_key(
            conn, user, pref.provider_id
        )  # no platform-key fallback, by design
        eff = prompts.effective(conn, user)
    pid, model = pref.provider_id, pref.model_id
    window = _context_window(user, pid, model)
    if (
        window
        and prompts.estimate_tokens(eff["prompt"] + body.task)
        + providers.MAX_OUTPUT_TOKENS
        > window
    ):
        raise AppError(
            400,
            "CONTEXT_TOO_LONG",
            "The task is too long for the selected model.",
        )

    row = {
        "user_id": user,
        "provider_id": pid,
        "model_id": model,
        "base_version": eff["base_version"],
        "patch_version": eff["patch_version"],
        "correlation_id": logs.correlation_id.get(),
        "status": "ERROR",
    }
    start = time.perf_counter()
    try:
        output, tin, tout = _provider_call(
            user,
            pid,
            ciphertext,
            "execute",
            key,
            model,
            eff["prompt"],
            body.task,
        )
        row.update(status="SUCCESS", tokens_in=tin, tokens_out=tout)
        if prompts.leaks_protected(
            output, eff["sections"], eff["protected_sections"]
        ):
            row["status"] = "OUTPUT_BLOCKED"
            raise AppError(
                422,
                "OUTPUT_BLOCKED",
                "The response was withheld because it contained protected "
                "platform instructions.",
            )
    except AppError as e:
        row["status"] = e.code
        raise
    finally:
        row["latency_ms"] = round((time.perf_counter() - start) * 1000)
        with db.engine.begin() as conn:
            conn.execute(db.audit.insert().values(**row))
        log.info(
            "execution",
            extra={
                "fields": {
                    k: v for k, v in row.items() if k != "correlation_id"
                }
            },
        )
    return {
        "output": output,
        "provider_id": pid,
        "model_id": model,
        "base_version": eff["base_version"],
        "patch_version": eff["patch_version"],
        "prompt_conflict": eff["conflict"],
        "tokens_in": tin,
        "tokens_out": tout,
        "correlation_id": row["correlation_id"],
    }
