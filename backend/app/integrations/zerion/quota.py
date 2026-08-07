"""Quotas and cost control for Zerion requests.

The failure mode this exists to prevent is an agent looping on a paid endpoint.
Every gate below is evaluated *before* a request is authorized, and each one
raises rather than returns, so there is no path where a check is "checked" and
then ignored.

Four independent limits apply:

* ``ZERION_MAX_REQUESTS_PER_JOB`` — how many Zerion calls one job may make;
* ``ZERION_MAX_REQUESTS_PER_SESSION`` — per principal, per rolling window;
* ``ZERION_MAX_SPEND_MICROS`` — total provider spend per principal, per window;
* the caller's own remaining budget, checked against the quote.

Accounting is done against :class:`~app.models.ZerionRequest` rows, which are
written for failures too — a request that was attempted counts, so a failing
loop still runs into the quota.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import ZerionRequest
from .errors import ZerionBudgetError, ZerionQuotaError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _window_start() -> datetime:
    return utcnow() - timedelta(seconds=max(settings.zerion_quota_window_seconds, 1))


def usage(db: Session, *, user_id: str, job_id: str | None = None) -> dict[str, Any]:
    """Current quota consumption for a principal (and optionally one job)."""
    since = _window_start()

    session_requests = int(
        db.scalar(
            select(func.count(ZerionRequest.id)).where(
                ZerionRequest.user_id == user_id, ZerionRequest.created_at >= since
            )
        ) or 0
    )
    session_spend = int(
        db.scalar(
            select(func.coalesce(func.sum(ZerionRequest.provider_cost_micros), 0)).where(
                ZerionRequest.user_id == user_id, ZerionRequest.created_at >= since
            )
        ) or 0
    )
    job_requests = 0
    if job_id:
        job_requests = int(
            db.scalar(
                select(func.count(ZerionRequest.id)).where(ZerionRequest.job_id == job_id)
            ) or 0
        )

    return {
        "window_seconds": settings.zerion_quota_window_seconds,
        "window_started_at": since.isoformat(),
        "requests_this_job": job_requests,
        "max_requests_per_job": settings.zerion_max_requests_per_job,
        "requests_this_session": session_requests,
        "max_requests_per_session": settings.zerion_max_requests_per_session,
        "spend_micros_this_session": session_spend,
        "max_spend_micros": settings.zerion_max_spend_micros,
        "requests_remaining_this_session": max(
            settings.zerion_max_requests_per_session - session_requests, 0
        ),
        "spend_remaining_micros": max(settings.zerion_max_spend_micros - session_spend, 0),
    }


def enforce(
    db: Session,
    *,
    user_id: str,
    job_id: str | None,
    cost_micros: int,
    upstream_requests: int = 1,
    budget_micros: int | None = None,
    price_micros: int = 0,
) -> dict[str, Any]:
    """Raise unless one more Zerion request is permitted right now.

    ``cost_micros`` is what the request will cost on the *provider's* rail;
    ``price_micros`` is what the consumer is being charged, which is what the
    caller's budget has to cover.
    """
    current = usage(db, user_id=user_id, job_id=job_id)

    if job_id and current["requests_this_job"] + 1 > settings.zerion_max_requests_per_job:
        raise ZerionQuotaError(
            f"job {job_id} has already made {current['requests_this_job']} Zerion request(s); "
            f"the per-job limit is {settings.zerion_max_requests_per_job}",
            limit="max_requests_per_job",
            used=current["requests_this_job"],
            allowed=settings.zerion_max_requests_per_job,
        )

    if current["requests_this_session"] + 1 > settings.zerion_max_requests_per_session:
        raise ZerionQuotaError(
            f"{current['requests_this_session']} Zerion request(s) already made in the last "
            f"{settings.zerion_quota_window_seconds}s; the limit is "
            f"{settings.zerion_max_requests_per_session}",
            limit="max_requests_per_session",
            used=current["requests_this_session"],
            allowed=settings.zerion_max_requests_per_session,
            window_seconds=settings.zerion_quota_window_seconds,
        )

    projected_spend = current["spend_micros_this_session"] + cost_micros * max(upstream_requests, 1)
    if projected_spend > settings.zerion_max_spend_micros:
        raise ZerionQuotaError(
            f"this request would take Zerion spend to {projected_spend} micros, over the "
            f"{settings.zerion_max_spend_micros} micro limit for this window",
            limit="max_spend_micros",
            used=current["spend_micros_this_session"],
            projected=projected_spend,
            allowed=settings.zerion_max_spend_micros,
        )

    if budget_micros is not None and price_micros > budget_micros:
        raise ZerionBudgetError(
            f"quote of {price_micros} micros exceeds the remaining budget of {budget_micros}",
            quoted_micros=price_micros,
            budget_micros=budget_micros,
        )

    return {**current, "projected_spend_micros": projected_spend, "ok": True}


def expiry() -> datetime:
    """When a recorded Zerion result should be swept by the cleanup pass."""
    return utcnow() + timedelta(seconds=max(settings.zerion_result_ttl_seconds, 60))
