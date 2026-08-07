"""Job lifecycle orchestration.

quote -> 402 -> pay (escrow) -> queue -> execute (sandbox) -> meter ->
verify integrity -> settle -> refund unused -> receipt -> reputation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..algorand import AssetPolicyError, asset_descriptor, check
from ..algorand import asset_id as mandated_asset_id
from ..algorand import is_mandated_asset, is_mandated_network
from ..config import settings
from ..integrations.registry import external_service_for
from ..integrity import hash_object, sha256_hex, verify_manifest
from ..metering import Usage, price_for_usage, quote_service
from ..models import (
    Artifact,
    Dispute,
    DisputeStatus,
    Job,
    JobEvent,
    JobStatus,
    Payment,
    PaymentStatus,
    Provider,
    Receipt,
    Refund,
    Service,
    UsageRecord,
    User,
    Worker,
    WorkerStatus,
)
from ..observability import (
    integrity_checks,
    job_duration,
    jobs_active,
    jobs_total,
    payment_volume_micros,
    payments_total,
    refunds_total,
    workers_active,
    workers_spawned,
)
from ..storage import put_artifact
from ..workers.sandbox import cleanup_workspace, runner
from ..x402.facilitator import facilitator
from ..x402.protocol import build_requirements, new_nonce
from . import ledger, receipts, reputation

log = logging.getLogger("m2x.jobs")


class JobError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def emit(db: Session, job: Job, kind: str, message: str = "", **data) -> JobEvent:
    event = JobEvent(job_id=job.id, kind=kind, message=message, data=data)
    db.add(event)
    return event


# --------------------------------------------------------------------------- #
# Quoting & creation
# --------------------------------------------------------------------------- #
def resource_uri(service: Service) -> str:
    return f"/v1/services/{service.id}/invoke"


def quote(db: Session, service: Service, payload: dict) -> dict:
    breakdown = quote_service(service, payload)
    provider = db.get(Provider, service.provider_id)
    return {
        "service_id": service.id,
        "service_slug": service.slug,
        "provider_id": service.provider_id,
        "provider_slug": provider.slug if provider else "",
        "runtime": service.runtime,
        "max_price_micros": breakdown.capped_micros,
        "estimated": breakdown.as_dict(),
        "input_hash": hash_object(payload),
        "sla_seconds": service.max_runtime_seconds,
        "reputation": provider.reputation_score if provider else 0.0,
        # Prices are only meaningful alongside the asset they are denominated in.
        "asset_id": mandated_asset_id(),
        "payment_asset": asset_descriptor(),
        "expires_at": (utcnow() + timedelta(seconds=settings.x402_escrow_timeout_seconds)).isoformat(),
    }


def create_job(
    db: Session,
    *,
    consumer: User,
    service: Service,
    payload: dict,
    max_price_micros: int | None = None,
    idempotency_key: str | None = None,
    plan_id: str | None = None,
) -> tuple[Job, Payment, dict]:
    if not service.is_active:
        raise JobError("service is not active")
    provider = db.get(Provider, service.provider_id)
    if provider is None or not provider.is_active:
        raise JobError("provider is not active")

    if idempotency_key:
        existing = db.scalar(
            select(Job).where(
                Job.idempotency_key == idempotency_key, Job.consumer_id == consumer.id
            )
        )
        if existing:
            payment = db.scalar(select(Payment).where(Payment.job_id == existing.id))
            return existing, payment, quote(db, service, existing.payload)

    # An externally-executed service gets to reject an unusable payload here,
    # before a quote is committed and before any payment record exists — so a
    # malformed request to a paid third party costs the consumer nothing.
    external = external_service_for(db, service)
    if external is not None and external.validate is not None:
        try:
            external.validate(payload)
        except ValueError as exc:
            raise JobError(str(exc))

    q = quote(db, service, payload)
    ceiling = max_price_micros or q["max_price_micros"]
    if ceiling < q["max_price_micros"]:
        raise JobError(
            f"max_price_micros {ceiling} below quote {q['max_price_micros']}"
        )

    job = Job(
        consumer_id=consumer.id,
        service_id=service.id,
        provider_id=service.provider_id,
        plan_id=plan_id,
        status=JobStatus.awaiting_payment,
        payload=payload,
        quoted_price_micros=q["max_price_micros"],
        max_price_micros=ceiling,
        input_hash=q["input_hash"],
        idempotency_key=idempotency_key,
        deadline_at=utcnow() + timedelta(seconds=service.max_runtime_seconds * 4 + 60),
    )
    db.add(job)
    db.flush()

    payment = Payment(
        job_id=job.id,
        payer_id=consumer.id,
        payee_id=provider.owner_id,
        amount_micros=q["max_price_micros"],
        network=settings.x402_network,
        asset=settings.x402_asset,
        asset_id=mandated_asset_id(),
        nonce=new_nonce(),
        resource=resource_uri(service),
        pay_to=provider.payout_address or provider.owner_id,
        status=PaymentStatus.required,
        expires_at=utcnow() + timedelta(seconds=settings.x402_escrow_timeout_seconds),
    )
    reqs = build_requirements(
        amount_micros=payment.amount_micros,
        resource=payment.resource,
        description=f"{service.name} by {provider.name}",
        pay_to=payment.pay_to,
        nonce=payment.nonce,
        job_id=job.id,
        output_schema=service.output_schema or None,
    )
    payment.requirements = reqs.as_dict()
    db.add(payment)
    db.flush()

    emit(db, job, "created", "job created, awaiting x402 payment",
         quote_micros=job.quoted_price_micros, payment_id=payment.id)
    payments_total.labels("required", payment.network).inc()
    return job, payment, q


def payment_requirements_for(payment: Payment):
    from ..x402.protocol import PaymentRequirements

    return PaymentRequirements(**payment.requirements)


# --------------------------------------------------------------------------- #
# Pre-execution validation
# --------------------------------------------------------------------------- #
def preflight(db: Session, job: Job, payment: Payment | None) -> dict:
    """The gate every compute job passes through before a sandbox is spawned.

    Reports on each mandated condition and says whether the job may run. The
    asset check is the hard one: a job whose money is not denominated in the
    mandated ASA is rejected outright, never executed and never settled.

    ``settlement_success`` is reported as ``pending`` here — settlement happens
    after execution, and is re-verified in :func:`_settle_success` once the
    facilitator returns.
    """
    checks: list[dict] = []
    provider = db.get(Provider, job.provider_id)

    network = payment.network if payment else settings.x402_network
    checks.append(check("algorand_network", is_mandated_network(network),
                        f"{network} (expected {settings.x402_network})"))

    asset = (payment.asset_id or payment.asset) if payment else mandated_asset_id()
    asset_ok = is_mandated_asset(asset)
    checks.append(check("asset_id", asset_ok,
                        f"{asset} (expected {mandated_asset_id()})"))

    if payment is None:
        checks.append(check("payment_authorization", False, "no payment record for this job"))
        checks.append(check("buyer_balance", False, "unknown without a payment"))
        checks.append(check("seller_address", bool(provider and provider.payout_address),
                            (provider.payout_address if provider else "") or "unset"))
    else:
        authorized = payment.status in (PaymentStatus.verified, PaymentStatus.escrowed,
                                        PaymentStatus.settled)
        checks.append(check("payment_authorization", authorized, payment.status.value))

        acct = ledger.get_account(db, payment.payer_id)
        held = payment.status in (PaymentStatus.escrowed, PaymentStatus.settled)
        funded = acct.escrow_micros >= payment.amount_micros if held else (
            acct.available_micros >= payment.amount_micros
        )
        checks.append(check(
            "buyer_balance", funded,
            f"available={acct.available_micros} escrow={acct.escrow_micros} "
            f"required={payment.amount_micros}",
        ))

        checks.append(check("seller_address", bool(payment.pay_to), payment.pay_to or "unset"))

    checks.append(check("settlement_success", True, "pending — verified after execution"))

    failed = [c for c in checks if not c["ok"] and c["check"] != "settlement_success"]
    return {
        "job_id": job.id,
        "payment_id": payment.id if payment else None,
        "asset": asset_descriptor(),
        "checks": checks,
        "ok": not failed,
        "failed": [c["check"] for c in failed],
    }


def assert_ready_to_execute(db: Session, job: Job, payment: Payment | None) -> dict:
    """Raise unless the job satisfies every pre-execution condition."""
    report = preflight(db, job, payment)
    if not report["ok"]:
        detail = "; ".join(
            f"{c['check']}: {c['detail']}" for c in report["checks"]
            if not c["ok"] and c["check"] != "settlement_success"
        )
        raise JobError(f"pre-execution validation failed ({detail})")
    return report


# --------------------------------------------------------------------------- #
# Payment
# --------------------------------------------------------------------------- #
def apply_payment(db: Session, job: Job, payment: Payment, x_payment_header: str) -> dict:
    """Verify an X-PAYMENT header and move funds into escrow."""
    from ..x402.protocol import decode_payment_header

    if payment.status in (PaymentStatus.escrowed, PaymentStatus.settled):
        return {"status": payment.status.value, "already": True}
    if payment.expires_at and _aware(payment.expires_at) < utcnow():
        payment.status = PaymentStatus.expired
        raise JobError("payment window expired")

    payload = decode_payment_header(x_payment_header)
    requirements = payment_requirements_for(payment)
    result = facilitator.verify(db, payload, requirements, payment)
    if not result.is_valid:
        payment.status = PaymentStatus.failed
        payments_total.labels("rejected", payment.network).inc()
        emit(db, job, "payment_rejected", result.invalid_reason)
        raise JobError(f"payment verification failed: {result.invalid_reason}")

    payment.status = PaymentStatus.verified
    facilitator.escrow(db, payment)
    job.status = JobStatus.queued
    jobs_active.inc()
    payments_total.labels("escrowed", payment.network).inc()
    emit(db, job, "payment_escrowed", "funds held in escrow",
         amount_micros=payment.amount_micros, payment_id=payment.id)
    return {"status": payment.status.value, "escrowed_micros": payment.amount_micros,
            "payer": result.payer}


def autopay(db: Session, job: Job, payment: Payment, user: User) -> dict:
    """Sign and submit an x402 authorization on behalf of a delegating principal.

    Used by the scheduler and the agent, which hold a custodial signing secret
    for the account they act for. Interactive clients sign client-side instead
    and send the ``X-PAYMENT`` header themselves.
    """
    from ..x402.protocol import build_exact_payload, encode_payment_header

    payload = build_exact_payload(
        payer=user.id,
        pay_to=payment.pay_to,
        value_micros=payment.amount_micros,
        nonce=payment.nonce,
        resource=payment.resource,
        payer_secret=user.payment_secret,
        asset=payment.asset_id or None,
    )
    return apply_payment(db, job, payment, encode_payment_header(payload))


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def execute_job(db: Session, job: Job) -> Job:
    service = db.get(Service, job.service_id)
    provider = db.get(Provider, job.provider_id)
    payment = db.scalar(select(Payment).where(Payment.job_id == job.id))

    if job.status not in (JobStatus.queued, JobStatus.running):
        raise JobError(f"job {job.id} is not runnable (status={job.status.value})")

    # No sandbox is spawned until the money behind the job is confirmed to be
    # the mandated Algorand asset on the mandated network.
    try:
        validation = assert_ready_to_execute(db, job, payment)
    except JobError as exc:
        return _reject_job(db, job, payment, reason=str(exc))
    emit(db, job, "validated", "pre-execution validation passed",
         asset_id=mandated_asset_id(), network=settings.x402_network,
         checks=validation["checks"])

    # An external service runs at the provider, not in a container. Everything
    # after this branch — metering, integrity, artifacts, settlement, receipt,
    # reputation — is identical, because both paths return an ExecutionResult.
    external = external_service_for(db, service)
    backend_label = f"external:{external.provider_slug}" if external else runner.backend

    job.status = JobStatus.running
    job.attempts += 1
    job.started_at = utcnow()
    emit(db, job, "started", f"attempt {job.attempts}/{job.max_attempts}",
         backend=backend_label)

    worker: Worker | None = None
    if external is None:
        worker = Worker(
            job_id=job.id,
            backend=runner.backend,
            image=settings.sandbox_image_python if service.runtime != "node" else settings.sandbox_image_node,
            status=WorkerStatus.running,
            expires_at=utcnow() + timedelta(seconds=settings.worker_ttl_seconds),
        )
        db.add(worker)
        db.flush()
        workers_spawned.labels(runner.backend).inc()
        workers_active.inc()

        execution = runner.run(
            job_id=job.id,
            runtime=service.runtime,
            code=service.entrypoint,
            payload=job.payload,
            timeout_seconds=service.max_runtime_seconds,
            memory_mb=service.memory_mb,
            network=service.network_access,
        )

        worker.status = WorkerStatus.exited
        worker.exit_code = execution.exit_code
        worker.workspace_path = execution.workspace
        workers_active.dec()
    else:
        emit(db, job, "external_call",
             f"delegating to external provider {external.provider_slug}",
             provider=external.provider_slug, capability=external.capability,
             no_worker_provisioned=True)
        execution = external.executor(db, job, service)

    # ---- metering ---------------------------------------------------------
    usage = _record_usage(db, job, service, execution.usage)
    job_duration.labels(service.runtime).observe(execution.usage.wall_ms / 1000.0)

    # ---- integrity --------------------------------------------------------
    output_doc = {"result": execution.result, "stdout_sha256": sha256_hex(execution.stdout),
                  "manifest": execution.manifest}
    job.output_hash = hash_object(output_doc)
    ok_manifest, problems = verify_manifest(
        execution.manifest, {**execution.artifacts, "stdout.txt": execution.stdout.encode()}
    )
    job.integrity_verified = ok_manifest
    integrity_checks.labels("pass" if ok_manifest else "fail").inc()
    emit(db, job, "integrity_checked",
         "sha256 manifest verified" if ok_manifest else f"integrity problems: {problems}",
         output_hash=job.output_hash, manifest_root=execution.manifest.get("root"))

    # ---- artifacts --------------------------------------------------------
    _persist_artifacts(db, job, execution)

    # ---- outcome ----------------------------------------------------------
    job.finished_at = utcnow()
    if execution.ok and ok_manifest:
        job.status = JobStatus.succeeded
        job.result = {
            "output": execution.result,
            "stdout": execution.stdout[-8000:],
            "manifest": execution.manifest,
            "backend": execution.backend,
        }
        job.error = ""
        service.invocations += 1
        emit(db, job, "succeeded", "execution complete")
    else:
        job.status = JobStatus.failed
        job.error = execution.error or (execution.stderr[-2000:] or "unknown failure")
        job.result = {"stdout": execution.stdout[-4000:], "stderr": execution.stderr[-4000:]}
        emit(db, job, "failed", job.error, exit_code=execution.exit_code,
             timed_out=execution.timed_out)

    # ---- money ------------------------------------------------------------
    if job.status == JobStatus.succeeded:
        _settle_success(db, job, service, payment, usage, execution.manifest,
                        external=_external_receipt_block(execution, external))
    else:
        _refund_failure(db, job, payment, reason=job.error[:120] or "execution failed")
        # A rejected input or an exhausted quota does not get better on a second
        # attempt, and re-running it would spend the consumer's money again.
        if job.attempts < job.max_attempts and execution.retryable:
            _schedule_retry(db, job)

    reputation.on_job_finished(db, provider, job, sla_seconds=service.max_runtime_seconds)
    jobs_total.labels(job.status.value).inc()
    if job.status in (JobStatus.succeeded, JobStatus.failed):
        try:
            jobs_active.dec()
        except Exception:
            pass

    cleanup_workspace(job.id)
    if worker is not None:
        worker.status = WorkerStatus.reaped
        worker.reaped_at = utcnow()
    db.flush()
    return job


def _external_receipt_block(execution, external) -> dict | None:
    """The provider-side facts a receipt for an external job must state.

    Carries the second payment rail, its settlement status and the integrity
    hash of the normalized provider response — never a credential, because the
    executor never produced one.
    """
    if external is None:
        return None
    output = execution.result if isinstance(execution.result, dict) else {}
    payment = output.get("payment") or {}
    integrity = output.get("integrity") or {}
    return {
        "provider": output.get("provider") or external.provider_slug,
        "capability": output.get("request_type") or external.capability,
        "transport": execution.backend,
        "source": output.get("source", ""),
        "requested_at": output.get("timestamp", ""),
        "payment": {
            "rail": payment.get("rail", ""),
            "status": payment.get("status", ""),
            "settled": bool(payment.get("settled")),
            "amount": payment.get("amount", "0"),
            "currency": payment.get("currency", ""),
            "network": payment.get("network", ""),
            "transaction": payment.get("transaction", ""),
        },
        "integrity": {
            "algorithm": integrity.get("algorithm", "sha256"),
            "response_hash": integrity.get("hash", ""),
            "scope": integrity.get("scope", ""),
            "note": integrity.get("note", ""),
        },
    }


def _reject_job(db: Session, job: Job, payment: Payment | None, *, reason: str) -> Job:
    """Refuse a job that failed validation: refund the escrow, run nothing."""
    job.status = JobStatus.failed
    job.error = reason
    job.finished_at = utcnow()
    emit(db, job, "validation_failed", reason,
         asset_id=mandated_asset_id(), network=settings.x402_network)
    _refund_failure(db, job, payment, reason=reason[:200])
    jobs_total.labels("rejected").inc()
    try:
        jobs_active.dec()
    except Exception:
        pass
    db.flush()
    return job


def _record_usage(db: Session, job: Job, service: Service, usage: Usage) -> UsageRecord:
    breakdown = price_for_usage(service, usage)
    record = db.scalar(select(UsageRecord).where(UsageRecord.job_id == job.id))
    if record is None:
        record = UsageRecord(job_id=job.id)
        db.add(record)
    record.cpu_ms = usage.cpu_ms
    record.wall_ms = usage.wall_ms
    record.peak_memory_mb = usage.peak_memory_mb
    record.egress_bytes = usage.egress_bytes
    record.invocations = usage.invocations
    record.exit_code = usage.exit_code
    record.computed_price_micros = min(breakdown.capped_micros, job.max_price_micros)
    record.breakdown = breakdown.as_dict()
    db.flush()
    emit(db, job, "metered", "usage metered", **usage.as_dict(),
         price_micros=record.computed_price_micros)
    return record


def _persist_artifacts(db: Session, job: Job, execution) -> None:
    entries = {e["name"]: e for e in execution.manifest.get("entries", [])}
    for name, blob in execution.artifacts.items():
        stored = put_artifact(job.id, name, blob, "application/octet-stream")
        db.add(
            Artifact(
                job_id=job.id,
                name=name,
                size_bytes=stored.size,
                sha256=stored.sha256,
                storage_backend=stored.backend,
                storage_key=stored.key,
                expires_at=utcnow() + timedelta(seconds=settings.artifact_ttl_seconds),
            )
        )
        if name in entries and entries[name]["sha256"] != stored.sha256:
            emit(db, job, "artifact_mismatch", f"{name} hash changed on write")
    db.flush()


def _settle_success(db: Session, job: Job, service: Service, payment: Payment | None,
                    usage: UsageRecord, manifest: dict, external: dict | None = None) -> None:
    charged = min(usage.computed_price_micros, job.max_price_micros)
    fee = usage.breakdown.get("platform_fee_micros", 0)
    fee = min(fee, charged)
    job.final_price_micros = charged
    job.platform_fee_micros = fee

    if payment is not None:
        unused = max(payment.amount_micros - charged, 0)
        try:
            result = facilitator.settle(db, payment, charged, fee)
        except AssetPolicyError as exc:
            # The asset changed under us between escrow and capture: keep the
            # money with the buyer rather than settling in something else.
            _reject_job(db, job, payment, reason=f"settlement refused: {exc}")
            return
        # "Settlement Success" half of the validation checklist, verified now
        # that the facilitator has answered.
        settled_ok = result.success and is_mandated_asset(result.asset_id)
        payment_volume_micros.inc(charged)
        payments_total.labels("settled" if settled_ok else "settle_failed", payment.network).inc()
        emit(db, job, "settled", f"charged {charged} micros",
             tx_hash=result.transaction, backend=result.backend,
             asset_id=result.asset_id, network=result.network,
             settlement_verified=settled_ok)
        if unused > 0:
            ledger.release(db, payment.payer_id, unused, job_id=job.id,
                           payment_id=payment.id, memo="unused escrow returned")
            db.add(Refund(payment_id=payment.id, job_id=job.id, amount_micros=unused,
                          asset_id=payment.asset_id or mandated_asset_id(),
                          reason="unused_escrow", initiated_by="system"))
            refunds_total.labels("unused_escrow").inc()
            emit(db, job, "refunded", f"returned {unused} unused micros", amount_micros=unused,
                 asset_id=payment.asset_id or mandated_asset_id())

    receipt = receipts.issue_receipt(db, job, payment, usage, service, manifest=manifest,
                                     external=external)
    emit(db, job, "receipt_issued", f"receipt #{receipt.sequence}",
         receipt_id=receipt.id, chain_hash=receipt.chain_hash)
    if external:
        _link_external_receipt(db, job, receipt.id)


def _link_external_receipt(db: Session, job: Job, receipt_id: str) -> None:
    """Point this job's external-provider request rows at the receipt covering them."""
    from ..models import ZerionRequest

    for row in db.scalars(select(ZerionRequest).where(ZerionRequest.job_id == job.id)).all():
        row.receipt_id = receipt_id


def _refund_failure(db: Session, job: Job, payment: Payment | None, *, reason: str) -> None:
    if payment is None or payment.status not in (PaymentStatus.escrowed, PaymentStatus.verified):
        return
    amount = payment.amount_micros
    tx = facilitator.refund(db, payment, amount, reason)
    db.add(Refund(payment_id=payment.id, job_id=job.id, amount_micros=amount,
                  asset_id=payment.asset_id or mandated_asset_id(),
                  reason=reason[:200], initiated_by="system", tx_hash=tx))
    refunds_total.labels("job_failed").inc()
    payments_total.labels("refunded", payment.network).inc()
    emit(db, job, "refunded", f"full refund: {reason}", amount_micros=amount, tx_hash=tx,
         asset_id=payment.asset_id or mandated_asset_id())


def _schedule_retry(db: Session, job: Job) -> None:
    delay = min(2 ** job.attempts, 60)
    job.next_attempt_at = utcnow() + timedelta(seconds=delay)
    emit(db, job, "retry_scheduled", f"retry #{job.attempts + 1} in {delay}s",
         next_attempt_at=job.next_attempt_at.isoformat())


def prepare_retry(db: Session, job: Job) -> Payment | None:
    """Re-quote and re-open payment for a retryable failed job."""
    service = db.get(Service, job.service_id)
    provider = db.get(Provider, job.provider_id)
    payment = Payment(
        job_id=job.id,
        payer_id=job.consumer_id,
        payee_id=provider.owner_id,
        amount_micros=job.quoted_price_micros,
        network=settings.x402_network,
        asset=settings.x402_asset,
        asset_id=mandated_asset_id(),
        nonce=new_nonce(),
        resource=resource_uri(service),
        pay_to=provider.payout_address or provider.owner_id,
        status=PaymentStatus.required,
        expires_at=utcnow() + timedelta(seconds=settings.x402_escrow_timeout_seconds),
    )
    reqs = build_requirements(
        amount_micros=payment.amount_micros,
        resource=payment.resource,
        description=f"retry {job.attempts + 1} of {service.name}",
        pay_to=payment.pay_to,
        nonce=payment.nonce,
        job_id=job.id,
    )
    payment.requirements = reqs.as_dict()
    db.add(payment)
    job.status = JobStatus.awaiting_payment
    job.next_attempt_at = None
    db.flush()
    emit(db, job, "retry_ready", "new payment required for retry", payment_id=payment.id)
    return payment


def cancel_job(db: Session, job: Job, reason: str = "cancelled by consumer") -> Job:
    if job.status in (JobStatus.succeeded, JobStatus.refunded, JobStatus.cancelled):
        raise JobError(f"cannot cancel a {job.status.value} job")
    payment = db.scalar(select(Payment).where(Payment.job_id == job.id))
    _refund_failure(db, job, payment, reason=reason)
    job.status = JobStatus.cancelled
    job.finished_at = utcnow()
    emit(db, job, "cancelled", reason)
    jobs_total.labels("cancelled").inc()
    cleanup_workspace(job.id)
    return job


# --------------------------------------------------------------------------- #
# Disputes
# --------------------------------------------------------------------------- #
def open_dispute(db: Session, job: Job, user: User, reason: str, detail: str,
                 evidence: dict | None = None) -> Dispute:
    if job.consumer_id != user.id:
        raise JobError("only the consumer can dispute this job")
    age = (utcnow() - _aware(job.created_at)).total_seconds()
    if age > settings.dispute_window_seconds:
        raise JobError("dispute window has closed")
    if db.scalar(select(Dispute).where(Dispute.job_id == job.id,
                                       Dispute.status.in_([DisputeStatus.open,
                                                           DisputeStatus.under_review]))):
        raise JobError("a dispute is already open for this job")

    receipt = db.scalar(select(Receipt).where(Receipt.job_id == job.id))
    dispute = Dispute(
        job_id=job.id,
        receipt_id=receipt.id if receipt else None,
        raised_by=user.id,
        reason=reason,
        detail=detail,
        # Snapshot the disputed facts: opening a dispute moves the job into
        # `disputed`, which would otherwise erase the outcome being contested.
        evidence={
            **(evidence or {}),
            "_job_status_at_open": job.status.value,
            "_final_price_micros": job.final_price_micros,
            "_integrity_verified": job.integrity_verified,
        },
    )
    db.add(dispute)
    job.status = JobStatus.disputed
    provider = db.get(Provider, job.provider_id)
    reputation.record_event(db, provider, "dispute_opened", job_id=job.id)
    emit(db, job, "dispute_opened", reason, dispute_id=dispute.id)
    db.flush()
    return dispute


def auto_triage(db: Session, dispute: Dispute) -> Dispute:
    """Evidence-based automatic resolution; ambiguous cases go to review."""
    job = db.get(Job, dispute.job_id)
    receipt = db.get(Receipt, dispute.receipt_id) if dispute.receipt_id else None

    verdict_consumer = False
    rationale = []
    status_at_open = dispute.evidence.get("_job_status_at_open", job.status.value)

    if not job.integrity_verified:
        verdict_consumer = True
        rationale.append("output failed sha256 integrity verification")
    if receipt is not None:
        report = receipts.verify_receipt(receipt)
        if not report["valid"]:
            verdict_consumer = True
            rationale.append("receipt signature/hash chain invalid")
    if status_at_open in (JobStatus.failed.value, JobStatus.cancelled.value):
        verdict_consumer = True
        rationale.append("job did not complete successfully")
    if job.final_price_micros > job.max_price_micros:
        verdict_consumer = True
        rationale.append("charged above the accepted quote")

    if verdict_consumer:
        return resolve_dispute(db, dispute, in_favor_of_consumer=True,
                               resolution="; ".join(rationale), auto=True)
    dispute.status = DisputeStatus.under_review
    dispute.resolution = "no automatic evidence of provider fault; escalated to review"
    db.flush()
    return dispute


def resolve_dispute(db: Session, dispute: Dispute, *, in_favor_of_consumer: bool,
                    resolution: str, refund_micros: int | None = None, auto: bool = False) -> Dispute:
    job = db.get(Job, dispute.job_id)
    payment = db.scalar(select(Payment).where(Payment.job_id == job.id))
    provider = db.get(Provider, job.provider_id)

    if in_favor_of_consumer and payment is not None:
        # Only money the platform still holds can be returned: a job that already
        # failed was refunded at execution time and owes nothing further.
        held = payment.captured_micros if payment.captured_micros else payment.amount_micros
        outstanding = max(held - payment.refunded_micros, 0)
        amount = min(refund_micros if refund_micros is not None else outstanding, outstanding)
        if amount > 0:
            tx = facilitator.refund(db, payment, amount, f"dispute:{dispute.id}")
            db.add(Refund(payment_id=payment.id, job_id=job.id, amount_micros=amount,
                          asset_id=payment.asset_id or mandated_asset_id(),
                          reason=f"dispute:{dispute.reason}", initiated_by=dispute.raised_by,
                          tx_hash=tx))
            dispute.refund_micros = amount
            refunds_total.labels("dispute").inc()
            job.status = JobStatus.refunded
            emit(db, job, "dispute_refund", f"refunded {amount} micros", tx_hash=tx)

    dispute.status = (DisputeStatus.resolved_consumer if in_favor_of_consumer
                      else DisputeStatus.resolved_provider)
    dispute.resolution = resolution
    dispute.resolved_at = utcnow()
    dispute.auto_resolved = auto
    reputation.on_dispute_resolved(db, provider, in_favor_of_consumer, job.id)
    if not in_favor_of_consumer and job.status == JobStatus.disputed:
        job.status = JobStatus.succeeded
    emit(db, job, "dispute_resolved", resolution,
         in_favor_of_consumer=in_favor_of_consumer, auto=auto)
    db.flush()
    return dispute
