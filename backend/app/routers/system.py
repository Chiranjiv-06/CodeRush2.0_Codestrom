"""Health, metrics, platform statistics and operator actions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..algorand import asset_descriptor
from ..algorand import configuration_report as algorand_config_report
from ..bazaar.discovery import discovery
from ..cache import backend_name as cache_backend
from ..config import settings
from ..db import get_db
from ..integrations.zerion.payment import mode_report as zerion_mode_report
from ..models import (
    Dispute,
    Job,
    JobStatus,
    Payment,
    PaymentStatus,
    Provider,
    Receipt,
    Service,
    User,
    Worker,
    ZerionRequest,
)
from ..observability import metrics_payload
from ..security import require_admin
from ..services import receipts as receipt_service
from ..services.reputation import leaderboard
from ..services.scheduler import cleanup as cleanup_task
from ..services.scheduler import scheduler
from ..storage import backend_name as storage_backend
from ..workers.sandbox import runner

router = APIRouter(tags=["system"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db_ok = True
    try:
        db.execute(select(func.count(User.id)))
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "version": settings.version,
        "env": settings.env,
        "components": {
            "database": {"ok": db_ok,
                         "driver": "postgresql" if not settings.is_sqlite else "sqlite"},
            "cache": {"backend": cache_backend()},
            "storage": {"backend": storage_backend()},
            "sandbox": {"backend": runner.backend},
            "scheduler": {"running": scheduler.running, "iterations": scheduler.iterations},
            "x402": {"network": settings.x402_network,
                     "asset_id": settings.algorand_asset_id,
                     "settlement": settings.x402_settlement_backend},
            "algorand": algorand_config_report(),
            "bazaar": {"enabled": settings.bazaar_enabled},
            "zerion": zerion_mode_report(),
        },
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    db.execute(select(func.count(Service.id)))
    return {"ready": True}


@router.get("/metrics")
def metrics() -> Response:
    if not settings.metrics_enabled:
        return Response("metrics disabled", status_code=404)
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type)


@router.get("/v1/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)

    def count(model, *where):
        return int(db.scalar(select(func.count(model.id)).where(*where)) or 0)

    settled = int(
        db.scalar(
            select(func.coalesce(func.sum(Payment.captured_micros), 0)).where(
                Payment.status == PaymentStatus.settled
            )
        )
        or 0
    )
    refunded = int(
        db.scalar(select(func.coalesce(func.sum(Payment.refunded_micros), 0))) or 0
    )
    by_status = {
        row[0].value if hasattr(row[0], "value") else str(row[0]): row[1]
        for row in db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    }
    return {
        "providers": count(Provider),
        "services": count(Service),
        "users": count(User),
        "jobs": {
            "total": count(Job),
            "last_24h": count(Job, Job.created_at >= day_ago),
            "by_status": by_status,
            "succeeded": by_status.get("succeeded", 0),
            "failed": by_status.get("failed", 0),
        },
        "payment_asset": asset_descriptor(),
        "payments": {
            "count": count(Payment),
            "settled_micros": settled,
            "refunded_micros": refunded,
            "platform_fee_micros": int(
                db.scalar(select(func.coalesce(func.sum(Payment.fee_micros), 0))) or 0
            ),
        },
        "receipts": receipt_service.receipt_stats(db),
        "disputes": {
            "total": count(Dispute),
            "open": count(Dispute, Dispute.status.in_(["open", "under_review"])),
        },
        "workers": {"total": count(Worker), "backend": runner.backend},
        "zerion": {
            "requests": count(ZerionRequest),
            "provider_spend_micros": int(
                db.scalar(
                    select(func.coalesce(func.sum(ZerionRequest.provider_cost_micros), 0))
                ) or 0
            ),
            **zerion_mode_report(),
        },
        "bazaar": discovery.status(db),
        "leaderboard": leaderboard(db, limit=10),
        "scheduler": scheduler.status(),
    }


@router.post("/v1/admin/scheduler/tick")
def force_tick(_admin=Depends(require_admin)) -> dict:
    return scheduler.tick(slow_pass=True)


@router.post("/v1/admin/cleanup")
def force_cleanup(db: Session = Depends(get_db), _admin=Depends(require_admin)) -> dict:
    return cleanup_task(db)


@router.get("/v1/admin/audit")
def audit(db: Session = Depends(get_db), _admin=Depends(require_admin), limit: int = 100) -> list[dict]:
    from ..models import AuditLog

    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [{"id": r.id, "actor_id": r.actor_id, "action": r.action, "target": r.target,
             "data": r.data, "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/v1/admin/integrity/verify-chain")
def verify_chain(db: Session = Depends(get_db), _admin=Depends(require_admin)) -> dict:
    return receipt_service.verify_chain(db)


@router.get("/v1/config")
def public_config() -> dict:
    """Non-secret runtime configuration, consumed by the dashboard."""
    return {
        "app_name": settings.app_name,
        "version": settings.version,
        "env": settings.env,
        "payment_asset": asset_descriptor(),
        "algorand": {
            "network": settings.algorand_network,
            "network_label": settings.network_label,
            "asset_id": settings.algorand_asset_id,
            "algod_configured": bool(settings.algod_url),
        },
        "x402": {
            "version": settings.x402_version,
            "network": settings.x402_network,
            "asset": settings.x402_asset,
            "asset_id": settings.algorand_asset_id,
            "decimals": settings.x402_asset_decimals,
            "settlement_backend": settings.x402_settlement_backend,
        },
        "bazaar": {
            "enabled": settings.bazaar_enabled,
            "endpoint": f"{settings.bazaar_base_url}{settings.bazaar_list_path}",
            "network": settings.bazaar_network,
            "asset_id": settings.algorand_asset_id,
            "extension": "@x402-avm/extensions",
        },
        "sandbox": {"backend": runner.backend, "timeout_seconds": settings.sandbox_timeout_seconds},
        # Presence flags only — no Zerion API key or wallet key is ever served
        # to a client, and mode_report() cannot return one.
        "zerion": zerion_mode_report(),
        "platform_fee_bps": settings.platform_fee_bps,
        "signup_grant_micros": settings.signup_grant_micros,
    }
