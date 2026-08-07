"""Registration, login, API keys, wallet balance."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..cache import rate_limit
from ..config import settings
from ..db import get_db
from ..models import ApiKey, AuditLog, Role, User
from ..schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    BalanceOut,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    TopUpRequest,
    UserOut,
)
from ..security import (
    CurrentUser,
    create_token,
    decode_token,
    generate_api_key,
    hash_password,
    verify_password,
)
from ..services import ledger

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_token(user.id, user.role.value, "access"),
        refresh_token=create_token(user.id, user.role.value, "refresh"),
        expires_in=settings.access_token_ttl_seconds,
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    allowed, _ = rate_limit(f"register:{body.email}", limit=5, window_seconds=3600)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many registration attempts")
    if db.scalar(select(User).where(User.email == str(body.email))):
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = User(
        email=str(body.email),
        display_name=body.display_name or str(body.email).split("@")[0],
        password_hash=hash_password(body.password),
        role=Role(body.role),
        wallet_address=f"M2X{secrets.token_hex(20).upper()}",
        payment_secret=secrets.token_urlsafe(32),
    )
    db.add(user)
    db.flush()
    ledger.credit(db, user.id, settings.signup_grant_micros, memo="testnet signup grant")
    db.add(AuditLog(actor_id=user.id, action="user.register", target=user.id,
                    data={"role": user.role.value}))
    db.flush()
    return _tokens(user)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    allowed, _ = rate_limit(f"login:{body.email}", limit=10, window_seconds=300)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts")
    user = db.scalar(select(User).where(User.email == str(body.email)))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account disabled")
    return _tokens(user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    payload = decode_token(body.refresh_token, expected_kind="refresh")
    user = db.get(User, payload["sub"])
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown principal")
    return _tokens(user)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/balance", response_model=BalanceOut)
def balance(user: CurrentUser, db: Session = Depends(get_db)) -> BalanceOut:
    return BalanceOut(**ledger.balance_summary(db, user.id))


@router.post("/topup", response_model=BalanceOut)
def topup(body: TopUpRequest, user: CurrentUser, db: Session = Depends(get_db)) -> BalanceOut:
    """Testnet faucet — credits the caller's ledger account."""
    allowed, _ = rate_limit(f"topup:{user.id}", limit=20, window_seconds=3600)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "faucet rate limit reached")
    ledger.credit(db, user.id, body.amount_micros, memo="testnet faucet")
    db.add(AuditLog(actor_id=user.id, action="wallet.topup", target=user.id,
                    data={"amount_micros": body.amount_micros}))
    return BalanceOut(**ledger.balance_summary(db, user.id))


@router.get("/payment-secret")
def payment_secret(user: CurrentUser) -> dict:
    """The signing key this principal uses to authorize x402 payments client-side."""
    return {"payer": user.id, "payment_secret": user.payment_secret,
            "wallet_address": user.wallet_address,
            "note": "sign x402 authorizations with HMAC-SHA256 over canonical JSON"}


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_keys(user: CurrentUser, db: Session = Depends(get_db)) -> list[ApiKeyOut]:
    rows = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id)).all()
    return [ApiKeyOut.model_validate(r) for r in rows]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
def create_key(body: ApiKeyCreate, user: CurrentUser, db: Session = Depends(get_db)) -> ApiKeyCreated:
    full, prefix, digest = generate_api_key()
    row = ApiKey(user_id=user.id, name=body.name, prefix=prefix, key_hash=digest,
                 scopes=body.scopes)
    db.add(row)
    db.flush()
    # `key` is returned exactly once; only its SHA-256 digest is persisted.
    return ApiKeyCreated(**ApiKeyOut.model_validate(row).model_dump(), key=full)


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    row = db.get(ApiKey, key_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    row.revoked = True
