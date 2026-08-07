"""ORM model layer for the machine-to-machine compute & tool exchange."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import ASSET_ID
from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class Role(str, enum.Enum):
    admin = "admin"
    provider = "provider"
    consumer = "consumer"
    agent = "agent"


class JobStatus(str, enum.Enum):
    quoted = "quoted"
    awaiting_payment = "awaiting_payment"
    queued = "queued"
    running = "running"
    verifying = "verifying"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    refunded = "refunded"
    disputed = "disputed"


TERMINAL_JOB_STATUSES = {
    JobStatus.succeeded,
    JobStatus.failed,
    JobStatus.cancelled,
    JobStatus.refunded,
}


class PaymentStatus(str, enum.Enum):
    required = "required"
    verified = "verified"
    escrowed = "escrowed"
    settled = "settled"
    refunded = "refunded"
    partially_refunded = "partially_refunded"
    failed = "failed"
    expired = "expired"


class DisputeStatus(str, enum.Enum):
    open = "open"
    under_review = "under_review"
    resolved_consumer = "resolved_consumer"
    resolved_provider = "resolved_provider"
    withdrawn = "withdrawn"


class WorkerStatus(str, enum.Enum):
    provisioning = "provisioning"
    running = "running"
    exited = "exited"
    reaped = "reaped"
    failed = "failed"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Identity & money
# --------------------------------------------------------------------------- #
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.consumer, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    wallet_address: Mapped[str] = mapped_column(String(120), default="")
    payment_secret: Mapped[str] = mapped_column(String(80), default="")

    providers: Mapped[list["Provider"]] = relationship(back_populates="owner")
    account: Mapped["Account"] = relationship(back_populates="user", uselist=False)


class ApiKey(Base, TimestampMixin):
    """Machine credential — only the SHA-256 digest is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("key"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="default")
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Account(Base, TimestampMixin):
    """Micro-unit ledger account (1 unit of the payment asset = 1_000_000 micros)."""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("acct"))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    available_micros: Mapped[int] = mapped_column(Integer, default=0)
    escrow_micros: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_earned_micros: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_spent_micros: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(back_populates="account")


class LedgerEntry(Base, TimestampMixin):
    """Append-only double-sided ledger movement."""

    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("led"))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # hold|capture|release|credit|debit|fee
    amount_micros: Mapped[int] = mapped_column(Integer)
    balance_after_micros: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    memo: Mapped[str] = mapped_column(String(255), default="")


# --------------------------------------------------------------------------- #
# Marketplace
# --------------------------------------------------------------------------- #
class Provider(Base, TimestampMixin):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("prv"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    endpoint_url: Mapped[str] = mapped_column(String(400), default="")
    payout_address: Mapped[str] = mapped_column(String(120), default="")
    # Every provider on this exchange is paid in the mandated ASA; the column
    # makes that advertisement explicit in listings, cards and receipts.
    payment_asset_id: Mapped[int] = mapped_column(Integer, default=ASSET_ID, index=True)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    reputation_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    total_jobs: Mapped[int] = mapped_column(Integer, default=0)
    successful_jobs: Mapped[int] = mapped_column(Integer, default=0)
    failed_jobs: Mapped[int] = mapped_column(Integer, default=0)
    disputes_lost: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    owner: Mapped[User] = relationship(back_populates="providers")
    services: Mapped[list["Service"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class Service(Base, TimestampMixin):
    """A priced, callable unit of compute or tool access."""

    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("provider_id", "slug", name="uq_service_slug"),)

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("svc"))
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(60), default="compute", index=True)
    runtime: Mapped[str] = mapped_column(String(40), default="python")  # python|bash|node|http
    entrypoint: Mapped[str] = mapped_column(Text, default="")           # code or remote path
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)

    # pricing, in micro-units of the exchange payment asset
    base_price_micros: Mapped[int] = mapped_column(Integer, default=1000)
    price_per_cpu_second_micros: Mapped[int] = mapped_column(Integer, default=500)
    price_per_mb_egress_micros: Mapped[int] = mapped_column(Integer, default=10)
    max_price_micros: Mapped[int] = mapped_column(Integer, default=1_000_000)

    # SLA
    max_runtime_seconds: Mapped[int] = mapped_column(Integer, default=60)
    memory_mb: Mapped[int] = mapped_column(Integer, default=512)
    concurrency_limit: Mapped[int] = mapped_column(Integer, default=8)
    network_access: Mapped[bool] = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    bazaar_listed: Mapped[bool] = mapped_column(Boolean, default=False)
    invocations: Mapped[int] = mapped_column(Integer, default=0)

    provider: Mapped[Provider] = relationship(back_populates="services")


class BazaarListing(Base, TimestampMixin):
    """Cached discovery record (local registry + GoPlausible Bazaar federation)."""

    __tablename__ = "bazaar_listings"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("bzr"))
    source: Mapped[str] = mapped_column(String(32), index=True)  # local | goplausible
    resource: Mapped[str] = mapped_column(String(500), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    network: Mapped[str] = mapped_column(String(60), default="", index=True)
    asset: Mapped[str] = mapped_column(String(60), default="")
    # 0 when a federated listing quotes something we cannot parse as an ASA id.
    asset_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    price_micros: Mapped[int] = mapped_column(Integer, default=0)
    pay_to: Mapped[str] = mapped_column(String(160), default="")
    accepts: Mapped[list] = mapped_column(JSON, default=list)
    service_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    reputation_score: Mapped[float] = mapped_column(Float, default=50.0)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


Index("ix_bazaar_source_resource", BazaarListing.source, BazaarListing.resource, unique=True)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("job"))
    consumer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.quoted, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")

    quoted_price_micros: Mapped[int] = mapped_column(Integer, default=0)
    max_price_micros: Mapped[int] = mapped_column(Integer, default=0)
    final_price_micros: Mapped[int] = mapped_column(Integer, default=0)
    platform_fee_micros: Mapped[int] = mapped_column(Integer, default=0)

    input_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    output_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    integrity_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.created_at"
    )
    usage: Mapped["UsageRecord | None"] = relationship(back_populates="job", uselist=False)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("evt"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    job: Mapped[Job] = relationship(back_populates="events")


class UsageRecord(Base, TimestampMixin):
    """Metering output for one job execution."""

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("use"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), unique=True)
    cpu_ms: Mapped[int] = mapped_column(Integer, default=0)
    wall_ms: Mapped[int] = mapped_column(Integer, default=0)
    peak_memory_mb: Mapped[float] = mapped_column(Float, default=0.0)
    egress_bytes: Mapped[int] = mapped_column(Integer, default=0)
    invocations: Mapped[int] = mapped_column(Integer, default=1)
    exit_code: Mapped[int] = mapped_column(Integer, default=0)
    computed_price_micros: Mapped[int] = mapped_column(Integer, default=0)
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="usage")


class Worker(Base, TimestampMixin):
    """Ephemeral sandbox worker lifecycle record."""

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("wrk"))
    job_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    backend: Mapped[str] = mapped_column(String(24), default="subprocess")
    container_id: Mapped[str] = mapped_column(String(80), default="")
    image: Mapped[str] = mapped_column(String(160), default="")
    workspace_path: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[WorkerStatus] = mapped_column(
        Enum(WorkerStatus), default=WorkerStatus.provisioning, index=True
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    reaped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("art"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_backend: Mapped[str] = mapped_column(String(24), default="local")
    storage_key: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- #
# Payments (x402)
# --------------------------------------------------------------------------- #
class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("pay"))
    job_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    payer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    payee_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    scheme: Mapped[str] = mapped_column(String(40), default="exact")
    network: Mapped[str] = mapped_column(String(60), default="algorand-testnet")
    asset: Mapped[str] = mapped_column(String(60), default=str(ASSET_ID))
    asset_id: Mapped[int] = mapped_column(Integer, default=ASSET_ID, index=True)
    amount_micros: Mapped[int] = mapped_column(Integer, default=0)
    captured_micros: Mapped[int] = mapped_column(Integer, default=0)
    refunded_micros: Mapped[int] = mapped_column(Integer, default=0)
    fee_micros: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.required, index=True
    )
    nonce: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    resource: Mapped[str] = mapped_column(String(400), default="")
    pay_to: Mapped[str] = mapped_column(String(160), default="")
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    tx_hash: Mapped[str] = mapped_column(String(120), default="", index=True)
    settlement_backend: Mapped[str] = mapped_column(String(24), default="ledger")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requirements: Mapped[dict] = mapped_column(JSON, default=dict)


class Refund(Base, TimestampMixin):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("ref"))
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    amount_micros: Mapped[int] = mapped_column(Integer, default=0)
    asset_id: Mapped[int] = mapped_column(Integer, default=ASSET_ID, index=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    initiated_by: Mapped[str] = mapped_column(String(48), default="system")
    tx_hash: Mapped[str] = mapped_column(String(120), default="")


class Receipt(Base, TimestampMixin):
    """Tamper-evident, hash-chained settlement receipt."""

    __tablename__ = "receipts"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("rcp"))
    job_id: Mapped[str] = mapped_column(String(48), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    consumer_id: Mapped[str] = mapped_column(String(48), index=True)
    provider_id: Mapped[str] = mapped_column(String(48), index=True)
    sequence: Mapped[int] = mapped_column(Integer, index=True)
    body: Mapped[dict] = mapped_column(JSON, default=dict)
    body_hash: Mapped[str] = mapped_column(String(64), index=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="0" * 64)
    chain_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    signature: Mapped[str] = mapped_column(String(128))
    anchored_tx: Mapped[str] = mapped_column(String(120), default="")


class Dispute(Base, TimestampMixin):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("dsp"))
    job_id: Mapped[str] = mapped_column(String(48), index=True)
    receipt_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    raised_by: Mapped[str] = mapped_column(String(48), index=True)
    reason: Mapped[str] = mapped_column(String(64), default="quality")
    detail: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[DisputeStatus] = mapped_column(
        Enum(DisputeStatus), default=DisputeStatus.open, index=True
    )
    resolution: Mapped[str] = mapped_column(Text, default="")
    refund_micros: Mapped[int] = mapped_column(Integer, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class ReputationEvent(Base):
    __tablename__ = "reputation_events"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("rep"))
    provider_id: Mapped[str] = mapped_column(String(48), index=True)
    job_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    delta: Mapped[float] = mapped_column(Float, default=0.0)
    score_after: Mapped[float] = mapped_column(Float, default=50.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# --------------------------------------------------------------------------- #
# Agent orchestration & scheduling
# --------------------------------------------------------------------------- #
class Plan(Base, TimestampMixin):
    """A LangGraph agent run: goal -> steps -> jobs."""

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("pln"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="planning", index=True)
    budget_micros: Mapped[int] = mapped_column(Integer, default=0)
    spent_micros: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    engine: Mapped[str] = mapped_column(String(32), default="builtin")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Schedule(Base, TimestampMixin):
    """Recurring or one-shot job scheduling."""

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("sch"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    service_id: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cron: Mapped[str] = mapped_column(String(120), default="")
    max_price_micros: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- #
# External provider telemetry
# --------------------------------------------------------------------------- #
class ExternalRequestStatus(str, enum.Enum):
    succeeded = "succeeded"
    failed = "failed"
    quota_exceeded = "quota_exceeded"
    budget_exceeded = "budget_exceeded"
    payment_failed = "payment_failed"
    invalid_request = "invalid_request"
    unavailable = "unavailable"


class ZerionRequest(Base, TimestampMixin):
    """One request to Zerion: what it cost, how it was paid for, what came back.

    The quota ledger and the dashboard both read this table. It holds no
    credential material of any kind — only the *rail* a request was paid on and
    the settlement metadata the adapter normalized.
    """

    __tablename__ = "zerion_requests"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("zrn"))
    job_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    service_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)

    capability: Mapped[str] = mapped_column(String(48), index=True, default="")
    wallet: Mapped[str] = mapped_column(String(120), default="", index=True)
    chain: Mapped[str] = mapped_column(String(40), default="")
    transport: Mapped[str] = mapped_column(String(24), default="")   # api | cli | demo
    rail: Mapped[str] = mapped_column(String(32), default="", index=True)

    status: Mapped[ExternalRequestStatus] = mapped_column(
        Enum(ExternalRequestStatus), default=ExternalRequestStatus.succeeded, index=True
    )
    http_status: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    upstream_requests: Mapped[int] = mapped_column(Integer, default=1)

    # Micro-units: `quoted`/`charged` are the exchange's own asset; `provider_cost`
    # is what the request cost us on the provider's rail.
    quoted_micros: Mapped[int] = mapped_column(Integer, default=0)
    charged_micros: Mapped[int] = mapped_column(Integer, default=0)
    provider_cost_micros: Mapped[int] = mapped_column(Integer, default=0)

    payment_status: Mapped[str] = mapped_column(String(32), default="")
    payment_amount: Mapped[str] = mapped_column(String(32), default="0")
    payment_currency: Mapped[str] = mapped_column(String(16), default="USDC")
    payment_network: Mapped[str] = mapped_column(String(32), default="")
    payment_tx: Mapped[str] = mapped_column(String(160), default="")
    external_payment_id: Mapped[str] = mapped_column(String(64), default="", index=True)

    integrity_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    receipt_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    error_code: Mapped[str] = mapped_column(String(48), default="", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("aud"))
    actor_id: Mapped[str] = mapped_column(String(48), default="system", index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    target: Mapped[str] = mapped_column(String(120), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
