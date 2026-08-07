"""A2A (Agent-to-Agent) endpoint.

Publishes an Agent Card at ``/.well-known/agent.json`` and speaks the A2A
JSON-RPC methods ``message/send``, ``tasks/get`` and ``tasks/cancel`` at
``/a2a``. A remote agent hands us a natural-language goal plus a budget; we run
the LangGraph planner, pay providers over x402 and return the artifacts, receipts
and the exact amount spent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.graph import run_plan
from ..algorand import asset_descriptor
from ..config import settings
from ..db import get_db
from ..models import Job, Plan, Receipt, User
from ..security import CurrentUser

log = logging.getLogger("m2x.a2a")
router = APIRouter(tags=["a2a"])

A2A_VERSION = "0.2.0"
STATE_MAP = {
    "planning": "submitted",
    "running": "working",
    "completed": "completed",
    "partial": "completed",
    "failed": "failed",
}


def agent_card() -> dict:
    base = f"http://localhost:{settings.port}"
    return {
        "name": "M2X Compute Exchange Agent",
        "description": (
            "Buys and runs machine work on the M2X exchange: discovers providers through "
            "Bazaar, pays per call with x402, executes in ephemeral sandboxes and returns "
            "SHA-256 verified results with signed receipts."
        ),
        "version": settings.version,
        "protocolVersion": A2A_VERSION,
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "provider": {"organization": "M2X", "url": base},
        "capabilities": {"streaming": False, "pushNotifications": False,
                         "stateTransitionHistory": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer",
                       "description": "JWT from /v1/auth/login or an m2x_ API key"}
        },
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": "execute-goal",
                "name": "Execute a compute goal",
                "description": "Decompose a goal, select providers by price and reputation, "
                               "pay via x402 and return verified results.",
                "tags": ["compute", "x402", "marketplace", "sandbox"],
                "examples": [
                    "Hash this payload, then summarize the statistics of the resulting digest",
                    "Analyze these numbers and produce word statistics for the report",
                ],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            },
            {
                "id": "discover-providers",
                "name": "Discover paid tools",
                "description": "Search the federated Bazaar index for x402-priced capabilities.",
                "tags": ["discovery", "bazaar"],
            },
        ],
    }


def _task_from_plan(plan: Plan, db: Session) -> dict:
    jobs = db.scalars(select(Job).where(Job.plan_id == plan.id)).all()
    receipts = db.scalars(
        select(Receipt).where(Receipt.job_id.in_([j.id for j in jobs] or ["-"]))
    ).all()
    artifacts = [
        {
            "artifactId": f"step-{step.get('index')}",
            "name": step.get("goal", "")[:80],
            "parts": [{"kind": "data", "data": step.get("output")}],
        }
        for step in (plan.steps or [])
        if step.get("output") is not None
    ]
    return {
        "id": plan.id,
        "contextId": plan.owner_id,
        "kind": "task",
        "status": {
            "state": STATE_MAP.get(plan.status, "unknown"),
            "timestamp": (plan.finished_at or plan.updated_at or datetime.now(timezone.utc)).isoformat(),
            "message": {
                "role": "agent",
                "parts": [{"kind": "text", "text": _summary(plan)}],
                "messageId": f"msg-{plan.id}",
            },
        },
        "artifacts": artifacts,
        "metadata": {
            "spentMicros": plan.spent_micros,
            "budgetMicros": plan.budget_micros,
            "paymentAsset": asset_descriptor(),
            "engine": plan.engine,
            "jobs": [{"id": j.id, "status": j.status.value, "charged_micros": j.final_price_micros,
                      "output_hash": j.output_hash} for j in jobs],
            "receipts": [{"id": r.id, "sequence": r.sequence, "chain_hash": r.chain_hash}
                         for r in receipts],
            "trace": plan.trace,
        },
    }


def _summary(plan: Plan) -> str:
    result = plan.result or {}
    return (
        f"{plan.status}: {result.get('steps_succeeded', 0)}/{result.get('steps_total', 0)} steps, "
        f"spent {plan.spent_micros} of {plan.budget_micros} micro-units "
        f"({settings.asset_display} on {settings.network_label})."
    )


def _extract_text(message: dict) -> str:
    parts = message.get("parts") or []
    chunks = []
    for part in parts:
        if part.get("kind") == "text" and part.get("text"):
            chunks.append(part["text"])
        elif part.get("kind") == "data" and isinstance(part.get("data"), dict):
            goal = part["data"].get("goal")
            if goal:
                chunks.append(str(goal))
    return " ".join(chunks).strip()


def _rpc_error(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


@router.get("/.well-known/agent.json")
def well_known_agent() -> dict:
    return agent_card()


@router.get("/.well-known/agent-card.json")
def well_known_agent_card() -> dict:
    return agent_card()


@router.post("/a2a")
async def a2a_endpoint(request: Request, user: CurrentUser, db: Session = Depends(get_db)) -> dict:
    body = await request.json()
    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if method in ("message/send", "message/stream", "tasks/send"):
        message = params.get("message") or {}
        goal = _extract_text(message) or params.get("goal", "")
        if not goal:
            return _rpc_error(rpc_id, -32602, "message must contain a text part with the goal")
        budget = (params.get("metadata") or {}).get("budgetMicros")
        plan = run_plan(db, user, goal, int(budget) if budget else None)
        db.commit()
        return {"jsonrpc": "2.0", "id": rpc_id, "result": _task_from_plan(plan, db)}

    if method == "tasks/get":
        plan = db.get(Plan, params.get("id", ""))
        if plan is None:
            return _rpc_error(rpc_id, -32001, "task not found")
        if plan.owner_id != user.id and user.role.value != "admin":
            return _rpc_error(rpc_id, -32003, "not your task")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": _task_from_plan(plan, db)}

    if method == "tasks/cancel":
        plan = db.get(Plan, params.get("id", ""))
        if plan is None:
            return _rpc_error(rpc_id, -32001, "task not found")
        if plan.status in ("planning", "running"):
            plan.status = "failed"
            plan.error = "cancelled by requesting agent"
            db.commit()
            return {"jsonrpc": "2.0", "id": rpc_id, "result": _task_from_plan(plan, db)}
        return _rpc_error(rpc_id, -32002, "task is not cancelable")

    if method == "agent/getCard":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": agent_card()}

    return _rpc_error(rpc_id, -32601, f"method not found: {method}")
