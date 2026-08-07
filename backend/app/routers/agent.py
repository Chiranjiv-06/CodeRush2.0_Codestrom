"""Agent planner and job scheduler APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.graph import engine_name, run_plan
from ..agent.planner import decompose, rank_services
from ..db import get_db
from ..models import Plan, Role, Schedule, Service
from ..schemas import PlanOut, PlanRequest, ScheduleCreate, ScheduleOut
from ..security import CurrentUser
from ..services.cron import CronError, describe, next_fire_time
from ..services.scheduler import compute_next_run, scheduler

router = APIRouter(prefix="/v1", tags=["agent"])


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
@router.post("/plans", response_model=PlanOut, status_code=201)
def create_plan(body: PlanRequest, user: CurrentUser, db: Session = Depends(get_db)) -> PlanOut:
    """Run the LangGraph agent: decompose, discover, pay via x402, execute, verify."""
    plan = run_plan(db, user, body.goal, body.budget_micros, body.max_steps)
    db.commit()
    return PlanOut.model_validate(plan)


@router.post("/plans/preview")
def preview_plan(body: PlanRequest, user: CurrentUser, db: Session = Depends(get_db)) -> dict:
    """Dry run: decomposition + candidate services, no jobs and no payments."""
    budget = body.budget_micros or 5_000_000
    steps = decompose(body.goal, max_steps=body.max_steps or 12)
    return {
        "goal": body.goal,
        "engine": engine_name(),
        "budget_micros": budget,
        "steps": [
            {
                **step.as_dict(),
                "candidates": rank_services(db, step, budget_micros=budget)[:5],
            }
            for step in steps
        ],
    }


@router.get("/plans", response_model=list[PlanOut])
def list_plans(user: CurrentUser, db: Session = Depends(get_db),
               limit: int = Query(25, le=100)) -> list[PlanOut]:
    stmt = select(Plan)
    if user.role != Role.admin:
        stmt = stmt.where(Plan.owner_id == user.id)
    rows = db.scalars(stmt.order_by(Plan.created_at.desc()).limit(limit)).all()
    return [PlanOut.model_validate(r) for r in rows]


@router.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> PlanOut:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plan not found")
    if user.role != Role.admin and plan.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your plan")
    return PlanOut.model_validate(plan)


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #
@router.post("/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(body: ScheduleCreate, user: CurrentUser,
                    db: Session = Depends(get_db)) -> ScheduleOut:
    service = db.get(Service, body.service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    if not body.cron and not body.interval_seconds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide cron or interval_seconds")
    if body.cron:
        try:
            next_fire_time(body.cron)
        except CronError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid cron: {exc}")

    schedule = Schedule(owner_id=user.id, **body.model_dump())
    schedule.next_run_at = compute_next_run(schedule)
    db.add(schedule)
    db.flush()
    return ScheduleOut.model_validate(schedule)


@router.get("/schedules", response_model=list[ScheduleOut])
def list_schedules(user: CurrentUser, db: Session = Depends(get_db)) -> list[ScheduleOut]:
    stmt = select(Schedule)
    if user.role != Role.admin:
        stmt = stmt.where(Schedule.owner_id == user.id)
    return [ScheduleOut.model_validate(r)
            for r in db.scalars(stmt.order_by(Schedule.created_at.desc())).all()]


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
def update_schedule(schedule_id: str, user: CurrentUser, db: Session = Depends(get_db),
                    enabled: bool | None = None, interval_seconds: int | None = None,
                    cron: str | None = None) -> ScheduleOut:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None or (schedule.owner_id != user.id and user.role != Role.admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "schedule not found")
    if enabled is not None:
        schedule.enabled = enabled
    if interval_seconds is not None:
        schedule.interval_seconds = interval_seconds
    if cron is not None:
        try:
            next_fire_time(cron)
        except CronError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid cron: {exc}")
        schedule.cron = cron
    schedule.next_run_at = compute_next_run(schedule)
    db.flush()
    return ScheduleOut.model_validate(schedule)


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    schedule = db.get(Schedule, schedule_id)
    if schedule is None or (schedule.owner_id != user.id and user.role != Role.admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "schedule not found")
    db.delete(schedule)


@router.get("/schedules/cron/validate")
def validate_cron(expression: str) -> dict:
    try:
        nxt = next_fire_time(expression)
        return {"valid": True, "description": describe(expression),
                "next_fire_at": nxt.isoformat() if nxt else None}
    except CronError as exc:
        return {"valid": False, "error": str(exc)}


@router.get("/scheduler/status")
def scheduler_status() -> dict:
    return scheduler.status()
