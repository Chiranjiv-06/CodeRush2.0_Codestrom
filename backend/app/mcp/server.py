"""MCP (Model Context Protocol) server.

JSON-RPC 2.0 over HTTP at ``POST /mcp`` implementing ``initialize``,
``tools/list``, ``tools/call``, ``resources/list``, ``resources/read`` and
``ping``. Any MCP-capable model or agent can discover services, get quotes, pay
via x402 and run jobs on the exchange through this endpoint.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.graph import run_plan
from ..algorand import asset_descriptor
from ..bazaar.discovery import discovery
from ..config import settings
from ..db import get_db
from ..integrity import canonical_json
from ..models import Job, Provider, Receipt, Service, User
from ..security import CurrentUser
from ..services import jobs as job_service
from ..services import ledger, receipts, reputation

log = logging.getLogger("m2x.mcp")
router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2024-11-05"


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def tool_discover_services(db: Session, user: User, *, query: str = "",
                           max_price_micros: int | None = None, limit: int = 10) -> dict:
    discovery.refresh(db)
    db.commit()
    items = discovery.search(db, query=query, max_price_micros=max_price_micros, limit=limit)
    return {"count": len(items), "listings": items}


def tool_get_quote(db: Session, user: User, *, service_id: str, payload: dict | None = None) -> dict:
    service = db.get(Service, service_id)
    if service is None:
        raise RpcError(-32602, f"unknown service_id {service_id}")
    return job_service.quote(db, service, payload or {})


def tool_run_job(db: Session, user: User, *, service_id: str, payload: dict | None = None,
                 max_price_micros: int | None = None) -> dict:
    service = db.get(Service, service_id)
    if service is None:
        raise RpcError(-32602, f"unknown service_id {service_id}")
    try:
        job, payment, quote = job_service.create_job(
            db, consumer=user, service=service, payload=payload or {},
            max_price_micros=max_price_micros,
        )
        job_service.autopay(db, job, payment, user)
        job_service.execute_job(db, job)
    except job_service.JobError as exc:
        raise RpcError(-32000, str(exc))
    db.commit()
    return {
        "job_id": job.id,
        "status": job.status.value,
        "result": (job.result or {}).get("output"),
        "stdout": (job.result or {}).get("stdout", "")[-2000:],
        "error": job.error,
        "charged_micros": job.final_price_micros,
        "output_hash": job.output_hash,
        "integrity_verified": job.integrity_verified,
        "quote": quote["max_price_micros"],
        "tx_hash": payment.tx_hash,
        "asset_id": payment.asset_id,
        "payment_asset": asset_descriptor(),
    }


def tool_get_job(db: Session, user: User, *, job_id: str) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise RpcError(-32602, f"unknown job_id {job_id}")
    if job.consumer_id != user.id and user.role.value != "admin":
        raise RpcError(-32001, "not your job")
    return {
        "job_id": job.id, "status": job.status.value, "result": job.result,
        "error": job.error, "charged_micros": job.final_price_micros,
        "attempts": job.attempts, "integrity_verified": job.integrity_verified,
    }


def tool_verify_receipt(db: Session, user: User, *, receipt_id: str) -> dict:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise RpcError(-32602, f"unknown receipt_id {receipt_id}")
    return receipts.verify_receipt(receipt)


def tool_provider_reputation(db: Session, user: User, *, provider_id: str) -> dict:
    provider = db.get(Provider, provider_id) or db.scalar(
        select(Provider).where(Provider.slug == provider_id)
    )
    if provider is None:
        raise RpcError(-32602, f"unknown provider {provider_id}")
    return reputation.provider_stats(db, provider)


def tool_wallet_balance(db: Session, user: User) -> dict:
    return ledger.balance_summary(db, user.id)


def tool_plan_and_execute(db: Session, user: User, *, goal: str,
                          budget_micros: int | None = None) -> dict:
    plan = run_plan(db, user, goal, budget_micros)
    db.commit()
    return {"plan_id": plan.id, "status": plan.status, "engine": plan.engine,
            "spent_micros": plan.spent_micros, "result": plan.result,
            "steps": plan.steps}


def tool_onchain_intelligence(db: Session, user: User, *, capability: str = "wallet_analysis",
                              wallet: str = "", chain: str = "", query: str = "",
                              limit: int | None = None) -> dict:
    """Buy one Zerion capability through the full paid job path."""
    from ..integrations.zerion import CAPABILITIES, PROVIDER_ID
    from ..models import JobStatus, Provider

    entry = CAPABILITIES.get(capability)
    if entry is None:
        raise RpcError(-32602, f"unknown capability {capability}; "
                               f"supported: {sorted(CAPABILITIES)}")
    provider = db.scalar(select(Provider).where(Provider.slug == PROVIDER_ID))
    service = db.scalar(
        select(Service).where(Service.provider_id == (provider.id if provider else ""),
                              Service.slug == entry.slug)
    ) if provider else None
    if service is None:
        raise RpcError(-32000, "the Zerion provider is not registered on this exchange")

    payload: dict[str, Any] = {"capability": capability}
    for key, value in (("wallet", wallet), ("chain", chain), ("query", query)):
        if value:
            payload[key] = value
    if limit is not None:
        payload["limit"] = limit

    try:
        job, payment, quote = job_service.create_job(
            db, consumer=user, service=service, payload=payload
        )
        job_service.autopay(db, job, payment, user)
        job_service.execute_job(db, job)
    except job_service.JobError as exc:
        raise RpcError(-32000, str(exc))
    db.commit()

    envelope = (job.result or {}).get("output") if job.status == JobStatus.succeeded else None
    return {
        "job_id": job.id,
        "status": job.status.value,
        "capability": capability,
        "summary": ((envelope or {}).get("data") or {}).get("summary", ""),
        "result": envelope,
        "error": job.error,
        "integrity_hash": ((envelope or {}).get("integrity") or {}).get("hash", ""),
        "consumer_charged_micros": job.final_price_micros,
        "consumer_payment_asset": asset_descriptor(),
        "provider_payment": (envelope or {}).get("payment", {}),
        "quote": quote["max_price_micros"],
    }


TOOLS: dict[str, dict[str, Any]] = {
    "discover_services": {
        "fn": tool_discover_services,
        "description": "Search the Bazaar discovery index (local exchange + GoPlausible) for paid services.",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "free-text capability search"},
                "max_price_micros": {"type": "integer",
                                     "description": "price ceiling, in micro-units of the payment asset"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    "get_quote": {
        "fn": tool_get_quote,
        "description": "Price a service invocation before committing funds.",
        "schema": {
            "type": "object",
            "properties": {"service_id": {"type": "string"}, "payload": {"type": "object"}},
            "required": ["service_id"],
        },
    },
    "run_job": {
        "fn": tool_run_job,
        "description": "Pay via x402 and execute a service in an ephemeral sandbox; returns the verified result.",
        "schema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "payload": {"type": "object"},
                "max_price_micros": {"type": "integer"},
            },
            "required": ["service_id"],
        },
    },
    "get_job": {
        "fn": tool_get_job,
        "description": "Fetch status and result of a previously submitted job.",
        "schema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                   "required": ["job_id"]},
    },
    "verify_receipt": {
        "fn": tool_verify_receipt,
        "description": "Verify a settlement receipt's SHA-256 body hash, chain link and signature.",
        "schema": {"type": "object", "properties": {"receipt_id": {"type": "string"}},
                   "required": ["receipt_id"]},
    },
    "provider_reputation": {
        "fn": tool_provider_reputation,
        "description": "Reputation score, tier, success rate and latency for a provider.",
        "schema": {"type": "object", "properties": {"provider_id": {"type": "string"}},
                   "required": ["provider_id"]},
    },
    "wallet_balance": {
        "fn": tool_wallet_balance,
        "description": "Available and escrowed balance for the calling principal.",
        "schema": {"type": "object", "properties": {}},
    },
    "plan_and_execute": {
        "fn": tool_plan_and_execute,
        "description": "Run the agent planner end-to-end for a natural-language goal within a budget.",
        "schema": {
            "type": "object",
            "properties": {"goal": {"type": "string"}, "budget_micros": {"type": "integer"}},
            "required": ["goal"],
        },
    },
    "onchain_intelligence": {
        "fn": tool_onchain_intelligence,
        "description": (
            "Buy on-chain wallet intelligence from Zerion through the exchange: portfolio, "
            "token positions, DeFi positions, PnL, transaction history, token search or a "
            "full wallet analysis. Pays x402 in the exchange's asset, settles the Zerion "
            "leg on Zerion's own rail, and returns a normalized, hash-verified result."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "enum": ["wallet_analysis", "portfolio", "positions", "defi_positions",
                             "pnl", "transactions", "token_search", "chains"],
                    "default": "wallet_analysis",
                },
                "wallet": {"type": "string",
                           "description": "EVM address, Solana address or .eth ENS name"},
                "chain": {"type": "string", "description": "optional chain id filter"},
                "query": {"type": "string", "description": "token_search only"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
}

RESOURCES = {
    "m2x://services": ("Active service catalog", "application/json"),
    "m2x://bazaar": ("Federated discovery index", "application/json"),
    "m2x://receipts/chain": ("Receipt hash-chain status", "application/json"),
    "m2x://wallet": ("Caller wallet balance", "application/json"),
}


def _read_resource(db: Session, user: User, uri: str) -> Any:
    if uri == "m2x://services":
        rows = db.scalars(select(Service).where(Service.is_active.is_(True)).limit(200)).all()
        return [{"id": s.id, "slug": s.slug, "name": s.name, "category": s.category,
                 "runtime": s.runtime, "max_price_micros": s.max_price_micros,
                 "description": s.description} for s in rows]
    if uri == "m2x://bazaar":
        return discovery.search(db, limit=200)
    if uri == "m2x://receipts/chain":
        return {**receipts.receipt_stats(db), **receipts.verify_chain(db)}
    if uri == "m2x://wallet":
        return ledger.balance_summary(db, user.id)
    raise RpcError(-32602, f"unknown resource {uri}")


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch
# --------------------------------------------------------------------------- #
def _handle(method: str, params: dict, db: Session, user: User) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False}},
            "serverInfo": {"name": "m2x-exchange", "version": settings.version},
            "paymentAsset": asset_descriptor(),
            "instructions": (
                "Machine-to-machine compute and tool exchange. Discover services, quote them, "
                "pay with x402 and execute in ephemeral sandboxes. All prices are micro-units "
                f"of {settings.asset_display} on Algorand {settings.network_label} "
                "(1 unit = 1000000 micros); no other asset is accepted."
            ),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {"name": name, "description": spec["description"], "inputSchema": spec["schema"]}
                for name, spec in TOOLS.items()
            ]
        }
    if method == "tools/call":
        name = params.get("name")
        spec = TOOLS.get(name)
        if spec is None:
            raise RpcError(-32602, f"unknown tool {name}")
        args = params.get("arguments") or {}
        fn: Callable = spec["fn"]
        try:
            result = fn(db, user, **args)
        except RpcError:
            raise
        except TypeError as exc:
            raise RpcError(-32602, f"invalid arguments for {name}: {exc}")
        return {
            "content": [{"type": "text", "text": canonical_json(result)}],
            "structuredContent": result,
            "isError": False,
        }
    if method == "resources/list":
        return {
            "resources": [
                {"uri": uri, "name": uri.split("//")[-1], "description": desc, "mimeType": mime}
                for uri, (desc, mime) in RESOURCES.items()
            ]
        }
    if method == "resources/read":
        uri = params.get("uri", "")
        data = _read_resource(db, user, uri)
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": canonical_json(data)}]}
    raise RpcError(-32601, f"method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request, user: CurrentUser, db: Session = Depends(get_db)) -> Any:
    body = await request.json()
    batch = isinstance(body, list)
    messages = body if batch else [body]
    responses = []

    for message in messages:
        rpc_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}
        try:
            result = _handle(method, params, db, user)
            if rpc_id is not None:
                responses.append({"jsonrpc": "2.0", "id": rpc_id, "result": result})
        except RpcError as exc:
            log.info("mcp error on %s: %s", method, exc.message)
            responses.append({"jsonrpc": "2.0", "id": rpc_id,
                              "error": {"code": exc.code, "message": exc.message,
                                        "data": exc.data}})
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("mcp internal error")
            responses.append({"jsonrpc": "2.0", "id": rpc_id,
                              "error": {"code": -32603, "message": f"internal error: {exc}"}})

    if not responses:
        return {"jsonrpc": "2.0", "result": None, "id": None}
    return responses if batch else responses[0]


@router.get("/mcp/manifest")
def mcp_manifest() -> dict:
    """Static description of the MCP surface for clients that prefer HTTP discovery."""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "m2x-exchange",
        "version": settings.version,
        "transport": {"type": "http", "endpoint": "/mcp", "auth": "Bearer JWT or X-API-Key"},
        "tools": [{"name": n, "description": s["description"], "inputSchema": s["schema"]}
                  for n, s in TOOLS.items()],
        "resources": [{"uri": u, "description": d} for u, (d, _m) in RESOURCES.items()],
    }
