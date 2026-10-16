"""Provider adapters: list_models, validate_key, execute.

Adding a provider = one adapter class (if the API shape is new) + one PROVIDERS
entry.
"""

import re

import httpx

from backend.errors import AppError

MAX_OUTPUT_TOKENS = 2048
# Pooled client. Retries cover connection failures only: retrying a completed
# request could bill twice.
_client = httpx.Client(
    timeout=httpx.Timeout(120.0, connect=10.0),
    transport=httpx.HTTPTransport(retries=2),
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
)


def map_provider_error(resp):
    """Translate a provider HTTP error into a platform AppError. Never echoes
    the key.
    """
    text = resp.text.lower()
    s = resp.status_code
    # Only a genuine key problem invalidates the stored key; a 403 is about this
    # model or account, not the key.
    if s == 401 or any(
        m in text
        for m in (
            "api_key_invalid",
            "api key not valid",
            "invalid api key",
            "incorrect api key",
            "invalid x-api-key",
        )
    ):
        return AppError(
            422, "INVALID_KEY", "The API key was rejected by the provider."
        )
    if (
        s == 429 and "limit: 0" in text
    # Gemini: model has no allowance on this plan (e.g. not on the free tier)
    ):
        return AppError(
            403,
            "MODEL_NOT_IN_PLAN",
            "This model isn't included in your provider plan.",
        )
    if s == 429 and ("quota" in text or "billing" in text or "credit" in text):
        return AppError(
            429, "QUOTA_EXCEEDED", "Your provider quota is exhausted."
        )
    if s == 429:
        return AppError(
            429,
            "RATE_LIMITED",
            "The provider rate limit was reached. Try again shortly.",
        )
    if "credit balance" in text:  # Anthropic reports no-credit as 400
        return AppError(
            429, "QUOTA_EXCEEDED", "Your provider quota is exhausted."
        )
    if s == 400 and (
        "context" in text or "too long" in text or "too many tokens" in text
    ):
        return AppError(
            400,
            "CONTEXT_TOO_LONG",
            "The task is too long for the selected model.",
        )
    if s == 404 or (
        "model" in text and ("not found" in text or "does not exist" in text)
    ):
        return AppError(
            404, "MODEL_UNAVAILABLE", "The selected model is not available."
        )
    if s == 403:
        return AppError(
            403,
            "MODEL_ACCESS_DENIED",
            "Your key doesn't have access to this model.",
        )
    if 400 <= s < 500:  # the request itself was refused; retrying won't help
        return AppError(
            400,
            "PROVIDER_REJECTED",
            "The provider can't run this request on the selected model.",
        )
    return AppError(
        502,
        "PROVIDER_UNAVAILABLE",
        "The provider is unavailable. Try again later.",
    )


def _call(method, url, **kw):
    try:
        resp = _client.request(method, url, **kw)
    except httpx.HTTPError:
        raise AppError(
            502,
            "PROVIDER_UNAVAILABLE",
            "The provider is unavailable. Try again later.",
        )
    if resp.status_code >= 400:
        raise map_provider_error(resp)
    try:
        return resp.json()
    except ValueError:
        raise AppError(
            502,
            "PROVIDER_UNAVAILABLE",
            "The provider is unavailable. Try again later.",
        )


class OpenAICompatible:
    """OpenAI and Mistral share the same models + chat-completions API."""

    def __init__(self, base_url):
        self.base_url = base_url

    def _h(self, key):
        return {"Authorization": f"Bearer {key}"}

    def list_models(self, key):
        data = _call("GET", f"{self.base_url}/models", headers=self._h(key))[
            "data"
        ]
        return sorted(
            (
                {"id": m["id"], "context_window": m.get("max_context_length")}
                for m in data
            ),
            key=lambda m: m["id"],
        )

    def execute(self, key, model, system, task):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": task},
            ],
        }
        d = _call(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self._h(key),
            json=body,
        )
        u = d.get("usage") or {}
        return (
            d["choices"][0]["message"]["content"],
            u.get("prompt_tokens"),
            u.get("completion_tokens"),
        )


class Anthropic:
    base_url = "https://api.anthropic.com/v1"

    def _h(self, key):
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}

    def list_models(self, key):
        data = _call(
            "GET", f"{self.base_url}/models?limit=1000", headers=self._h(key)
        )["data"]
        return sorted(
            (
                {"id": m["id"], "context_window": m.get("max_input_tokens")}
                for m in data
            ),
            key=lambda m: m["id"],
        )

    def execute(self, key, model, system, task):
        body = {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": task}],
        }
        d = _call(
            "POST", f"{self.base_url}/messages", headers=self._h(key), json=body
        )
        text = "".join(
            b.get("text", "") for b in d["content"] if b["type"] == "text"
        )
        u = d.get("usage") or {}
        return text, u.get("input_tokens"), u.get("output_tokens")


# ponytail: name heuristic for Gemini models that can't return text; switch to a
# capability field if Google adds one
AUDIO_ONLY = re.compile(r"tts|native-audio|transcribe|-live|live-|lyria")


class Gemini:
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def _h(self, key):
        return {
            "x-goog-api-key": key
        }  # header, not ?key=, so the key never lands in URLs/logs

    def list_models(self, key):
        data = _call(
            "GET", f"{self.base_url}/models?pageSize=1000", headers=self._h(key)
        )
        return sorted(
            (
                {
                    "id": m["name"].removeprefix("models/"),
                    "context_window": m.get("inputTokenLimit"),
                }
                for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and not AUDIO_ONLY.search(m["name"])
            ),
            key=lambda m: m["id"],
        )

    def execute(self, key, model, system, task):
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": task}]}],
        }
        d = _call(
            "POST",
            f"{self.base_url}/models/{model}:generateContent",
            headers=self._h(key),
            json=body,
        )
        parts = (
            (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        )
        u = d.get("usageMetadata") or {}
        return (
            "".join(p.get("text", "") for p in parts),
            u.get("promptTokenCount"),
            u.get("candidatesTokenCount"),
        )


# Models with a free API tier, matched against the full model id. Model-list
# APIs don't expose pricing,
# so this is hand-kept. Only documented free models are listed: an unknown id is
# treated as paid.
# Gemini: ai.google.dev/gemini-api/docs/pricing (checked 2026-10-15). Anthropic
# and OpenAI have no free API tier.
GEMINI_FREE_TIER = (
    r"gemma-.+"
    r"|gemini-2\.5-(pro|flash|flash-lite|flash-preview-tts|"
    r"flash-native-audio-preview-12-2025)"
    r"|gemini-3-flash-preview"
    r"|gemini-3\.1-(flash-lite|flash-live-preview|flash-tts-preview)"
    r"|gemini-3\.5-(flash-lite|transcribe|transcribe-live|"
    r"live-translate-preview)"
    r"|gemini-3\.6-flash"
    r"|gemini-3\.8-(flash|flash-tts|flash-lite-tts|live|live-extended-thinking)"
    r"|gemini-robotics-er-2-(preview|streaming-preview)"
)

# id -> name, base_url, key format check, adapter, free_tier (regex or None)
PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "key_format": r"^sk-[A-Za-z0-9_\-]{20,}$",
        "adapter": OpenAICompatible("https://api.openai.com/v1"),
        "free_tier": None,
    },
    "anthropic": {
        "name": "Anthropic (Claude)",
        "base_url": Anthropic.base_url,
        "key_format": r"^sk-ant-[A-Za-z0-9_\-]{20,}$",
        "adapter": Anthropic(),
        "free_tier": None,
    },
    "gemini": {
        "name": "Google (Gemini)",
        "base_url": Gemini.base_url,
        # classic and newer AQ. keys
        "key_format": r"^(AIza[A-Za-z0-9_\-]{30,}|AQ\.[A-Za-z0-9_.\-]{30,})$",
        "adapter": Gemini(),
        "free_tier": GEMINI_FREE_TIER,
    },
    "mistral": {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "key_format": r"^[A-Za-z0-9]{24,}$",
        "adapter": OpenAICompatible("https://api.mistral.ai/v1"),
        "free_tier": None,  # not catalogued
    },
}


def get(provider_id):
    p = PROVIDERS.get(provider_id)
    if not p:
        raise AppError(404, "PROVIDER_NOT_FOUND", "Unknown provider.")
    return p


def tag_free_tier(provider_id, models):
    """Adds free_tier: bool to each model from the provider's hand-kept free
    list.
    """
    pattern = get(provider_id)["free_tier"]
    for m in models:
        m["free_tier"] = bool(pattern and re.fullmatch(pattern, m["id"]))
    return models


def check_key_format(provider_id, key):
    if not key or not key.strip():
        raise AppError(400, "VALIDATION_ERROR", "API key is required.")
    if not re.match(get(provider_id)["key_format"], key.strip()):
        raise AppError(
            400,
            "VALIDATION_ERROR",
            "The API key format is not valid for this provider.",
        )


def call(provider_id, op, *args):
    """Run an adapter op; a malformed provider response becomes a 502, not a
    500.
    """
    try:
        return getattr(get(provider_id)["adapter"], op)(*args)
    except (KeyError, IndexError, TypeError, AttributeError):
        raise AppError(
            502,
            "PROVIDER_UNAVAILABLE",
            "The provider returned an unexpected response.",
        )
