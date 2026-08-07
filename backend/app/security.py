"""Authentication & authorization: JWT bearer tokens, API keys, RBAC."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import ApiKey, Role, User

_PBKDF2_ROUNDS = 120_000
bearer_scheme = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def create_token(subject: str, role: str, kind: str = "access", ttl: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    ttl = ttl or (
        settings.access_token_ttl_seconds
        if kind == "access"
        else settings.refresh_token_ttl_seconds
    )
    payload = {
        "sub": subject,
        "role": role,
        "typ": kind,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": secrets.token_hex(8),
        "iss": "m2x",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_kind: str = "access") -> dict:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], issuer="m2x"
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}")
    if payload.get("typ") != expected_kind:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong token type")
    return payload


# --------------------------------------------------------------------------- #
# API keys (machine-to-machine)
# --------------------------------------------------------------------------- #
def generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key, prefix, sha256_digest). Full key is shown exactly once."""
    raw = secrets.token_urlsafe(32)
    prefix = f"m2x_{secrets.token_hex(4)}"
    full = f"{prefix}.{raw}"
    return full, prefix, hashlib.sha256(full.encode()).hexdigest()


def _user_from_api_key(db: Session, key: str) -> User | None:
    digest = hashlib.sha256(key.encode()).hexdigest()
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked.is_(False)))
    if not row:
        return None
    row.last_used_at = datetime.now(timezone.utc)
    return db.get(User, row.user_id)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    db: Session = Depends(get_db),
) -> User:
    api_key = request.headers.get("X-API-Key")
    user: User | None = None

    if api_key:
        user = _user_from_api_key(db, api_key)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
    elif creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
        if token.startswith("m2x_"):
            user = _user_from_api_key(db, token)
        else:
            payload = decode_token(token)
            user = db.get(User, payload["sub"])
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown principal")
    else:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account disabled")
    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    db: Session = Depends(get_db),
) -> User | None:
    if not creds and not request.headers.get("X-API-Key"):
        return None
    try:
        return get_current_user(request, creds, db)
    except HTTPException:
        return None


def require_roles(*roles: Role):
    def _guard(user: CurrentUser) -> User:
        if user.role not in roles and user.role != Role.admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires role in {[r.value for r in roles]}",
            )
        return user

    return _guard


require_admin = require_roles(Role.admin)
require_provider = require_roles(Role.provider)
