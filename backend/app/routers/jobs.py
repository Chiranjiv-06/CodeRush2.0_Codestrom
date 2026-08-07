"""The paid execution path: quote -> 402 -> pay -> run -> receipt."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Artifact, Job, JobEvent, JobStatus, Payment, Role, Service
from ..schemas import JobCreate, JobEventOut, JobOut, PayRequest, QuoteRequest
from ..security import CurrentUser
from ..services import jobs as job_service
from ..storage import get_artifact
from ..x402.protocol import (
    PaymentRequirements,
    X402Error,
    build_payment_required,
    decode_payment_header,
    encode_settlement_header,
)

router = APIRouter(prefix="/v1", tags=["jobs"])


def _service_or_404(db: Session, service_id: str) -> Service:
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    return service


def _job_or_404(db: Session, job_id: str, user) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if user.role != Role.admin and user.id not in (job.consumer_id,):
        from ..models import Provider

        provider = db.get(Provider, job.provider_id)
        if provider is None or provider.owner_id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not your job")
    return job


def _resolve_quoted_payment(db: Session, user, service: Service, x_payment: str):
    """Map an ``X-PAYMENT`` header back to the payment it was signed against."""
    try:
        payload = decode_payment_header(x_payment)
    except X402Error as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"malformed payment: {exc}")
    nonce = (payload.payload.get("authorization") or {}).get("nonce", "")
    payment = db.scalar(select(Payment).where(Payment.nonce == nonce)) if nonce else None
    if payment is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "unknown payment nonce — call this resource without X-PAYMENT to get a fresh 402 quote",
        )
    if payment.payer_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "payment belongs to another principal")
    job = db.get(Job, payment.job_id) if payment.job_id else None
    if job is None or job.service_id != service.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payment does not match this resource")
    return job, payment


def _payment_required_response(job: Job, payment: Payment, quote: dict) -> JSONResponse:
    body = build_payment_required(
        [PaymentRequirements(**payment.requirements)],
        error="X-PAYMENT header is required to execute this resource",
    )
    body["job_id"] = job.id
    body["payment_id"] = payment.id
    body["quote"] = quote
    return JSONResponse(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        content=body,
        headers={"X-Payment-Id": payment.id, "X-Job-Id": job.id},
    )


# --------------------------------------------------------------------------- #
# Quote
# --------------------------------------------------------------------------- #
@router.post("/quotes")
def create_quote(body: QuoteRequest, user: CurrentUser, db: Session = Depends(get_db)) -> dict:
    service = _service_or_404(db, body.service_id)
    return job_service.quote(db, service, body.payload)


# --------------------------------------------------------------------------- #
# x402-native resource invocation
# --------------------------------------------------------------------------- #
@router.post("/services/{service_id}/invoke")
def invoke_service(
    service_id: str,
    payload: dict,
    user: CurrentUser,
    response: Response,
    db: Session = Depends(get_db),
    x_payment: str | None = Header(default=None, alias="X-PAYMENT"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Canonical x402 endpoint.

    Without ``X-PAYMENT`` it answers **402** with the payment requirements.
    With a valid ``X-PAYMENT`` it escrows, executes, settles, and returns the
    result plus an ``X-PAYMENT-RESPONSE`` settlement header.
    """
    service = _service_or_404(db, service_id)

    if x_payment:
        # A retry of a 402'd request: bind to the payment the client was quoted,
        # identified by the nonce inside the signed authorization.
        job, payment = _resolve_quoted_payment(db, user, service, x_payment)
        quote = job_service.quote(db, service, job.payload)
    else:
        try:
            job, payment, quote = job_service.create_job(
                db, consumer=user, service=service, payload=payload,
                idempotency_key=idempotency_key,
            )
        except job_service.JobError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        db.commit()
        return _payment_required_response(job, payment, quote)

    try:
        job_service.apply_payment(db, job, payment, x_payment)
    except X402Error as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"malformed payment: {exc}")
    except job_service.JobError as exc:
        db.commit()
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content={
                **build_payment_required(
                    [PaymentRequirements(**payment.requirements)], error=str(exc)
                ),
                "job_id": job.id,
                "payment_id": payment.id,
            },
        )

    job_service.execute_job(db, job)
    db.commit()

    settlement = {
        "success": job.status == JobStatus.succeeded,
        "transaction": payment.tx_hash,
        "network": payment.network,
        "blockchain": settings.blockchain,
        "asset": payment.asset,
        "assetId": payment.asset_id,
        "payer": payment.payer_id,
        "amountCharged": str(job.final_price_micros),
        "amountRefunded": str(payment.refunded_micros),
    }
    response.headers["X-PAYMENT-RESPONSE"] = encode_settlement_header(settlement)
    response.headers["X-Job-Id"] = job.id
    status_code = 200 if job.status == JobStatus.succeeded else 502
    return JSONResponse(
        status_code=status_code,
        content={
            "job_id": job.id,
            "status": job.status.value,
            "result": (job.result or {}).get("output"),
            "stdout": (job.result or {}).get("stdout", "")[-4000:],
            "error": job.error,
            "output_hash": job.output_hash,
            "integrity_verified": job.integrity_verified,
            "charged_micros": job.final_price_micros,
            "refunded_micros": payment.refunded_micros,
            "settlement": settlement,
        },
        headers=dict(response.headers),
    )


# --------------------------------------------------------------------------- #
# Job resources
# --------------------------------------------------------------------------- #
@router.post("/jobs", status_code=201)
def create_job(body: JobCreate, user: CurrentUser, db: Session = Depends(get_db),
               x_payment: str | None = Header(default=None, alias="X-PAYMENT")):
    service = _service_or_404(db, body.service_id)
    try:
        job, payment, quote = job_service.create_job(
            db, consumer=user, service=service, payload=body.payload,
            max_price_micros=body.max_price_micros, idempotency_key=body.idempotency_key,
        )
    except job_service.JobError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    paid = False
    if x_payment:
        try:
            job_service.apply_payment(db, job, payment, x_payment)
            paid = True
        except (X402Error, job_service.JobError) as exc:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc))
    elif body.auto_pay:
        try:
            job_service.autopay(db, job, payment, user)
            paid = True
        except (X402Error, job_service.JobError) as exc:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc))

    if not paid:
        db.commit()
        return _payment_required_response(job, payment, quote)

    db.commit()
    return {
        "job": JobOut.model_validate(job).model_dump(),
        "payment_id": payment.id,
        "quote": quote,
        "next": "the scheduler will execute this job; poll GET /v1/jobs/{id}",
    }


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(user: CurrentUser, db: Session = Depends(get_db),
              status_filter: str = Query("", alias="status"),
              limit: int = Query(50, le=200), offset: int = 0) -> list[JobOut]:
    from ..models import Provider

    owned_providers = [p.id for p in db.scalars(
        select(Provider).where(Provider.owner_id == user.id)
    ).all()]
    stmt = select(Job)
    if user.role != Role.admin:
        stmt = stmt.where(
            (Job.consumer_id == user.id) | (Job.provider_id.in_(owned_providers or ["-"]))
        )
    if status_filter:
        stmt = stmt.where(Job.status == JobStatus(status_filter))
    rows = db.scalars(stmt.order_by(Job.created_at.desc()).offset(offset).limit(limit)).all()
    return [JobOut.model_validate(r) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> JobOut:
    return JobOut.model_validate(_job_or_404(db, job_id, user))


@router.get("/jobs/{job_id}/events", response_model=list[JobEventOut])
def job_events(job_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> list[JobEventOut]:
    _job_or_404(db, job_id, user)
    rows = db.scalars(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
    ).all()
    return [JobEventOut.model_validate(r) for r in rows]


@router.post("/jobs/{job_id}/pay")
def pay_job(job_id: str, user: CurrentUser, db: Session = Depends(get_db),
            body: PayRequest | None = None,
            x_payment: str | None = Header(default=None, alias="X-PAYMENT")) -> dict:
    job = _job_or_404(db, job_id, user)
    payment = db.scalar(
        select(Payment).where(Payment.job_id == job.id).order_by(Payment.created_at.desc())
    )
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no open payment for this job")
    header = x_payment or (body.x_payment if body else None)
    if not header:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-PAYMENT header or body required")
    try:
        result = job_service.apply_payment(db, job, payment, header)
    except X402Error as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"malformed payment: {exc}")
    except job_service.JobError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc))
    return {"job_id": job.id, "payment_id": payment.id, **result}


@router.get("/jobs/{job_id}/preflight")
def preflight(job_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> dict:
    """The validation a job must pass before it is allowed to run.

    Same checks the executor enforces — network, asset id, buyer balance, seller
    address and payment authorization — exposed so a client can see why a job
    would be rejected without spending anything.
    """
    job = _job_or_404(db, job_id, user)
    payment = db.scalar(
        select(Payment).where(Payment.job_id == job.id).order_by(Payment.created_at.desc())
    )
    return job_service.preflight(db, job, payment)


@router.post("/jobs/{job_id}/execute")
def execute_now(job_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> dict:
    """Run a paid, queued job immediately instead of waiting for the scheduler."""
    job = _job_or_404(db, job_id, user)
    if job.status != JobStatus.queued:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"job is {job.status.value}, expected queued")
    job_service.execute_job(db, job)
    db.commit()
    return JobOut.model_validate(job).model_dump()


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> JobOut:
    job = _job_or_404(db, job_id, user)
    try:
        job_service.cancel_job(db, job)
    except job_service.JobError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return JobOut.model_validate(job)


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str, user: CurrentUser, db: Session = Depends(get_db),
          auto_pay: bool = True) -> dict:
    job = _job_or_404(db, job_id, user)
    if job.status != JobStatus.failed:
        raise HTTPException(status.HTTP_409_CONFLICT, "only failed jobs can be retried")
    if job.attempts >= job.max_attempts:
        raise HTTPException(status.HTTP_409_CONFLICT, "retry budget exhausted")
    payment = job_service.prepare_retry(db, job)
    if auto_pay:
        job_service.autopay(db, job, payment, user)
    db.commit()
    return {"job_id": job.id, "payment_id": payment.id, "status": job.status.value,
            "requirements": payment.requirements}


# --------------------------------------------------------------------------- #
# Artifacts
# --------------------------------------------------------------------------- #
@router.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> list[dict]:
    _job_or_404(db, job_id, user)
    rows = db.scalars(select(Artifact).where(Artifact.job_id == job_id)).all()
    return [
        {"id": a.id, "name": a.name, "size_bytes": a.size_bytes, "sha256": a.sha256,
         "backend": a.storage_backend, "deleted": a.deleted,
         "expires_at": a.expires_at.isoformat() if a.expires_at else None}
        for a in rows
    ]


@router.get("/artifacts/{artifact_id}")
def download_artifact(artifact_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> Response:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    _job_or_404(db, artifact.job_id, user)
    try:
        data = get_artifact(artifact.storage_key, artifact.sha256)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except Exception:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact bytes are gone")
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.name}"',
            "X-Content-SHA256": artifact.sha256,
        },
    )
