"""Authentication primitives for DropNote: password hashing + JWT + FastAPI guards.

- Passwords are hashed with bcrypt. Plaintext is never stored.
- Sessions are stateless JWTs (HS256) signed with ``AUTH_JWT_SECRET``; the app stores
  the token and sends it as ``Authorization: Bearer <jwt>`` on every protected call.
- ``require_user`` gates the token-minting endpoint; ``require_admin`` gates the
  personal-use user-creation endpoint via a static ``X-Admin-Token`` header.

All secrets come from the environment — nothing is hardcoded.
"""

from __future__ import annotations

import time

import bcrypt
import jwt
from fastapi import Header, HTTPException, status

import env_util

JWT_ALGORITHM = "HS256"

# bcrypt only considers the first 72 bytes of a password; longer inputs raise in
# bcrypt 5.x, so truncate deterministically on both hash and verify.
_BCRYPT_MAX_BYTES = 72


def _pw_bytes(password: str) -> bytes:
    return (password or "").encode("utf-8")[:_BCRYPT_MAX_BYTES]


def _jwt_secret() -> str:
    secret = env_util.get("AUTH_JWT_SECRET")
    if not secret:
        raise RuntimeError("AUTH_JWT_SECRET is not set — refusing to sign/verify tokens.")
    return secret


def _token_ttl_seconds() -> int:
    hours = float(env_util.get("AUTH_TOKEN_TTL_HOURS", "720"))  # default 30 days
    return int(hours * 3600)


# ---- password hashing ----------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(password), password_hash.encode("utf-8"))
    except Exception:
        return False


# ---- JWT -----------------------------------------------------------------------


def create_access_token(email: str) -> str:
    now = int(time.time())
    payload = {"sub": email, "iat": now, "exp": now + _token_ttl_seconds()}
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> str:
    """Return the subject (email) of a valid token, or raise jwt exceptions."""
    payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("token has no subject")
    return sub


# ---- FastAPI dependencies ------------------------------------------------------


async def require_user(authorization: str = Header(default="")) -> str:
    """Validate the Bearer token and return the authenticated email, else 401."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_token(token.strip())
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_admin(x_admin_token: str = Header(default="")) -> None:
    """Gate the user-creation endpoint with a static admin secret (personal use)."""
    expected = env_util.get("ADMIN_SECRET")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server missing ADMIN_SECRET.",
        )
    if not x_admin_token or x_admin_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token.",
        )
