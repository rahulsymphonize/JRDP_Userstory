# Multi-Provider LLM Agent

Bring-your-own-key LLM agent (OpenAI, Anthropic, Gemini, Mistral) with per-user system prompt patches.
FastAPI + PostgreSQL backend, minimal Streamlit UI.

This application lets each user connect their own OpenAI, Anthropic, Gemini or Mistral API key (stored with envelope encryption, never shown again), pick any model that key can use, and run prompts through one common interface. Every user gets a fixed, protected base prompt (Role, Safety & Compliance) that an admin controls, plus a personal patch to the editable sections (Rules, Output Format, Behaviour) with validation, diffs, versioning and revert. Unsafe edits are rejected on save, and answers that leak the protected instructions are withheld, so teams can let people tailor an assistant to their own work without being able to switch off the guardrails. Every run is audited with a correlation ID, which makes it suited to internal AI assistants, bring-your-own-key tools, provider comparison and prompt experimentation.

## Quick start

Windows Command Prompt (cmd). Needs [uv](https://docs.astral.sh/uv/) (`winget install --id astral-sh.uv`) and Git. It fetches Python 3.12 and the dependencies on the first run.

    git clone https://github.com/rahulsymphonize/JRDP_Userstory.git
    cd JRDP_Userstory
    start.cmd

Then open http://localhost:8501 (UI). The API is on http://localhost:8000, with docs at `/docs`. Press Ctrl+C to stop both (if the API keeps running, close the window). This mode uses SQLite and a dev user ID, so there is nothing to configure. Sign in as user ID `admin` to see the admin tools; any other ID is a normal user.

## Run

Production-shaped stack:

    copy .env.example .env         & rem fill in KEKS, OIDC_*, POSTGRES_PASSWORD, ADMIN_USERS
    docker compose up --build      # db -> migrate -> api + ui

Tests:

    .venv\Scripts\python -m pytest -q tests
    set TEST_DATABASE_URL=postgresql+psycopg://user@host/db
    .venv\Scripts\python -m pytest -q tests      & rem also runs the concurrency test

## Deploying

1. Run `python -m backend.manage migrate` once per deploy, before starting the new API version. It applies
   migrations, syncs the provider list and publishes `backend/base_prompt.json` if it changed. The API
   refuses to start against a database that has not been migrated.
2. Start the API: `uvicorn backend.main:app --workers N --proxy-headers --no-access-log`.
3. Terminate TLS in front of the API and UI. API keys must never travel over plain HTTP.
4. Probes: `/healthz` (liveness) and `/readyz` (database reachable).

With `APP_ENV=production` the API will not start unless OIDC auth, PostgreSQL and `KEKS` are configured.
`/docs` is disabled in production.

**Single app (Vercel):** `asgi.py` serves the API and the Streamlit UI from one process and one URL: API
routes first, the UI at `/`. `pyproject.toml` points Vercel at it. Set `LLM_AGENT_API` to the deployment's own
`https://<host>/api/v1`. On Vercel the UI's WebSocket closes at the function's max duration (300s on Hobby);
Streamlit reconnects, but widget state can reset if the reconnect lands on another instance.

**Sign-in (OIDC):** with `LLM_AGENT_AUTH=oidc` and `OIDC_CLIENT_ID` set, `asgi.py` turns on `st.login`. Users sign
in with the IdP and the UI sends their ID token to the API, so `OIDC_AUDIENCE` must equal the client id.
Also set `OIDC_CLIENT_SECRET`, `AUTH_COOKIE_SECRET` (random, 32+ chars) and `PUBLIC_URL` (the app's base URL).
At the IdP, allow `<PUBLIC_URL>/oauth2callback` as both a callback URL and a logout URL. `ADMIN_USERS` takes
`sub` values; each user's id is shown in the sidebar after sign-in.

## Operations

- **Change the base prompt:** edit `backend/base_prompt.json` and deploy, or run
  `python -m backend.manage publish-base FILE`. Users whose patch no longer applies are flagged and get the
  base prompt until they apply their edits again or reset.
- **Rotate the encryption key:** run `manage gen-kek`, add the new key at the front of `KEKS`
  (`new:key,old:key`), deploy, then run `manage rewrap`. Once rewrap finishes, the old key can be removed.
- **Move to a managed KMS:** replace the bodies of `wrap_dek` / `unwrap_dek` in `backend/crypto.py`.
- **Trace a request:** every response and error body carries `correlationId`. It is also in the JSON logs
  and in `execution_audit.correlation_id`.

## Layout

- `backend/main.py`: REST API (`/api/v1/...`), error format, correlation IDs, rate limits
- `backend/providers.py`: one adapter per provider, key format checks, provider error mapping
- `backend/prompts.py`: patch build/validate/apply, diff, versioning
- `backend/crypto.py`: envelope encryption (per-key AES-GCM data key, wrapped by a KEK)
- `backend/auth.py`: OIDC JWT verification via JWKS (`sub` = user id)
- `backend/db.py` + `backend/migrations/`: schema and Alembic migrations
- `backend/manage.py`: deploy-time commands
- `asgi.py`: API + UI as one ASGI app (the Vercel entrypoint)
- `frontend/app.py`: Streamlit UI

## Prompt safety

Protected text is admin-only: users never receive it from the API. Admins are the user IDs in `ADMIN_USERS`
(comma-separated OIDC `sub` values). They can view and publish the protected sections (`GET/PUT /api/v1/admin/base-prompt`)
and trace a run by correlation ID (`GET /api/v1/admin/audit/{id}`). Users can diff any two of their saved versions
(`GET /api/v1/users/me/system-prompt/patches/diff?a=1&b=2`).

Users can edit Rules, Output Format and Behaviour, never the protected sections (Role, Safety & Compliance).
Three layers keep a user's edits from switching the protected rules off:

1. **On save:** edits that try to disable the rules, mention a protected section, or ask for the prompt itself
   are rejected (`UNSAFE_CONTENT`). Revert re-runs this check, so old versions can't bring rejected text back.
2. **In the prompt:** when a patch is active, the protected rules are repeated at the end, with a note that they win
   over anything above.
3. **On the answer:** if a model's output copies 6+ consecutive words of a protected section, it is withheld
   (`OUTPUT_BLOCKED`) and the run is audited with that status. This is the backstop. Layers 1 and 2 alone were
   bypassed by a reworded instruction against real Gemini in QA; layer 3 stopped it.

## Error codes worth knowing

| Code | Meaning |
|---|---|
| `INVALID_KEY` (422) | The provider rejected the key itself. Only this marks a stored key Invalid. |
| `MODEL_ACCESS_DENIED` (403) | The key works but can't use this model. |
| `MODEL_NOT_IN_PLAN` (403) | The model has no allowance on your plan (e.g. not on Gemini's free tier). |
| `MODEL_UNAVAILABLE` (404) | The provider doesn't serve this model (any more). |
| `PROVIDER_REJECTED` (400) | The provider refused this request on this model; retrying won't help. |
| `QUOTA_EXCEEDED` / `RATE_LIMITED` (429) | Out of quota, or slow down. |
| `PROVIDER_UNAVAILABLE` (502) | Outage or timeout (120 s). Retry later. |
| `OUTPUT_BLOCKED` (422) | The answer revealed protected instructions and was withheld. |

## Known limits

- The model cache and rate limits are per process. With many instances, each one enforces its own limit.
  Move them to Redis or the gateway if that matters.
- The input check is a keyword list and the output check catches copied text, not loose paraphrases. Add a
  moderation model if that matters for your users.
- Model lists come from the providers. A listed model can still be refused at run time (e.g. Gemini models
  retired for new users); the run then fails with `MODEL_UNAVAILABLE`.
- Only Gemini has been run against the real provider. OpenAI, Anthropic and Mistral adapters are tested with
  mocks and real key-rejection calls only.
- The Docker files have not been built yet.
- Token counts for context checks are estimated (about 4 characters per token).
- The UI asks for a pasted access token in production. It has no OAuth login flow.
