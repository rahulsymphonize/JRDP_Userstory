# Multi-Provider LLM Agent

Bring-your-own-key LLM agent (OpenAI, Anthropic, Gemini, Mistral) with per-user system prompt patches.
FastAPI + PostgreSQL backend, minimal Streamlit UI.

## Run

Local dev (SQLite, dev auth via `X-User-Id`, no setup needed):

    ./start.sh                     # API :8000 (docs at /docs), UI :8501

Production-shaped stack:

    cp .env.example .env           # fill in KEKS, OIDC_*, POSTGRES_PASSWORD
    docker compose up --build      # db -> migrate -> api + ui

Tests:

    .venv/bin/python -m pytest -q tests
    TEST_DATABASE_URL=postgresql+psycopg://user@host/db .venv/bin/python -m pytest -q tests   # also runs the concurrency test

## Deploying

1. Run `python -m backend.manage migrate` once per deploy, before starting the new API version. It applies
   migrations, syncs the provider list and publishes `backend/base_prompt.json` if it changed. The API
   refuses to start against a database that has not been migrated.
2. Start the API: `uvicorn backend.main:app --workers N --proxy-headers --no-access-log`.
3. Terminate TLS in front of the API and UI. API keys must never travel over plain HTTP.
4. Probes: `/healthz` (liveness) and `/readyz` (database reachable).

With `APP_ENV=production` the API will not start unless OIDC auth, PostgreSQL and `KEKS` are configured.
`/docs` is disabled in production.

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
- `frontend/app.py`: Streamlit UI

## Prompt safety

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
