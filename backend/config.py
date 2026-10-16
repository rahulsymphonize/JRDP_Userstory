"""Settings from environment. Production refuses to boot with an unsafe configuration."""
import os
import sys


def _env(name, default=None):
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


ENV = _env("APP_ENV", "development")  # development | production
DATABASE_URL = _env("DATABASE_URL", "sqlite:///./data/agent.db")
AUTH_MODE = _env("AUTH_MODE", "dev" if ENV != "production" else "oidc")  # oidc | dev
OIDC_ISSUER = _env("OIDC_ISSUER")
OIDC_AUDIENCE = _env("OIDC_AUDIENCE")
OIDC_JWKS_URL = _env("OIDC_JWKS_URL")
OIDC_ALGORITHMS = [a.strip() for a in _env("OIDC_ALGORITHMS", "RS256,ES256").split(",") if a.strip()]
# Key-encryption keys: "id:base64key,id:base64key". First entry encrypts new data; all decrypt.
KEKS = _env("KEKS", "")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
DB_POOL_SIZE = int(_env("DB_POOL_SIZE", "10"))
THREADPOOL_SIZE = int(_env("THREADPOOL_SIZE", "100"))  # concurrent blocking provider calls per worker
KEY_SAVES_PER_MINUTE = int(_env("KEY_SAVES_PER_MINUTE", "10"))
EXECUTIONS_PER_MINUTE = int(_env("EXECUTIONS_PER_MINUTE", "30"))


def validate():
    errors = []
    if ENV not in ("development", "production"):
        errors.append("APP_ENV must be development or production")
    if AUTH_MODE not in ("oidc", "dev"):
        errors.append("AUTH_MODE must be oidc or dev")
    if AUTH_MODE == "oidc" and not (OIDC_ISSUER and OIDC_AUDIENCE and OIDC_JWKS_URL):
        errors.append("AUTH_MODE=oidc needs OIDC_ISSUER, OIDC_AUDIENCE and OIDC_JWKS_URL")
    if not KEKS:
        errors.append("KEKS is not set (generate one with: python -m backend.manage gen-kek)")
    if ENV == "production":
        if AUTH_MODE != "oidc":
            errors.append("production requires AUTH_MODE=oidc")
        if not DATABASE_URL.startswith("postgresql"):
            errors.append("production requires a PostgreSQL DATABASE_URL")
    if errors:
        sys.exit("Refusing to start:\n  - " + "\n  - ".join(errors))
