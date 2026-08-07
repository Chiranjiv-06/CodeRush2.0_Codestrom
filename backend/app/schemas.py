"""Pydantic request/response contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .algorand import asset_descriptor
from .algorand import asset_id as mandated_asset_id

# Permissive on purpose: machine operators routinely use internal domains
# (``ops@cluster.local``) that strict deliverability checks reject.
EmailStr = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=5,
        max_length=255,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$",
    ),
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PaymentAssetOut(BaseModel):
    """The payment asset, in the shape every card and receipt renders."""

    blockchain: str
    network: str
    asset_id: int
    asset: str
    unit_name: str
    decimals: int
    display: str
    label: str = "Payment Asset"


def _payment_asset() -> PaymentAssetOut:
    return PaymentAssetOut(**{k: v for k, v in asset_descriptor().items()
                              if k in PaymentAssetOut.model_fields})


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=120)
    role: str = Field(default="consumer", pattern="^(consumer|provider|agent)$")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    id: str
    email: str
    display_name: str
    role: str
    wallet_address: str
    created_at: datetime

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v):
        return getattr(v, "value", v)


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", max_length=120)
    scopes: list[str] = Field(default_factory=list)


class ApiKeyOut(ORMModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    revoked: bool
    created_at: datetime
    last_used_at: datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    key: str


# --------------------------------------------------------------------------- #
# Marketplace
# --------------------------------------------------------------------------- #
class ProviderCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{2,63}$")
    name: str = Field(max_length=160)
    description: str = ""
    endpoint_url: str = ""
    payout_address: str = ""
    regions: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    payment_asset_id: int | None = Field(
        default=None,
        description="Optional; registration is refused unless it is the exchange's payment asset.",
    )

    @field_validator("payment_asset_id")
    @classmethod
    def _mandated_asset(cls, v: int | None) -> int | None:
        if v is not None and v != mandated_asset_id():
            raise ValueError(
                f"providers must accept asset {mandated_asset_id()}; {v} is not supported"
            )
        return v


class ProviderUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    endpoint_url: str | None = None
    payout_address: str | None = None
    regions: list[str] | None = None
    capabilities: list[str] | None = None
    is_active: bool | None = None


class ProviderOut(ORMModel):
    id: str
    owner_id: str
    slug: str
    name: str
    description: str
    endpoint_url: str
    payout_address: str
    payment_asset_id: int
    payment_asset: PaymentAssetOut = Field(default_factory=_payment_asset)
    regions: list[str]
    capabilities: list[str]
    is_active: bool
    is_verified: bool
    reputation_score: float
    total_jobs: int
    successful_jobs: int
    failed_jobs: int
    disputes_lost: int
    avg_latency_ms: float
    created_at: datetime


class ServiceCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{2,63}$")
    name: str = Field(max_length=160)
    description: str = ""
    category: str = "compute"
    runtime: str = Field(default="python", pattern="^(python|bash|node)$")
    entrypoint: str = Field(min_length=1)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    base_price_micros: int = Field(default=1000, ge=0, le=10_000_000)
    price_per_cpu_second_micros: int = Field(default=500, ge=0, le=10_000_000)
    price_per_mb_egress_micros: int = Field(default=10, ge=0, le=1_000_000)
    max_price_micros: int = Field(default=1_000_000, ge=1, le=100_000_000)
    max_runtime_seconds: int = Field(default=60, ge=1, le=900)
    memory_mb: int = Field(default=512, ge=64, le=8192)
    concurrency_limit: int = Field(default=8, ge=1, le=256)
    network_access: bool = False


class ServiceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    entrypoint: str | None = None
    tags: list[str] | None = None
    base_price_micros: int | None = None
    price_per_cpu_second_micros: int | None = None
    price_per_mb_egress_micros: int | None = None
    max_price_micros: int | None = None
    max_runtime_seconds: int | None = None
    memory_mb: int | None = None
    is_active: bool | None = None


class ServiceOut(ORMModel):
    id: str
    provider_id: str
    slug: str
    name: str
    description: str
    category: str
    runtime: str
    tags: list[str]
    input_schema: dict
    output_schema: dict
    base_price_micros: int
    price_per_cpu_second_micros: int
    price_per_mb_egress_micros: int
    max_price_micros: int
    # Prices above are micro-units of this asset.
    payment_asset: PaymentAssetOut = Field(default_factory=_payment_asset)
    max_runtime_seconds: int
    memory_mb: int
    network_access: bool
    is_active: bool
    source_hash: str
    invocations: int
    created_at: datetime


# --------------------------------------------------------------------------- #
# Jobs & payments
# --------------------------------------------------------------------------- #
class QuoteRequest(BaseModel):
    service_id: str
    payload: dict = Field(default_factory=dict)


class JobCreate(BaseModel):
    service_id: str
    payload: dict = Field(default_factory=dict)
    max_price_micros: int | None = None
    idempotency_key: str | None = Field(default=None, max_length=120)
    auto_pay: bool = Field(
        default=False,
        description="Sign the x402 authorization server-side with the caller's custodial key.",
    )


class JobOut(ORMModel):
    id: str
    consumer_id: str
    provider_id: str
    service_id: str
    plan_id: str | None
    status: str
    payload: dict
    result: dict | None
    error: str
    quoted_price_micros: int
    max_price_micros: int
    final_price_micros: int
    platform_fee_micros: int
    input_hash: str
    output_hash: str
    integrity_verified: bool
    attempts: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        return getattr(v, "value", v)


class JobEventOut(ORMModel):
    id: str
    kind: str
    message: str
    data: dict
    created_at: datetime


class PaymentOut(ORMModel):
    id: str
    job_id: str | None
    scheme: str
    network: str
    asset: str
    asset_id: int
    payment_asset: PaymentAssetOut = Field(default_factory=_payment_asset)
    amount_micros: int
    captured_micros: int
    refunded_micros: int
    fee_micros: int
    status: str
    nonce: str
    resource: str
    pay_to: str
    tx_hash: str
    settlement_backend: str
    requirements: dict
    created_at: datetime
    settled_at: datetime | None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        return getattr(v, "value", v)


class PayRequest(BaseModel):
    x_payment: str = Field(description="base64(json(PaymentPayload)) — same value as X-PAYMENT")


class SignPaymentRequest(BaseModel):
    """Test/SDK helper: server-side signing for the caller's own custodial key."""

    payment_id: str


# --------------------------------------------------------------------------- #
# Disputes, receipts, reputation
# --------------------------------------------------------------------------- #
class DisputeCreate(BaseModel):
    job_id: str
    reason: str = Field(default="quality", max_length=64)
    detail: str = ""
    evidence: dict = Field(default_factory=dict)


class DisputeResolve(BaseModel):
    in_favor_of_consumer: bool
    resolution: str = ""
    refund_micros: int | None = None


class DisputeOut(ORMModel):
    id: str
    job_id: str
    raised_by: str
    reason: str
    detail: str
    status: str
    resolution: str
    refund_micros: int
    auto_resolved: bool
    created_at: datetime
    resolved_at: datetime | None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        return getattr(v, "value", v)


class ReceiptOut(ORMModel):
    id: str
    job_id: str
    payment_id: str | None
    sequence: int
    body: dict
    body_hash: str
    prev_hash: str
    chain_hash: str
    signature: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Agent & schedules
# --------------------------------------------------------------------------- #
class PlanRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=2000)
    budget_micros: int | None = Field(default=None, ge=0, le=1_000_000_000)
    max_steps: int | None = Field(default=None, ge=1, le=25)


class PlanOut(ORMModel):
    id: str
    owner_id: str
    goal: str
    status: str
    engine: str
    budget_micros: int
    spent_micros: int
    steps: list
    trace: list
    result: dict | None
    error: str
    created_at: datetime
    finished_at: datetime | None


class ScheduleCreate(BaseModel):
    name: str = Field(default="", max_length=160)
    service_id: str
    payload: dict = Field(default_factory=dict)
    interval_seconds: int | None = Field(default=None, ge=5, le=2_592_000)
    cron: str = ""
    max_price_micros: int = 0
    enabled: bool = True


class ScheduleOut(ORMModel):
    id: str
    name: str
    service_id: str
    payload: dict
    interval_seconds: int | None
    cron: str
    max_price_micros: int
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_job_id: str | None
    run_count: int
    failure_count: int
    created_at: datetime


# --------------------------------------------------------------------------- #
# External providers (Zerion onchain intelligence)
# --------------------------------------------------------------------------- #
class ZerionQueryRequest(BaseModel):
    """Buy one Zerion capability through the normal paid job path."""

    capability: str = Field(
        default="wallet_analysis",
        description="wallet_analysis | portfolio | positions | defi_positions | pnl | "
                    "transactions | token_search | chains",
        max_length=48,
    )
    wallet: str = Field(default="", max_length=120,
                        description="EVM address, Solana address or .eth ENS name")
    chain: str = Field(default="", max_length=32)
    query: str = Field(default="", max_length=64, description="token_search only")
    currency: str = Field(default="", max_length=8)
    limit: int | None = Field(default=None, ge=1, le=100)
    max_price_micros: int | None = Field(default=None, ge=0, le=100_000_000)

    def to_payload(self) -> dict:
        body = {"capability": self.capability}
        for key in ("wallet", "chain", "query", "currency"):
            value = getattr(self, key)
            if value:
                body[key] = value
        if self.limit is not None:
            body["limit"] = self.limit
        return body


class ZerionRequestOut(ORMModel):
    """One recorded Zerion request. Contains no credential material."""

    id: str
    job_id: str | None
    plan_id: str | None
    service_id: str | None
    capability: str
    wallet: str
    chain: str
    transport: str
    rail: str
    status: str
    latency_ms: int
    upstream_requests: int
    quoted_micros: int
    provider_cost_micros: int
    payment_status: str
    payment_amount: str
    payment_currency: str
    payment_network: str
    payment_tx: str
    integrity_hash: str
    receipt_id: str | None
    error_code: str
    error: str
    summary: str
    created_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        return getattr(v, "value", v)


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
class BalanceOut(BaseModel):
    account_id: str
    available_micros: int
    escrow_micros: int
    lifetime_earned_micros: int
    lifetime_spent_micros: int


class TopUpRequest(BaseModel):
    amount_micros: int = Field(ge=1, le=1_000_000_000)


class Page(BaseModel):
    items: list[Any]
    total: int
    limit: int
    offset: int


TokenResponse.model_rebuild()
