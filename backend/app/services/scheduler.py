"""Background scheduler.

One asyncio loop drives every periodic concern:

  queued jobs      -> execute
  retry backoff    -> re-quote, re-pay, re-run
  schedules        -> fire due recurring jobs
  escrow expiry    -> release funds, cancel job
  stuck jobs       -> refund past deadline
  disputes         -> auto-triage on evidence
  cleanup          -> artifacts, workspaces, orphan containers, worker records
  reputation       -> idle decay + metric refresh
  bazaar           -> refresh local + remote discovery index

Every task is idempotent and lock-guarded, so running several API replicas is safe.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bazaar.discovery import discovery
from ..cache import Lock
from ..config import settings
from ..db import session_scope
from ..models import (
    Artifact,
    Dispute,
    DisputeStatus,
    Job,
    JobStatus,
    Payment,
    PaymentStatus,
    Provider,
    Schedule,
    Service,
    User,
    Worker,
    WorkerStatus,
    ZerionRequest,
)
from ..observability import reputation_gauge, scheduler_ticks, workers_reaped
from ..storage import delete_artifact
from ..workers.sandbox import reap_orphan_containers, sweep_stale_workspaces
from . import jobs as job_service
from . import reputation
from .cron import next_fire_time

log = logging.getLogger("m2x.scheduler")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Individual tasks
# --------------------------------------------------------------------------- #
def run_queued_jobs(db: Session, limit: int = 5) -> int:
    ran = 0
    queued = db.scalars(
        select(Job).where(Job.status == JobStatus.queued).order_by(Job.created_at).limit(limit)
    ).all()
    for job in queued:
        lock = Lock(f"job:{job.id}", ttl=300)
        if not lock.acquire():
            continue
        try:
            job_service.execute_job(db, job)
            db.commit()
            ran += 1
        except Exception as exc:
            db.rollback()
            log.exception("job %s execution failed: %s", job.id, exc)
        finally:
            lock.release()
    return ran


def process_retries(db: Session, limit: int = 5) -> int:
    due = db.scalars(
        select(Job)
        .where(
            Job.status == JobStatus.failed,
            Job.next_attempt_at.is_not(None),
            Job.next_attempt_at <= utcnow(),
            Job.attempts < Job.max_attempts,
        )
        .limit(limit)
    ).all()
    count = 0
    for job in due:
        try:
            payment = job_service.prepare_retry(db, job)
            user = db.get(User, job.consumer_id)
            job_service.autopay(db, job, payment, user)
            db.commit()
            count += 1
        except Exception as exc:
            db.rollback()
            log.warning("retry setup failed for %s: %s", job.id, exc)
            job.next_attempt_at = None
            db.commit()
    return count


def fire_due_schedules(db: Session, limit: int = 10) -> int:
    now = utcnow()
    due = db.scalars(
        select(Schedule)
        .where(Schedule.enabled.is_(True), Schedule.next_run_at.is_not(None),
               Schedule.next_run_at <= now)
        .limit(limit)
    ).all()
    fired = 0
    for schedule in due:
        lock = Lock(f"schedule:{schedule.id}", ttl=120)
        if not lock.acquire():
            continue
        try:
            service = db.get(Service, schedule.service_id)
            owner = db.get(User, schedule.owner_id)
            if service is None or owner is None or not service.is_active:
                schedule.enabled = False
                schedule.failure_count += 1
                db.commit()
                continue
            job, payment, _ = job_service.create_job(
                db, consumer=owner, service=service, payload=schedule.payload,
                max_price_micros=schedule.max_price_micros or None,
            )
            job_service.autopay(db, job, payment, owner)
            schedule.last_run_at = now
            schedule.last_job_id = job.id
            schedule.run_count += 1
            schedule.next_run_at = compute_next_run(schedule, now)
            db.commit()
            fired += 1
        except Exception as exc:
            db.rollback()
            schedule.failure_count += 1
            schedule.next_run_at = compute_next_run(schedule, now)
            db.commit()
            log.warning("schedule %s failed: %s", schedule.id, exc)
        finally:
            lock.release()
    return fired


def compute_next_run(schedule: Schedule, after: datetime | None = None) -> datetime | None:
    after = after or utcnow()
    if schedule.cron:
        return next_fire_time(schedule.cron, after)
    if schedule.interval_seconds:
        return after + timedelta(seconds=schedule.interval_seconds)
    return None


def expire_payments(db: Session) -> int:
    now = utcnow()
    stale = db.scalars(
        select(Payment).where(
            Payment.status == PaymentStatus.required,
            Payment.expires_at.is_not(None),
            Payment.expires_at <= now,
        )
    ).all()
    count = 0
    for payment in stale:
        payment.status = PaymentStatus.expired
        job = db.get(Job, payment.job_id) if payment.job_id else None
        if job and job.status == JobStatus.awaiting_payment:
            job.status = JobStatus.cancelled
            job.finished_at = now
            job_service.emit(db, job, "expired", "payment window elapsed without settlement")
        count += 1
    if count:
        db.commit()
    return count


def refund_stuck_jobs(db: Session) -> int:
    now = utcnow()
    stuck = db.scalars(
        select(Job).where(
            Job.status.in_([JobStatus.running, JobStatus.queued]),
            Job.deadline_at.is_not(None),
            Job.deadline_at <= now,
        )
    ).all()
    count = 0
    for job in stuck:
        payment = db.scalar(select(Payment).where(Payment.job_id == job.id))
        job_service._refund_failure(db, job, payment, reason="deadline exceeded")
        job.status = JobStatus.failed
        job.error = job.error or "deadline exceeded"
        job.finished_at = now
        job_service.emit(db, job, "deadline_exceeded", "job passed its deadline; escrow released")
        count += 1
    if count:
        db.commit()
    return count


def triage_disputes(db: Session, older_than_seconds: int = 5) -> int:
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    open_disputes = db.scalars(
        select(Dispute).where(Dispute.status == DisputeStatus.open, Dispute.created_at <= cutoff)
    ).all()
    count = 0
    for dispute in open_disputes:
        try:
            job_service.auto_triage(db, dispute)
            db.commit()
            count += 1
        except Exception as exc:
            db.rollback()
            log.warning("dispute triage failed for %s: %s", dispute.id, exc)
    return count


def cleanup(db: Session) -> dict:
    now = utcnow()
    expired_artifacts = db.scalars(
        select(Artifact).where(
            Artifact.deleted.is_(False), Artifact.expires_at.is_not(None), Artifact.expires_at <= now
        ).limit(200)
    ).all()
    for artifact in expired_artifacts:
        delete_artifact(artifact.storage_key)
        artifact.deleted = True

    reaped = 0
    stale_workers = db.scalars(
        select(Worker).where(
            Worker.status.in_([WorkerStatus.provisioning, WorkerStatus.running]),
            Worker.expires_at.is_not(None),
            Worker.expires_at <= now,
        ).limit(100)
    ).all()
    for worker in stale_workers:
        worker.status = WorkerStatus.reaped
        worker.reaped_at = now
        reaped += 1
    if reaped:
        workers_reaped.inc(reaped)

    workspaces = sweep_stale_workspaces(settings.worker_ttl_seconds)
    containers = reap_orphan_containers()

    # External-provider results have their own TTL: the telemetry row survives
    # for audit, but the cached payload it points at does not outlive the policy
    # the capability advertised.
    expired_external = db.scalars(
        select(ZerionRequest).where(
            ZerionRequest.expires_at.is_not(None),
            ZerionRequest.expires_at <= now,
            ZerionRequest.summary != "",
        ).limit(200)
    ).all()
    for row in expired_external:
        row.meta = {"expired": True, "capability": row.capability}
        row.summary = ""

    db.commit()
    return {
        "artifacts_deleted": len(expired_artifacts),
        "workers_reaped": reaped,
        "workspaces_removed": workspaces,
        "containers_removed": containers,
        "external_results_expired": len(expired_external),
    }


def refresh_reputation(db: Session) -> int:
    touched = reputation.decay_idle_providers(db)
    for provider in db.scalars(select(Provider)).all():
        try:
            reputation_gauge.labels(provider.slug).set(provider.reputation_score)
        except Exception:
            pass
    db.commit()
    return touched


def refresh_discovery(db: Session) -> dict:
    result = discovery.refresh(db)
    db.commit()
    return result


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #
class Scheduler:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.running = False
        self.iterations = 0
        self.last_tick: datetime | None = None
        self.last_result: dict = {}
        self.errors = 0

    async def start(self) -> None:
        if self.task is not None or not settings.scheduler_enabled:
            return
        self.running = True
        self.task = asyncio.create_task(self._loop(), name="m2x-scheduler")
        log.info("scheduler started (tick=%ss)", settings.scheduler_tick_seconds)

    async def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
            log.info("scheduler stopped")

    async def _loop(self) -> None:
        slow_every = max(int(30 / max(settings.scheduler_tick_seconds, 0.5)), 1)
        while self.running:
            try:
                await asyncio.to_thread(self.tick, self.iterations % slow_every == 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors += 1
                log.exception("scheduler tick failed")
            self.iterations += 1
            await asyncio.sleep(settings.scheduler_tick_seconds)

    def tick(self, slow_pass: bool = True) -> dict:
        scheduler_ticks.inc()
        result: dict = {}
        with session_scope() as db:
            result["jobs_run"] = run_queued_jobs(db)
            result["retries"] = process_retries(db)
            result["schedules_fired"] = fire_due_schedules(db)
            result["payments_expired"] = expire_payments(db)
            result["disputes_triaged"] = triage_disputes(db)
            if slow_pass:
                result["stuck_refunded"] = refund_stuck_jobs(db)
                result["cleanup"] = cleanup(db)
                result["reputation_decayed"] = refresh_reputation(db)
                result["discovery"] = refresh_discovery(db)
        self.last_tick = utcnow()
        self.last_result = result
        return result

    def status(self) -> dict:
        return {
            "enabled": settings.scheduler_enabled,
            "running": self.running,
            "iterations": self.iterations,
            "errors": self.errors,
            "tick_seconds": settings.scheduler_tick_seconds,
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "last_result": self.last_result,
        }


scheduler = Scheduler()
