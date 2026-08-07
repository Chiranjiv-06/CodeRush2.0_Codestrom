"""Zerion Onchain Intelligence API.

Two ways in, both of which go through the same paid path:

``POST /v1/zerion/query``
    The one-call demo/agent surface. Creates a job against the Zerion service in
    the catalog, settles the consumer's x402 escrow in the mandated ASA,
    executes the Zerion capability, verifies integrity and issues a receipt —
    then returns the normalized result together with every piece of telemetry a
    judge or an auditor would want.

``POST /v1/services/{id}/invoke``
    The canonical x402 endpoint, unchanged. Zerion capabilities are ordinary
    catalog services, so an agent that already speaks x402 buys them with no new
    client code at all.

Nothing here returns a credential: status reports say *whether* a key is
configured, never what it is.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..algorand import asset_descriptor
from ..db import get_db
from ..integrations.zerion import (
    CAPABILITIES,
    PROVIDER_ID,
    ZerionError,
    capability_catalog,
    run_request,
    status_report,
)
from ..integrations.zerion import quota as quota_service
from ..integrations.zerion.normalizer import verify_envelope
from ..integrations.zerion.registration import ensure_registered
from ..models import Job, JobStatus, Payment, Provider, Receipt, Role, Service, ZerionRequest
from ..schemas import ZerionQueryRequest, ZerionRequestOut
from ..security import CurrentUser
from ..services import jobs as job_service

log = logging.getLogger("m2x.zerion.router")

router = APIRouter(prefix="/v1/zerion", tags=["zerion"])


def _zerion_service(db: Session, capability: str) -> Service:
    entry = CAPABILITIES.get(capability)
    if entry is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown Zerion capability {capability!r}; supported: {sorted(CAPABILITIES)}",
        )
    provider = db.scalar(select(Provider).where(Provider.slug == PROVIDER_ID))
    if provider is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "the Zerion provider is not registered on this exchange")
    service = db.scalar(
        select(Service).where(Service.provider_id == provider.id, Service.slug == entry.slug)
    )
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"Zerion capability {capability!r} is not listed")
    return service


# --------------------------------------------------------------------------- #
# Discovery & status
# --------------------------------------------------------------------------- #
@router.get("/status")
def zerion_status(db: Session = Depends(get_db)) -> dict:
    """Provider status, active mode, capabilities and pricing. No secrets."""
    provider = db.scalar(select(Provider).where(Provider.slug == PROVIDER_ID))
    report = status_report()
    report["registered"] = provider is not None
    report["provider_id"] = provider.id if provider else None
    report["reputation"] = provider.reputation_score if provider else None
    report["consumer_payment_asset"] = asset_descriptor()
    return report


@router.get("/capabilities")
def zerion_capabilities(db: Session = Depends(get_db)) -> dict:
    """Every Zerion capability with price, rail, quota, schemas and service id."""
    provider = db.scalar(select(Provider).where(Provider.slug == PROVIDER_ID))
    services = {}
    if provider is not None:
        services = {
            s.slug: s.id
            for s in db.scalars(select(Service).where(Service.provider_id == provider.id)).all()
        }
    items = []
    for entry in capability_catalog():
        items.append({**entry, "service_id": services.get(entry["service_slug"])})
    return {"count": len(items), "provider": PROVIDER_ID, "items": items,
            "payment_asset": asset_descriptor()}


@router.post("/register")
def zerion_register(user: CurrentUser, db: Session = Depends(get_db)) -> dict:
    """Re-register the Zerion provider and refresh its catalog entries."""
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    from ..bazaar.discovery import publish_local_services

    report = ensure_registered(db)
    report["listings"] = publish_local_services(db)
    db.commit()
    return report


@router.get("/quota")
def zerion_quota(user: CurrentUser, db: Session = Depends(get_db),
                 job_id: str | None = None) -> dict:
    """Quota consumption for the calling principal."""
    return {
        "user_id": user.id,
        **quota_service.usage(db, user_id=user.id, job_id=job_id),
        "cost_micros_per_request": status_report()["cost_micros_per_request"],
    }


# --------------------------------------------------------------------------- #
# The paid path
# --------------------------------------------------------------------------- #
@router.post("/query")
def zerion_query(body: ZerionQueryRequest, user: CurrentUser,
                 db: Session = Depends(get_db)) -> dict:
    """Discover -> quote -> pay (x402/ASA) -> call Zerion -> verify -> receipt.

    The consumer's leg is settled on the exchange's own rail before Zerion is
    contacted; the Zerion leg is settled by the configured provider adapter. Both
    are reported.
    """
    service = _zerion_service(db, body.capability)
    payload = body.to_payload()

    try:
        job, payment, quote = job_service.create_job(
            db, consumer=user, service=service, payload=payload,
            max_price_micros=body.max_price_micros,
        )
    except job_service.JobError as exc:
        # Validation failures land here — before any money moved.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    try:
        job_service.autopay(db, job, payment, user)
    except Exception as exc:
        db.commit()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED,
                            f"could not settle the exchange-side payment: {exc}")

    job_service.execute_job(db, job)
    db.commit()

    request_row = db.scalar(
        select(ZerionRequest).where(ZerionRequest.job_id == job.id)
        .order_by(ZerionRequest.created_at.desc())
    )
    receipt = db.scalar(select(Receipt).where(Receipt.job_id == job.id))
    envelope = (job.result or {}).get("output") if job.status == JobStatus.succeeded else None

    body_out = {
        "ok": job.status == JobStatus.succeeded,
        "capability": body.capability,
        "job_id": job.id,
        "status": job.status.value,
        "result": envelope,
        "summary": ((envelope or {}).get("data") or {}).get("summary", "") if envelope else "",
        "error": job.error,
        "integrity": {
            "output_hash": job.output_hash,
            "verified": job.integrity_verified,
            "response_hash": ((envelope or {}).get("integrity") or {}).get("hash", ""),
            "note": ((envelope or {}).get("integrity") or {}).get("note", ""),
        },
        "consumer_payment": {
            "rail": "m2x_algorand",
            "payment_id": payment.id,
            "status": payment.status.value,
            "quoted_micros": quote["max_price_micros"],
            "charged_micros": job.final_price_micros,
            "refunded_micros": payment.refunded_micros,
            "tx_hash": payment.tx_hash,
            "asset": asset_descriptor(),
        },
        "provider_payment": (envelope or {}).get("payment", {}) if envelope else {},
        "receipt": {
            "id": receipt.id, "sequence": receipt.sequence, "chain_hash": receipt.chain_hash,
        } if receipt else None,
        "telemetry": {
            "request_id": request_row.id if request_row else None,
            "transport": request_row.transport if request_row else None,
            "rail": request_row.rail if request_row else None,
            "latency_ms": request_row.latency_ms if request_row else None,
            "upstream_requests": request_row.upstream_requests if request_row else None,
            "provider_cost_micros": request_row.provider_cost_micros if request_row else None,
            "error_code": request_row.error_code if request_row else None,
        },
        "quota": quota_service.usage(db, user_id=user.id, job_id=job.id),
    }
    if job.status != JobStatus.succeeded:
        # A failed provider call is a 200 with ok=false: the escrow was already
        # refunded and the caller needs the structured error, not an exception.
        body_out["refunded"] = True
    return body_out


@router.post("/preview")
def zerion_preview(body: ZerionQueryRequest, user: CurrentUser,
                   db: Session = Depends(get_db)) -> dict:
    """Price and validate a Zerion request without paying for it."""
    service = _zerion_service(db, body.capability)
    payload = body.to_payload()
    try:
        from ..integrations.zerion import validate_payload

        spec = validate_payload(body.capability, payload)
    except ZerionError as exc:
        raise HTTPException(exc.http_status, exc.as_dict())
    quote = job_service.quote(db, service, payload)
    return {
        "capability": body.capability,
        "service_id": service.id,
        "request": spec.as_dict(),
        "quote": quote,
        "quota": quota_service.usage(db, user_id=user.id),
        "provider": status_report(),
    }


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
@router.get("/requests", response_model=list[ZerionRequestOut])
def zerion_requests(user: CurrentUser, db: Session = Depends(get_db),
                    limit: int = Query(25, le=200),
                    capability: str = "") -> list[ZerionRequestOut]:
    stmt = select(ZerionRequest)
    if user.role != Role.admin:
        stmt = stmt.where(ZerionRequest.user_id == user.id)
    if capability:
        stmt = stmt.where(ZerionRequest.capability == capability)
    rows = db.scalars(stmt.order_by(ZerionRequest.created_at.desc()).limit(limit)).all()
    return [ZerionRequestOut.model_validate(r) for r in rows]


@router.get("/requests/{request_id}")
def zerion_request_detail(request_id: str, user: CurrentUser,
                          db: Session = Depends(get_db)) -> dict:
    row = db.get(ZerionRequest, request_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "zerion request not found")
    if user.role != Role.admin and row.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your request")

    job = db.get(Job, row.job_id) if row.job_id else None
    envelope = (job.result or {}).get("output") if job and job.result else None
    return {
        **ZerionRequestOut.model_validate(row).model_dump(),
        "result": envelope,
        "integrity_check": verify_envelope(envelope) if isinstance(envelope, dict) else None,
        "meta": row.meta,
    }


@router.get("/stats")
def zerion_stats(db: Session = Depends(get_db)) -> dict:
    """Exchange-wide Zerion telemetry for the dashboard."""
    from sqlalchemy import func

    rows = db.execute(
        select(ZerionRequest.status, func.count(ZerionRequest.id)).group_by(ZerionRequest.status)
    ).all()
    by_status = {getattr(r[0], "value", str(r[0])): r[1] for r in rows}
    totals = db.execute(
        select(
            func.count(ZerionRequest.id),
            func.coalesce(func.sum(ZerionRequest.provider_cost_micros), 0),
            func.coalesce(func.sum(ZerionRequest.quoted_micros), 0),
            func.coalesce(func.avg(ZerionRequest.latency_ms), 0),
        )
    ).first()
    by_capability = {
        r[0]: r[1]
        for r in db.execute(
            select(ZerionRequest.capability, func.count(ZerionRequest.id))
            .group_by(ZerionRequest.capability)
        ).all()
    }
    settled = int(
        db.scalar(
            select(func.count(ZerionRequest.id)).where(ZerionRequest.payment_status == "settled")
        ) or 0
    )
    return {
        "provider": PROVIDER_ID,
        "requests_total": int(totals[0] or 0),
        "by_status": by_status,
        "by_capability": by_capability,
        "provider_spend_micros": int(totals[1] or 0),
        "consumer_quoted_micros": int(totals[2] or 0),
        "avg_latency_ms": round(float(totals[3] or 0), 1),
        "payments_settled": settled,
        "mode": status_report(),
    }


@router.get("/verify/{job_id}")
def zerion_verify(job_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> dict:
    """Recompute the integrity hash of a stored Zerion result."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if user.role != Role.admin and job.consumer_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your job")
    envelope = (job.result or {}).get("output")
    if not isinstance(envelope, dict) or envelope.get("provider") != PROVIDER_ID:
        raise HTTPException(status.HTTP_409_CONFLICT, "this job has no Zerion result to verify")
    receipt = db.scalar(select(Receipt).where(Receipt.job_id == job.id))
    return {
        "job_id": job.id,
        "response_integrity": verify_envelope(envelope),
        "job_output_hash": job.output_hash,
        "manifest_verified": job.integrity_verified,
        "receipt": {
            "id": receipt.id,
            "sequence": receipt.sequence,
            "chain_hash": receipt.chain_hash,
            "external_provider": (receipt.body or {}).get("external_provider"),
        } if receipt else None,
    }


@router.get("/payments")
def zerion_payments(user: CurrentUser, db: Session = Depends(get_db),
                    limit: int = Query(25, le=100)) -> dict:
    """Both legs of recent Zerion jobs: consumer->exchange and exchange->Zerion."""
    stmt = select(ZerionRequest)
    if user.role != Role.admin:
        stmt = stmt.where(ZerionRequest.user_id == user.id)
    rows = db.scalars(stmt.order_by(ZerionRequest.created_at.desc()).limit(limit)).all()

    items = []
    for row in rows:
        consumer_payment = (
            db.scalar(select(Payment).where(Payment.job_id == row.job_id)) if row.job_id else None
        )
        items.append({
            "request_id": row.id,
            "capability": row.capability,
            "created_at": row.created_at.isoformat(),
            "consumer_leg": {
                "rail": "m2x_algorand",
                "status": consumer_payment.status.value if consumer_payment else "none",
                "amount_micros": consumer_payment.captured_micros if consumer_payment else 0,
                "asset_id": consumer_payment.asset_id if consumer_payment else None,
                "tx_hash": consumer_payment.tx_hash if consumer_payment else "",
            },
            "provider_leg": {
                "rail": row.rail,
                "status": row.payment_status,
                "amount": row.payment_amount,
                "currency": row.payment_currency,
                "network": row.payment_network,
                "transaction": row.payment_tx,
            },
        })
    return {"count": len(items), "items": items}
