"""Signed, hash-chained settlement receipts."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..algorand import asset_descriptor
from ..algorand import asset_id as mandated_asset_id
from ..config import settings
from ..integrity import ZERO_HASH, canonical_json, chain_hash, hash_object, sign, verify_signature
from ..models import Job, Payment, Receipt, Service, UsageRecord


def _latest(db: Session) -> Receipt | None:
    return db.scalar(select(Receipt).order_by(Receipt.sequence.desc()).limit(1))


def issue_receipt(
    db: Session,
    job: Job,
    payment: Payment | None,
    usage: UsageRecord | None,
    service: Service,
    *,
    manifest: dict | None = None,
    external: dict | None = None,
) -> Receipt:
    prev = _latest(db)
    sequence = (prev.sequence + 1) if prev else 1
    prev_hash = prev.chain_hash if prev else ZERO_HASH

    body = {
        "version": 1,
        "receipt_type": "settlement",
        "job": {
            "id": job.id,
            "status": job.status.value,
            "service_id": job.service_id,
            "service_slug": service.slug,
            "provider_id": job.provider_id,
            "consumer_id": job.consumer_id,
            "attempts": job.attempts,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        },
        "integrity": {
            "algorithm": "sha256",
            "input_hash": job.input_hash,
            "output_hash": job.output_hash,
            "manifest_root": (manifest or {}).get("root", ZERO_HASH),
            "artifact_count": (manifest or {}).get("count", 0),
            "verified": job.integrity_verified,
        },
        "metering": {
            "cpu_ms": usage.cpu_ms if usage else 0,
            "wall_ms": usage.wall_ms if usage else 0,
            "egress_bytes": usage.egress_bytes if usage else 0,
            "invocations": usage.invocations if usage else 0,
            "breakdown": usage.breakdown if usage else {},
        },
        # Every receipt states, on its face, which chain and which asset the
        # money moved on, the transaction it moved in, and whether it settled.
        "payment": {
            "id": payment.id if payment else None,
            "scheme": payment.scheme if payment else None,
            "blockchain": settings.blockchain,
            "network": settings.network_label,
            "x402_network": payment.network if payment else settings.x402_network,
            "asset_id": (payment.asset_id or mandated_asset_id()) if payment else mandated_asset_id(),
            "asset": payment.asset if payment else settings.x402_asset,
            "asset_display": settings.asset_display,
            "transaction_id": payment.tx_hash if payment else "",
            "settlement_status": payment.status.value if payment else "unpaid",
            "quoted_micros": job.quoted_price_micros,
            "charged_micros": job.final_price_micros,
            "platform_fee_micros": job.platform_fee_micros,
            "refunded_micros": payment.refunded_micros if payment else 0,
            "tx_hash": payment.tx_hash if payment else "",
            "settlement_backend": payment.settlement_backend if payment else "",
        },
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "sequence": sequence,
    }

    # A job served by an external provider states the *second* leg on its face
    # too: which rail the exchange paid the provider on, whether that settled,
    # and the hash of the response it paid for. Absent for ordinary jobs, so
    # every receipt written before this existed still verifies unchanged.
    if external:
        body["external_provider"] = external

    body_hash = hash_object(body)
    ch = chain_hash(prev_hash, body_hash)
    receipt = Receipt(
        job_id=job.id,
        payment_id=payment.id if payment else None,
        consumer_id=job.consumer_id,
        provider_id=job.provider_id,
        sequence=sequence,
        body=body,
        body_hash=body_hash,
        prev_hash=prev_hash,
        chain_hash=ch,
        signature=sign(ch),
    )
    db.add(receipt)
    db.flush()
    return receipt


def verify_receipt(receipt: Receipt) -> dict:
    body_hash = hash_object(receipt.body)
    expected_chain = chain_hash(receipt.prev_hash, body_hash)
    checks = {
        "body_hash_matches": body_hash == receipt.body_hash,
        "chain_hash_matches": expected_chain == receipt.chain_hash,
        "signature_valid": verify_signature(receipt.chain_hash, receipt.signature),
    }
    return {
        "receipt_id": receipt.id,
        "sequence": receipt.sequence,
        "valid": all(checks.values()),
        "checks": checks,
        "computed_body_hash": body_hash,
        "canonical_length": len(canonical_json(receipt.body)),
    }


def verify_chain(db: Session, limit: int = 1000) -> dict:
    receipts = list(
        db.scalars(select(Receipt).order_by(Receipt.sequence).limit(limit)).all()
    )
    broken: list[dict] = []
    prev_hash = ZERO_HASH
    for r in receipts:
        report = verify_receipt(r)
        link_ok = r.prev_hash == prev_hash
        if not report["valid"] or not link_ok:
            broken.append({"sequence": r.sequence, "receipt_id": r.id,
                           "link_ok": link_ok, **report["checks"]})
        prev_hash = r.chain_hash
    return {
        "receipts_checked": len(receipts),
        "chain_valid": not broken,
        "head": prev_hash,
        "broken": broken,
    }


def receipt_stats(db: Session) -> dict:
    total = db.scalar(select(func.count(Receipt.id))) or 0
    head = _latest(db)
    return {
        "total": total,
        "head_sequence": head.sequence if head else 0,
        "head_hash": head.chain_hash if head else ZERO_HASH,
        "asset": asset_descriptor(),
    }
