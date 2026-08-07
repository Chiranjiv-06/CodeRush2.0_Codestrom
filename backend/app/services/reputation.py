"""Reputation engine.

Score is an EWMA over signed outcome events, bounded to [0, 100], with
separate weights for success, latency against the advertised SLA, integrity
failures and lost disputes. Providers decay toward the neutral baseline when
idle so stale five-star histories don't persist forever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Job, JobStatus, Provider, ReputationEvent
from ..observability import reputation_gauge

BASELINE = 50.0
ALPHA = 0.18  # EWMA responsiveness

WEIGHTS = {
    "job_succeeded": +6.0,
    "job_failed": -9.0,
    "job_timeout": -7.0,
    "integrity_failed": -25.0,
    "integrity_verified": +2.0,
    "dispute_opened": -4.0,
    "dispute_lost": -20.0,
    "dispute_won": +5.0,
    "sla_met": +1.5,
    "sla_missed": -4.0,
    "refund_issued": -3.0,
    "idle_decay": 0.0,
}


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def record_event(
    db: Session, provider: Provider, kind: str, *, job_id: str | None = None,
    detail: dict | None = None, weight_override: float | None = None,
) -> ReputationEvent:
    delta = WEIGHTS.get(kind, 0.0) if weight_override is None else weight_override
    target = _clamp(provider.reputation_score + delta * 2.5)
    new_score = _clamp(provider.reputation_score + ALPHA * (target - provider.reputation_score) + delta * 0.25)

    provider.reputation_score = round(new_score, 3)
    event = ReputationEvent(
        provider_id=provider.id,
        job_id=job_id,
        kind=kind,
        delta=round(new_score - (new_score - delta * 0.25), 4),
        score_after=provider.reputation_score,
        detail=detail or {},
    )
    db.add(event)
    try:
        reputation_gauge.labels(provider.slug).set(provider.reputation_score)
    except Exception:  # pragma: no cover - metrics must never break business logic
        pass
    return event


def on_job_finished(db: Session, provider: Provider, job: Job, *, sla_seconds: int) -> None:
    provider.total_jobs += 1
    if job.status == JobStatus.succeeded:
        provider.successful_jobs += 1
        record_event(db, provider, "job_succeeded", job_id=job.id)
        if job.integrity_verified:
            record_event(db, provider, "integrity_verified", job_id=job.id)
        else:
            record_event(db, provider, "integrity_failed", job_id=job.id)
    else:
        provider.failed_jobs += 1
        kind = "job_timeout" if "timeout" in (job.error or "").lower() else "job_failed"
        record_event(db, provider, kind, job_id=job.id, detail={"error": job.error[:200]})

    if job.started_at and job.finished_at:
        elapsed_ms = (job.finished_at - job.started_at).total_seconds() * 1000
        n = max(provider.total_jobs, 1)
        provider.avg_latency_ms = round(
            ((provider.avg_latency_ms * (n - 1)) + elapsed_ms) / n, 2
        )
        record_event(
            db, provider,
            "sla_met" if elapsed_ms <= sla_seconds * 1000 else "sla_missed",
            job_id=job.id, detail={"elapsed_ms": round(elapsed_ms, 1), "sla_ms": sla_seconds * 1000},
        )


def on_dispute_resolved(db: Session, provider: Provider, in_favor_of_consumer: bool, job_id: str) -> None:
    if in_favor_of_consumer:
        provider.disputes_lost += 1
        record_event(db, provider, "dispute_lost", job_id=job_id)
    else:
        record_event(db, provider, "dispute_won", job_id=job_id)


def decay_idle_providers(db: Session, idle_hours: int = 24) -> int:
    """Pull inactive providers gently back toward the neutral baseline."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=idle_hours)
    touched = 0
    for provider in db.scalars(select(Provider).where(Provider.is_active.is_(True))).all():
        last = db.scalar(
            select(func.max(ReputationEvent.created_at)).where(
                ReputationEvent.provider_id == provider.id
            )
        )
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or last < cutoff:
            drift = (BASELINE - provider.reputation_score) * 0.05
            if abs(drift) > 0.01:
                provider.reputation_score = round(_clamp(provider.reputation_score + drift), 3)
                record_event(db, provider, "idle_decay", weight_override=0.0,
                             detail={"drift": round(drift, 3)})
                touched += 1
    return touched


def provider_stats(db: Session, provider: Provider) -> dict:
    success_rate = (
        provider.successful_jobs / provider.total_jobs if provider.total_jobs else 0.0
    )
    recent = db.scalars(
        select(ReputationEvent)
        .where(ReputationEvent.provider_id == provider.id)
        .order_by(ReputationEvent.created_at.desc())
        .limit(20)
    ).all()
    return {
        "provider_id": provider.id,
        "slug": provider.slug,
        "score": provider.reputation_score,
        "tier": tier_for(provider.reputation_score),
        "total_jobs": provider.total_jobs,
        "successful_jobs": provider.successful_jobs,
        "failed_jobs": provider.failed_jobs,
        "success_rate": round(success_rate, 4),
        "disputes_lost": provider.disputes_lost,
        "avg_latency_ms": provider.avg_latency_ms,
        "recent_events": [
            {"kind": e.kind, "score_after": e.score_after, "at": e.created_at.isoformat()}
            for e in recent
        ],
    }


def tier_for(score: float) -> str:
    if score >= 85:
        return "platinum"
    if score >= 70:
        return "gold"
    if score >= 55:
        return "silver"
    if score >= 35:
        return "bronze"
    return "probation"


def leaderboard(db: Session, limit: int = 20) -> list[dict]:
    rows = db.scalars(
        select(Provider)
        .where(Provider.is_active.is_(True))
        .order_by(Provider.reputation_score.desc())
        .limit(limit)
    ).all()
    return [
        {
            "provider_id": p.id,
            "slug": p.slug,
            "name": p.name,
            "score": p.reputation_score,
            "tier": tier_for(p.reputation_score),
            "total_jobs": p.total_jobs,
            "success_rate": round(p.successful_jobs / p.total_jobs, 4) if p.total_jobs else 0.0,
            "avg_latency_ms": p.avg_latency_ms,
        }
        for p in rows
    ]
