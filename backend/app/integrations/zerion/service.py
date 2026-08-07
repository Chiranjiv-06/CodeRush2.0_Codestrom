"""Zerion request orchestration.

One function does the whole provider-side sequence, in a fixed order, with no
way to skip a step:

    validate -> quota -> budget -> preauthorize payment -> call Zerion ->
    normalize -> settle/describe payment -> integrity hash -> record

:func:`run_request` is what every caller uses — the job executor, the REST
router, the MCP tool. It never raises for an expected failure: it records the
attempt (so quotas count it) and returns a structured outcome, which is what
lets the job lifecycle refund and receipt a failed Zerion call exactly like a
failed sandbox job.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...integrity import build_manifest, canonical_json
from ...metering import Usage
from ...models import ExternalRequestStatus, ZerionRequest, new_id
from ...observability import (
    zerion_latency,
    zerion_payments,
    zerion_requests,
    zerion_spend_micros,
)
from ...payments.rails import PaymentOutcome, PaymentRail
from ...workers.sandbox import ExecutionResult
from . import quota as quota_service
from .cli import cli_client
from .client import ZerionRawResult, client as api_client
from .demo import demo_client
from .errors import (
    ZerionBudgetError,
    ZerionDisabledError,
    ZerionError,
    ZerionQuotaError,
    ZerionValidationError,
    sanitize,
)
from .models import PROVIDER_ID, ZerionRequestSpec, capability_for
from .normalizer import normalize, summary_line
from .payment import adapter, mode_report, transport_name

log = logging.getLogger("m2x.zerion")

MAX_RAW_ARTIFACT_BYTES = 512 * 1024

_STATUS_FOR_ERROR = {
    "zerion_quota_exceeded": ExternalRequestStatus.quota_exceeded,
    "zerion_budget_exceeded": ExternalRequestStatus.budget_exceeded,
    "zerion_payment_failed": ExternalRequestStatus.payment_failed,
    "zerion_invalid_request": ExternalRequestStatus.invalid_request,
    "zerion_not_configured": ExternalRequestStatus.unavailable,
    "zerion_disabled": ExternalRequestStatus.unavailable,
    "zerion_unavailable": ExternalRequestStatus.unavailable,
}


@dataclass
class ZerionOutcome:
    """Everything one Zerion request produced, ready to store or return."""

    ok: bool
    capability: str
    transport: str
    rail: str
    envelope: dict | None = None
    raw: dict = field(default_factory=dict)
    payment: PaymentOutcome | None = None
    record_id: str = ""
    latency_ms: int = 0
    upstream_requests: int = 0
    quoted_micros: int = 0
    provider_cost_micros: int = 0
    error_code: str = ""
    error: str = ""
    retryable: bool = False
    quota: dict = field(default_factory=dict)

    @property
    def integrity_hash(self) -> str:
        return ((self.envelope or {}).get("integrity") or {}).get("hash", "")

    @property
    def summary(self) -> str:
        return summary_line(self.envelope or {}) if self.envelope else self.error

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ok": self.ok,
            "provider": PROVIDER_ID,
            "capability": self.capability,
            "transport": self.transport,
            "rail": self.rail,
            "request_id": self.record_id,
            "latency_ms": self.latency_ms,
            "upstream_requests": self.upstream_requests,
            "quoted_micros": self.quoted_micros,
            "provider_cost_micros": self.provider_cost_micros,
            "quota": self.quota,
        }
        if self.envelope:
            body["result"] = self.envelope
            body["summary"] = self.summary
            body["integrity_hash"] = self.integrity_hash
        if self.payment is not None:
            body["payment"] = self.payment.as_dict()
        if not self.ok:
            body["error"] = self.error
            body["error_code"] = self.error_code
            body["retryable"] = self.retryable
        return body


# --------------------------------------------------------------------------- #
# Transport dispatch
# --------------------------------------------------------------------------- #
def _execute_transport(spec: ZerionRequestSpec, transport: str) -> ZerionRawResult:
    if transport == "cli":
        return cli_client.execute(spec, use_x402=adapter.rail is PaymentRail.ZERION_X402)
    if transport == "api":
        return api_client.execute(spec)
    if transport == "demo":
        return demo_client.execute(spec)
    raise ZerionDisabledError(f"no Zerion transport available ({transport})")


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def _record(
    db: Session,
    *,
    request_id: str,
    spec: ZerionRequestSpec | None,
    capability: str,
    user_id: str,
    job_id: str | None,
    plan_id: str | None,
    service_id: str | None,
    status: ExternalRequestStatus,
    transport: str,
    rail: str,
    latency_ms: int = 0,
    upstream_requests: int = 0,
    quoted_micros: int = 0,
    provider_cost_micros: int = 0,
    payment: PaymentOutcome | None = None,
    integrity_hash: str = "",
    summary: str = "",
    error_code: str = "",
    error: str = "",
    meta: dict | None = None,
) -> ZerionRequest:
    """Persist one attempt. Failures are recorded too — they count against quota."""
    row = ZerionRequest(
        id=request_id,
        job_id=job_id,
        plan_id=plan_id,
        user_id=user_id or "",
        service_id=service_id,
        capability=capability,
        wallet=(spec.wallet if spec else "")[:120],
        chain=(spec.chain if spec else "")[:40],
        transport=transport,
        rail=rail,
        status=status,
        latency_ms=latency_ms,
        upstream_requests=upstream_requests,
        quoted_micros=quoted_micros,
        provider_cost_micros=provider_cost_micros,
        payment_status=(payment.status if payment else ""),
        payment_amount=(payment.amount if payment else "0"),
        payment_currency=(payment.currency if payment else "USDC"),
        payment_network=(payment.network if payment else ""),
        payment_tx=(payment.transaction if payment else "")[:160],
        external_payment_id=(payment.payment_id if payment else "")[:64],
        integrity_hash=integrity_hash,
        error_code=error_code,
        error=sanitize(error),
        summary=summary[:500],
        expires_at=quota_service.expiry(),
        meta=meta or {},
    )
    db.add(row)
    db.flush()

    zerion_requests.labels(capability, transport, status.value).inc()
    if latency_ms:
        zerion_latency.labels(capability, transport).observe(latency_ms / 1000.0)
    if payment is not None:
        zerion_payments.labels(payment.rail.value, payment.status).inc()
        if payment.settled and provider_cost_micros:
            zerion_spend_micros.inc(provider_cost_micros)
    return row


def _failure(
    db: Session,
    exc: ZerionError,
    *,
    request_id: str,
    spec: ZerionRequestSpec | None,
    capability: str,
    user_id: str,
    job_id: str | None,
    plan_id: str | None,
    service_id: str | None,
    transport: str,
    quoted_micros: int,
    payment: PaymentOutcome | None = None,
    quota: dict | None = None,
    record: bool = True,
) -> ZerionOutcome:
    status = _STATUS_FOR_ERROR.get(exc.code, ExternalRequestStatus.failed)
    if record:
        _record(
            db,
            request_id=request_id, spec=spec, capability=capability, user_id=user_id,
            job_id=job_id, plan_id=plan_id, service_id=service_id, status=status,
            transport=transport, rail=adapter.rail.value, quoted_micros=quoted_micros,
            payment=payment, error_code=exc.code, error=exc.detail,
            summary=f"Zerion request failed: {exc.code}",
            meta={"context": exc.context},
        )
    log.warning("zerion %s failed: %s (%s)", capability, exc.code, exc.detail)
    return ZerionOutcome(
        ok=False, capability=capability, transport=transport, rail=adapter.rail.value,
        payment=payment, record_id=request_id if record else "",
        quoted_micros=quoted_micros, error_code=exc.code, error=exc.detail,
        retryable=exc.retryable, quota=quota or {},
    )


# --------------------------------------------------------------------------- #
# The orchestrated request
# --------------------------------------------------------------------------- #
def run_request(
    db: Session,
    *,
    capability: str,
    payload: dict | None = None,
    user_id: str = "",
    job_id: str | None = None,
    plan_id: str | None = None,
    service_id: str | None = None,
    budget_micros: int | None = None,
    price_micros: int | None = None,
    record: bool = True,
) -> ZerionOutcome:
    """Discover-to-receipt provider leg for one Zerion capability."""
    request_id = new_id("zrn")
    transport = transport_name()
    spec: ZerionRequestSpec | None = None
    cap_key = str(capability)

    def fail(exc: ZerionError, *, payment: PaymentOutcome | None = None,
             quota: dict | None = None) -> ZerionOutcome:
        return _failure(
            db, exc, request_id=request_id, spec=spec, capability=cap_key, user_id=user_id,
            job_id=job_id, plan_id=plan_id, service_id=service_id, transport=transport,
            quoted_micros=quoted, payment=payment, quota=quota, record=record,
        )

    quoted = 0

    # 1. availability -------------------------------------------------------
    if not settings.zerion_enabled:
        return fail(ZerionDisabledError("the Zerion integration is disabled (ZERION_ENABLED=false)"))

    # 2. validation ---------------------------------------------------------
    try:
        capability_obj = capability_for(capability)
        cap_key = capability_obj.key
        spec = ZerionRequestSpec.from_payload(cap_key, payload)
    except ZerionValidationError as exc:
        return fail(exc)

    quoted = price_micros if price_micros is not None else capability_obj.price_micros
    provider_cost = capability_obj.cost_micros

    # 3 + 4. quota and budget, before anything is authorized -----------------
    try:
        quota_state = quota_service.enforce(
            db,
            user_id=user_id,
            job_id=job_id,
            cost_micros=settings.zerion_cost_micros,
            upstream_requests=spec.upstream_requests,
            budget_micros=budget_micros,
            price_micros=quoted,
        )
    except (ZerionQuotaError, ZerionBudgetError) as exc:
        return fail(exc)

    # 5. payment authorization ---------------------------------------------
    try:
        adapter.preauthorize(capability=cap_key, upstream_requests=spec.upstream_requests)
    except ZerionError as exc:
        return fail(exc, quota=quota_state)

    # 6. call Zerion --------------------------------------------------------
    started = time.perf_counter()
    try:
        raw = _execute_transport(spec, transport)
    except ZerionError as exc:
        failed_payment = adapter.finalize(
            request_id=request_id, capability=cap_key, transport=transport,
            upstream_requests=spec.upstream_requests, succeeded=False,
            evidence={"error": exc.detail},
        )
        return fail(exc, payment=failed_payment, quota=quota_state)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("unexpected zerion transport failure")
        return fail(ZerionError(f"unexpected provider failure: {sanitize(exc)}"),
                    quota=quota_state)

    latency_ms = raw.latency_ms or int((time.perf_counter() - started) * 1000)

    # 7. settle / describe the provider-side payment ------------------------
    payment = adapter.finalize(
        request_id=request_id,
        capability=cap_key,
        transport=transport,
        upstream_requests=raw.upstream_requests or spec.upstream_requests,
        succeeded=True,
        evidence=raw.payment_evidence,
    )

    # 8. normalize + hash ---------------------------------------------------
    envelope = normalize(
        spec, raw.payloads, source=raw.source, payment=payment.as_dict(),
        warnings=raw.warnings, include_raw=False,
    )

    settled_cost = provider_cost if payment.settled else 0

    # 9. record -------------------------------------------------------------
    row_id = ""
    if record:
        row = _record(
            db,
            request_id=request_id, spec=spec, capability=cap_key, user_id=user_id,
            job_id=job_id, plan_id=plan_id, service_id=service_id,
            status=ExternalRequestStatus.succeeded, transport=transport,
            rail=payment.rail.value, latency_ms=latency_ms,
            upstream_requests=raw.upstream_requests or spec.upstream_requests,
            quoted_micros=quoted, provider_cost_micros=settled_cost, payment=payment,
            integrity_hash=(envelope.get("integrity") or {}).get("hash", ""),
            summary=summary_line(envelope),
            meta={
                "source": raw.source,
                "http_status": raw.http_status,
                "warnings": raw.warnings[:4],
                "request": spec.as_dict(),
            },
        )
        row_id = row.id

    return ZerionOutcome(
        ok=True, capability=cap_key, transport=transport, rail=payment.rail.value,
        envelope=envelope, raw=raw.payloads, payment=payment, record_id=row_id or request_id,
        latency_ms=latency_ms,
        upstream_requests=raw.upstream_requests or spec.upstream_requests,
        quoted_micros=quoted, provider_cost_micros=settled_cost,
        quota=quota_state,
    )


# --------------------------------------------------------------------------- #
# Job executor
# --------------------------------------------------------------------------- #
def _artifacts(outcome: ZerionOutcome) -> dict[str, bytes]:
    files = {"zerion_response.json": canonical_json(outcome.envelope or {}).encode("utf-8")}
    raw_bytes = canonical_json(outcome.raw).encode("utf-8")
    if outcome.raw and len(raw_bytes) <= MAX_RAW_ARTIFACT_BYTES:
        # The untouched provider document, kept alongside the normalized one so
        # an audit can always compare the two.
        files["zerion_raw.json"] = raw_bytes
    return files


def _execution(outcome: ZerionOutcome, *, stdout: str, artifacts: dict[str, bytes]) -> ExecutionResult:
    manifest = build_manifest({**artifacts, "stdout.txt": stdout.encode("utf-8")})
    return ExecutionResult(
        ok=outcome.ok,
        exit_code=0 if outcome.ok else 1,
        stdout=stdout,
        stderr="" if outcome.ok else f"{outcome.error_code}: {outcome.error}",
        result=outcome.envelope if outcome.ok else {"error": outcome.error_code,
                                                    "detail": outcome.error},
        usage=Usage(
            cpu_ms=0,                       # no compute is billed: this is a data call
            wall_ms=outcome.latency_ms,
            peak_memory_mb=0.0,
            egress_bytes=len(stdout.encode("utf-8"))
            + sum(len(v) for v in artifacts.values()),
            invocations=max(outcome.upstream_requests, 1),
            exit_code=0 if outcome.ok else 1,
        ),
        artifacts=artifacts,
        manifest=manifest,
        backend=f"zerion:{outcome.transport}",
        workspace="",
        timed_out=outcome.error_code == "zerion_timeout",
        error="" if outcome.ok else f"{outcome.error_code}: {outcome.error}",
        retryable=outcome.retryable,
    )


def execute_zerion_job(db: Session, job: Any, service: Any) -> ExecutionResult:
    """Run a Zerion capability as a job, in the shape a sandbox worker returns.

    Returning rather than raising is deliberate: the caller's existing failure
    path already refunds escrow, records the event and issues reputation, and a
    provider outage should travel that path like any other failed job.
    """
    from .registration import capability_for_service

    payload = dict(job.payload or {})
    capability = capability_for_service(service, payload)

    outcome = run_request(
        db,
        capability=capability,
        payload=payload,
        user_id=job.consumer_id,
        job_id=job.id,
        plan_id=job.plan_id,
        service_id=service.id,
        price_micros=job.quoted_price_micros or None,
        budget_micros=job.max_price_micros or None,
    )

    stdout = outcome.summary or (outcome.error or "no result")
    artifacts = _artifacts(outcome) if outcome.ok else {}
    return _execution(outcome, stdout=stdout, artifacts=artifacts)


def status_report(db: Session | None = None, *, user_id: str = "") -> dict[str, Any]:
    """Provider status for the router, the dashboard and /v1/config."""
    from .registration import capability_catalog

    report: dict[str, Any] = {
        **mode_report(),
        "name": "Zerion Onchain Intelligence",
        "capabilities": capability_catalog(),
        "cost_micros_per_request": settings.zerion_cost_micros,
        "price_micros_per_request": settings.zerion_price_micros,
        "consumer_rail": PaymentRail.M2X_ALGORAND.value,
    }
    if db is not None and user_id:
        report["quota"] = quota_service.usage(db, user_id=user_id)
    return report
