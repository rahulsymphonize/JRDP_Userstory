"""Caller identity: OIDC bearer token verified against the IdP's JWKS. User id = `sub` claim."""
from functools import lru_cache

import jwt
from fastapi import Depends, Header

from backend import config
from backend.errors import AppError


@lru_cache(maxsize=1)
def _jwks():
    return jwt.PyJWKClient(config.OIDC_JWKS_URL, cache_keys=True, lifespan=3600, timeout=10)


def verify(token):
    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=config.OIDC_ALGORITHMS, audience=config.OIDC_AUDIENCE,
            issuer=config.OIDC_ISSUER, options={"require": ["exp", "iss", "aud", "sub"]}, leeway=30,
        )
    except jwt.PyJWKClientConnectionError:
        raise AppError(503, "AUTH_UNAVAILABLE", "Authentication is temporarily unavailable.")
    except jwt.PyJWTError:
        raise AppError(401, "UNAUTHENTICATED", "Authentication required.")
    sub = claims["sub"]
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise AppError(401, "UNAUTHENTICATED", "Authentication required.")
    return sub


def is_admin(user):
    return user in config.ADMIN_USERS


def current_user(authorization: str = Header(None), x_user_id: str = Header(None)):
    if config.AUTH_MODE == "dev":  # config.validate() forbids this in production
        if x_user_id and x_user_id.strip() and len(x_user_id) <= 255:
            return x_user_id.strip()
        raise AppError(401, "UNAUTHENTICATED", "Authentication required.")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AppError(401, "UNAUTHENTICATED", "Authentication required.")
    return verify(token.strip())


def require_admin(user=Depends(current_user)):
    if not is_admin(user):
        raise AppError(403, "FORBIDDEN", "Unauthorized access.")
    return user
