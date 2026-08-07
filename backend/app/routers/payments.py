"""x402 payments, receipts, refunds and disputes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..algorand import asset_descriptor, configuration_report
from ..db import get_db
from ..models import Dispute, Job, Payment, Provider, Receipt, Refund, Role
from ..schemas import (
    DisputeCreate,
    DisputeOut,
    DisputeResolve,
    PaymentOut,
    ReceiptOut,
    SignPaymentRequest,
)
from ..security import CurrentUser, require_admin
from ..services import jobs as job_service
from ..services import receipts as receipt_service
from ..x402.facilitator import facilitator
from ..x402.protocol import build_exact_payload, encode_payment_header

router = APIRouter(prefix="/v1", tags=["payments"])


# --------------------------------------------------------------------------- #
# x402 metadata
# --------------------------------------------------------------------------- #
@router.get("/x402/supported")
def supported() -> dict:
    return facilitator.supported()


@router.get("/x402/asset")
def payment_asset() -> dict:
    """The one asset this exchange quotes, escrows, settles and refunds in."""
    return configuration_report()


@router.get("/payments", response_model=list[PaymentOut])
def list_payments(user: CurrentUser, db: Session = Depends(get_db),
                  limit: int = Query(50, le=200), offset: int = 0) -> list[PaymentOut]:
    stmt = select(Payment)
    if user.role != Role.admin:
        stmt = stmt.where((Payment.payer_id == user.id) | (Payment.payee_id == user.id))
    rows = db.scalars(stmt.order_by(Payment.created_at.desc()).offset(offset).limit(limit)).all()
    return [PaymentOut.model_validate(r) for r in rows]


@router.get("/payments/{payment_id}", response_model=PaymentOut)
def get_payment(payment_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> PaymentOut:
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found")
    if user.role != Role.admin and user.id not in (payment.payer_id, payment.payee_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your payment")
    return PaymentOut.model_validate(payment)


@router.post("/payments/sign")
def sign_payment(body: SignPaymentRequest, user: CurrentUser,
                 db: Session = Depends(get_db)) -> dict:
    """Produce the ``X-PAYMENT`` header for one of the caller's own payments.

    Convenience for SDKs and the dashboard; the signing material never leaves
    the caller's own account.
    """
    payment = db.get(Payment, body.payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found")
    if payment.payer_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your payment")
    payload = build_exact_payload(
        payer=user.id,
        pay_to=payment.pay_to,
        value_micros=payment.amount_micros,
        nonce=payment.nonce,
        resource=payment.resource,
        payer_secret=user.payment_secret,
        # Signs for the asset this payment was quoted in; a payment carrying
        # anything but the mandated ASA is refused here rather than signed.
        asset=payment.asset_id or None,
    )
    return {
        "payment_id": payment.id,
        "asset_id": payment.asset_id,
        "payment_asset": asset_descriptor(),
        "x_payment": encode_payment_header(payload),
        "payload": payload.as_dict(),
        "usage": "send as the X-PAYMENT request header",
    }


@router.get("/refunds")
def list_refunds(user: CurrentUser, db: Session = Depends(get_db),
                 limit: int = Query(50, le=200)) -> list[dict]:
    stmt = select(Refund).order_by(Refund.created_at.desc()).limit(limit)
    rows = db.scalars(stmt).all()
    out = []
    for r in rows:
        payment = db.get(Payment, r.payment_id)
        if user.role != Role.admin and payment and user.id not in (payment.payer_id, payment.payee_id):
            continue
        out.append({"id": r.id, "payment_id": r.payment_id, "job_id": r.job_id,
                    "amount_micros": r.amount_micros, "asset_id": r.asset_id,
                    "payment_asset": asset_descriptor(), "reason": r.reason,
                    "tx_hash": r.tx_hash, "created_at": r.created_at.isoformat()})
    return out


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #
@router.get("/receipts", response_model=list[ReceiptOut])
def list_receipts(user: CurrentUser, db: Session = Depends(get_db),
                  limit: int = Query(50, le=200), offset: int = 0) -> list[ReceiptOut]:
    stmt = select(Receipt)
    if user.role != Role.admin:
        owned = select(Provider.id).where(Provider.owner_id == user.id)
        stmt = stmt.where((Receipt.consumer_id == user.id) | (Receipt.provider_id.in_(owned)))
    rows = db.scalars(stmt.order_by(Receipt.sequence.desc()).offset(offset).limit(limit)).all()
    return [ReceiptOut.model_validate(r) for r in rows]


@router.get("/receipts/chain")
def chain_status(db: Session = Depends(get_db)) -> dict:
    return {**receipt_service.receipt_stats(db), **receipt_service.verify_chain(db)}


@router.get("/receipts/{receipt_id}", response_model=ReceiptOut)
def get_receipt(receipt_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> ReceiptOut:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "receipt not found")
    return ReceiptOut.model_validate(receipt)


@router.get("/receipts/{receipt_id}/verify")
def verify_receipt(receipt_id: str, db: Session = Depends(get_db)) -> dict:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "receipt not found")
    return receipt_service.verify_receipt(receipt)


# --------------------------------------------------------------------------- #
# Disputes
# --------------------------------------------------------------------------- #
@router.post("/disputes", response_model=DisputeOut, status_code=201)
def open_dispute(body: DisputeCreate, user: CurrentUser,
                 db: Session = Depends(get_db)) -> DisputeOut:
    job = db.get(Job, body.job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    try:
        dispute = job_service.open_dispute(db, job, user, body.reason, body.detail, body.evidence)
    except job_service.JobError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return DisputeOut.model_validate(dispute)


@router.get("/disputes", response_model=list[DisputeOut])
def list_disputes(user: CurrentUser, db: Session = Depends(get_db),
                  limit: int = Query(50, le=200)) -> list[DisputeOut]:
    stmt = select(Dispute)
    if user.role != Role.admin:
        stmt = stmt.where(Dispute.raised_by == user.id)
    rows = db.scalars(stmt.order_by(Dispute.created_at.desc()).limit(limit)).all()
    return [DisputeOut.model_validate(r) for r in rows]


@router.get("/disputes/{dispute_id}", response_model=DisputeOut)
def get_dispute(dispute_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> DisputeOut:
    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dispute not found")
    if user.role != Role.admin and dispute.raised_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your dispute")
    return DisputeOut.model_validate(dispute)


@router.post("/disputes/{dispute_id}/triage", response_model=DisputeOut)
def triage(dispute_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> DisputeOut:
    """Run the evidence-based auto-resolver (integrity, receipt chain, overcharge)."""
    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dispute not found")
    if user.role != Role.admin and dispute.raised_by != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your dispute")
    return DisputeOut.model_validate(job_service.auto_triage(db, dispute))


@router.post("/disputes/{dispute_id}/resolve", response_model=DisputeOut)
def resolve(dispute_id: str, body: DisputeResolve, db: Session = Depends(get_db),
            _admin=Depends(require_admin)) -> DisputeOut:
    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dispute not found")
    resolved = job_service.resolve_dispute(
        db, dispute, in_favor_of_consumer=body.in_favor_of_consumer,
        resolution=body.resolution or "resolved by operator",
        refund_micros=body.refund_micros,
    )
    return DisputeOut.model_validate(resolved)
