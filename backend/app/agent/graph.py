"""The agent graph.

Uses LangGraph's ``StateGraph`` when the package is installed; otherwise an
embedded engine with the same semantics (nodes, conditional edges, END) drives
the identical node functions, so behaviour is the same with or without the
dependency.

    plan -> discover -> quote -> pay -> execute -> verify -> settle -> report
                ^                                    |
                +--------------- next step ----------+
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..algorand import asset_descriptor
from ..algorand import asset_id as mandated_asset_id
from ..bazaar.discovery import discovery
from ..config import settings
from ..models import Job, JobStatus, Plan, Service, User
from ..observability import agent_runs, agent_steps
from ..services import jobs as job_service
from ..services import ledger
from ..x402.protocol import build_exact_payload, encode_payment_header
from .planner import PlanStep, decompose, rank_services

log = logging.getLogger("m2x.agent")
END = "__end__"


# --------------------------------------------------------------------------- #
# Minimal LangGraph-compatible engine (used when langgraph isn't installed)
# --------------------------------------------------------------------------- #
class _FallbackGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, Callable] = {}
        self.edges: dict[str, str] = {}
        self.conditional: dict[str, tuple[Callable, dict[str, str]]] = {}
        self.entry: str | None = None

    def add_node(self, name: str, fn: Callable) -> None:
        self.nodes[name] = fn

    def add_edge(self, src: str, dst: str) -> None:
        self.edges[src] = dst

    def add_conditional_edges(self, src: str, router: Callable, mapping: dict[str, str]) -> None:
        self.conditional[src] = (router, mapping)

    def set_entry_point(self, name: str) -> None:
        self.entry = name

    def compile(self) -> "_FallbackGraph":
        return self

    def invoke(self, state: "AgentState", max_iterations: int = 200) -> "AgentState":
        node = self.entry
        iterations = 0
        while node and node != END:
            if iterations >= max_iterations:
                state.error = "agent graph exceeded iteration budget"
                break
            iterations += 1
            state = self.nodes[node](state)
            if node in self.conditional:
                router, mapping = self.conditional[node]
                node = mapping.get(router(state), END)
            else:
                node = self.edges.get(node, END)
        return state


class _LangGraphAdapter:  # pragma: no cover - exercised when langgraph is installed
    """Runs the same node functions on the real LangGraph runtime.

    State travels as ``{"agent": AgentState}``; nodes mutate in place and return
    the same key, which is exactly how LangGraph merges dict channel updates.
    """

    def __init__(self, compiled) -> None:
        self.compiled = compiled

    def invoke(self, state: "AgentState") -> "AgentState":
        out = self.compiled.invoke({"agent": state}, {"recursion_limit": 200})
        return out["agent"]


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class AgentState:
    db: Session
    owner: User
    plan: Plan
    goal: str
    budget_micros: int
    steps: list[PlanStep] = field(default_factory=list)
    cursor: int = 0
    spent_micros: int = 0
    trace: list[dict] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    current_service: Service | None = None
    current_job: Job | None = None
    current_payment: Any = None
    quote: dict | None = None
    error: str = ""
    finished: bool = False
    max_steps: int = settings.agent_max_steps

    def log(self, node: str, message: str, **data) -> None:
        entry = {
            "node": node,
            "message": message,
            "at": datetime.now(timezone.utc).isoformat(),
            "step": self.cursor,
            **data,
        }
        self.trace.append(entry)
        agent_steps.labels(node).inc()
        log.info("agent[%s] %s: %s", self.plan.id, node, message)

    @property
    def step(self) -> PlanStep | None:
        return self.steps[self.cursor] if 0 <= self.cursor < len(self.steps) else None

    @property
    def remaining_budget(self) -> int:
        return max(self.budget_micros - self.spent_micros, 0)


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def node_plan(state: AgentState) -> AgentState:
    state.steps = decompose(state.goal, max_steps=state.max_steps)
    state.plan.steps = [s.as_dict() for s in state.steps]
    state.plan.status = "running"
    state.log("plan", f"decomposed goal into {len(state.steps)} step(s)",
              steps=[s.goal for s in state.steps])
    return state


def node_discover(state: AgentState) -> AgentState:
    state.current_service = None
    step = state.step
    if step is None:
        state.finished = True
        return state

    discovery.refresh(state.db)
    listing = discovery.best_for(
        state.db, step.capability + " " + " ".join(step.keywords),
        budget_micros=state.remaining_budget,
    )
    candidates = rank_services(state.db, step, budget_micros=state.remaining_budget)

    chosen = None
    if candidates:
        chosen = candidates[0]
    elif listing and listing.get("service_id"):
        svc = state.db.get(Service, listing["service_id"])
        if svc:
            chosen = {"service_id": svc.id, "slug": svc.slug, "provider_slug": "",
                      "price_micros": svc.max_price_micros, "score": 0.0}

    if not chosen:
        step.status = "unmatched"
        step.error = (
            f"no affordable service from provider '{step.provider_hint}' matched this step"
            if step.provider_hint
            else "no affordable service matched this capability"
        )
        state.log("discover", step.error, capability=step.capability,
                  provider_hint=step.provider_hint, budget=state.remaining_budget)
        state.cursor += 1
        return state

    service = state.db.get(Service, chosen["service_id"])
    state.current_service = service
    step.service_id = service.id
    step.service_slug = service.slug
    step.provider_slug = chosen.get("provider_slug", "")
    step.status = "selected"
    state.log("discover", f"selected {service.slug}",
              candidates=[c["slug"] for c in candidates[:5]],
              bazaar_sources=discovery.status(state.db)["counts"])
    return state


def node_quote(state: AgentState) -> AgentState:
    step, service = state.step, state.current_service
    if step is None or service is None:
        return state
    payload = _payload_for(state, step)
    state.quote = job_service.quote(state.db, service, payload)
    step.estimated_micros = state.quote["max_price_micros"]
    if step.estimated_micros > state.remaining_budget:
        step.status = "over_budget"
        step.error = f"quote {step.estimated_micros} exceeds remaining budget {state.remaining_budget}"
        state.log("quote", step.error)
        state.cursor += 1
        return state
    state.context["payload"] = payload
    step.asset_id = state.quote.get("asset_id", mandated_asset_id())
    state.log("quote",
              f"quoted {step.estimated_micros} micros of ASA {step.asset_id} for {service.slug}",
              quote=state.quote["estimated"], asset_id=step.asset_id)
    return state


def node_pay(state: AgentState) -> AgentState:
    step, service = state.step, state.current_service
    if step is None or service is None:
        return state
    try:
        job, payment, _q = job_service.create_job(
            state.db,
            consumer=state.owner,
            service=service,
            payload=state.context.get("payload", {}),
            plan_id=state.plan.id,
        )
    except job_service.JobError as exc:
        step.status = "failed"
        step.error = str(exc)
        state.log("pay", f"job creation refused: {exc}")
        state.cursor += 1
        return state

    state.current_job, state.current_payment = job, payment
    step.job_id = job.id

    # The agent settles its own x402 invoice: sign the authorization, send the header.
    payload = build_exact_payload(
        payer=state.owner.id,
        pay_to=payment.pay_to,
        value_micros=payment.amount_micros,
        nonce=payment.nonce,
        resource=payment.resource,
        payer_secret=state.owner.payment_secret,
        asset=payment.asset_id or None,
    )
    header = encode_payment_header(payload)
    try:
        result = job_service.apply_payment(state.db, job, payment, header)
    except Exception as exc:
        step.status = "failed"
        step.error = f"payment failed: {exc}"
        state.log("pay", step.error)
        state.cursor += 1
        return state
    state.log("pay", f"x402 escrow funded ({payment.amount_micros} micros)",
              job_id=job.id, payment_id=payment.id, result=result)
    return state


def node_execute(state: AgentState) -> AgentState:
    step, job = state.step, state.current_job
    if step is None or job is None:
        return state
    try:
        job_service.execute_job(state.db, job)
    except Exception as exc:  # sandbox blowups must not kill the plan
        step.status = "failed"
        step.error = str(exc)[:300]
        state.log("execute", f"execution error: {exc}")
        state.cursor += 1
        return state
    state.log("execute", f"job {job.id} -> {job.status.value}",
              job_id=job.id, attempts=job.attempts)
    return state


def node_verify(state: AgentState) -> AgentState:
    step, job = state.step, state.current_job
    if step is None or job is None:
        return state
    ok = job.status == JobStatus.succeeded and job.integrity_verified
    step.status = "succeeded" if ok else "failed"
    if not ok:
        step.error = job.error or "integrity verification failed"
    else:
        step.output = (job.result or {}).get("output")
        state.context["last_output"] = step.output
    state.log("verify", "integrity verified" if ok else f"verification failed: {step.error}",
              output_hash=job.output_hash, integrity=job.integrity_verified)
    return state


def node_settle(state: AgentState) -> AgentState:
    step, job = state.step, state.current_job
    if job is not None:
        state.spent_micros += job.final_price_micros
        state.plan.spent_micros = state.spent_micros
    if step is not None:
        state.log("settle", f"charged {job.final_price_micros if job else 0} micros",
                  spent=state.spent_micros, remaining=state.remaining_budget)
    state.cursor += 1
    state.current_job = None
    state.current_service = None
    state.current_payment = None
    return state


def node_report(state: AgentState) -> AgentState:
    for pending in state.steps:
        if pending.status == "pending":
            pending.status = "skipped"
            pending.error = pending.error or "plan ended before this step ran"
    succeeded = [s for s in state.steps if s.status == "succeeded"]
    failed = [s for s in state.steps if s.status in ("failed", "unmatched", "over_budget")]
    state.plan.steps = [s.as_dict() for s in state.steps]
    state.plan.trace = state.trace
    state.plan.spent_micros = state.spent_micros
    state.plan.status = "completed" if succeeded and not failed else (
        "partial" if succeeded else "failed"
    )
    state.plan.result = {
        "goal": state.goal,
        "steps_total": len(state.steps),
        "steps_succeeded": len(succeeded),
        "steps_failed": len(failed),
        "spent_micros": state.spent_micros,
        "budget_micros": state.budget_micros,
        "payment_asset": asset_descriptor(),
        "summary": _narrate(state, succeeded, failed),
        "providers_used": sorted({s.provider_slug or s.provider_hint
                                  for s in succeeded if s.provider_slug or s.provider_hint}),
        "final_output": state.context.get("last_output"),
        "outputs": [{"step": s.index, "goal": s.goal, "output": s.output} for s in succeeded],
        "failures": [{"step": s.index, "goal": s.goal, "error": s.error} for s in failed],
    }
    state.plan.finished_at = datetime.now(timezone.utc)
    state.finished = True
    state.log("report", f"plan {state.plan.status}: {len(succeeded)}/{len(state.steps)} steps")
    agent_runs.labels(state.plan.status).inc()
    return state


def _narrate(state: AgentState, succeeded: list[PlanStep], failed: list[PlanStep]) -> str:
    """A short natural-language answer to sit alongside the structured result.

    Providers that already summarize their own output — an on-chain envelope
    carries ``data.summary`` — are quoted rather than re-described.
    """
    lines: list[str] = []
    for step in succeeded:
        output = step.output if isinstance(step.output, dict) else {}
        summary = (output.get("data") or {}).get("summary") or output.get("summary")
        provider = step.provider_slug or step.provider_hint
        prefix = f"{provider}: " if provider else ""
        lines.append(f"{prefix}{summary}" if summary else f"{prefix}{step.goal} — completed.")
    for step in failed:
        lines.append(f"{step.goal} — not completed ({step.error or 'no detail'}).")
    spent = state.spent_micros / 1_000_000
    lines.append(
        f"{len(succeeded)}/{len(state.steps)} step(s) completed for {spent:.6f} "
        f"{settings.algorand_asset_unit_name} on {settings.blockchain} {settings.network_label}."
    )
    return "\n".join(lines)


def _payload_for(state: AgentState, step: PlanStep) -> dict:
    payload = {
        "goal": step.goal,
        "capability": step.capability,
        "keywords": step.keywords,
        "input": state.context.get("last_output"),
        "plan_id": state.plan.id,
        "step_index": step.index,
    }
    # Parameters the planner resolved for this step — a wallet address, the
    # concrete provider capability, a chain filter — override the generic ones,
    # because that is what an external provider is actually being asked for.
    payload.update(step.params or {})
    return payload


# --------------------------------------------------------------------------- #
# Routers
# --------------------------------------------------------------------------- #
def _after_discover(state: AgentState) -> str:
    if state.cursor >= len(state.steps):
        return "report"
    # An unmatched step already advanced the cursor: keep shopping for the rest
    # of the plan instead of abandoning it.
    return "discover" if state.current_service is None else "quote"


def _after_quote(state: AgentState) -> str:
    if state.step is None:
        return "report"
    if state.step.status in ("over_budget", "failed"):
        return "discover" if state.cursor < len(state.steps) else "report"
    return "pay"


def _continue_or_report(state: AgentState) -> str:
    if state.cursor >= len(state.steps):
        return "report"
    if state.remaining_budget <= 0:
        state.log("budget", "budget exhausted, stopping early")
        for remaining in state.steps[state.cursor:]:
            remaining.status = "skipped"
            remaining.error = "budget exhausted"
        return "report"
    return "discover"


def _after_pay(state: AgentState) -> str:
    if state.step is None or state.current_job is None:
        return "discover" if state.cursor < len(state.steps) else "report"
    return "execute"


def _after_execute(state: AgentState) -> str:
    if state.current_job is None:
        return "discover" if state.cursor < len(state.steps) else "report"
    return "verify"


# --------------------------------------------------------------------------- #
# Graph assembly
# --------------------------------------------------------------------------- #
NODES = {
    "plan": node_plan,
    "discover": node_discover,
    "quote": node_quote,
    "pay": node_pay,
    "execute": node_execute,
    "verify": node_verify,
    "settle": node_settle,
    "report": node_report,
}
ROUTES = {
    # "discover" must map to itself: an unmatched step advances the cursor and
    # routes back here for the next one. Without the self-edge the plan ends on
    # the first unmatched step and never reaches "report".
    "discover": (_after_discover, {"discover": "discover", "quote": "quote", "report": "report"}),
    "quote": (_after_quote, {"pay": "pay", "discover": "discover", "report": "report"}),
    "pay": (_after_pay, {"execute": "execute", "discover": "discover", "report": "report"}),
    "execute": (_after_execute, {"verify": "verify", "discover": "discover", "report": "report"}),
    "settle": (_continue_or_report, {"discover": "discover", "report": "report"}),
}
STATIC_EDGES = {"plan": "discover", "verify": "settle"}


def _build_langgraph():  # pragma: no cover - requires langgraph installed
    from langgraph.graph import END as LG_END
    from langgraph.graph import StateGraph

    graph = StateGraph(dict)
    for name, fn in NODES.items():
        graph.add_node(name, (lambda f: lambda s: {"agent": f(s["agent"])})(fn))
    graph.set_entry_point("plan")
    for src, dst in STATIC_EDGES.items():
        graph.add_edge(src, dst)
    for src, (router, mapping) in ROUTES.items():
        graph.add_conditional_edges(src, (lambda r: lambda s: r(s["agent"]))(router), mapping)
    graph.add_edge("report", LG_END)
    return _LangGraphAdapter(graph.compile())


def build_agent_graph():
    """LangGraph when available, identical embedded engine otherwise."""
    try:  # pragma: no cover
        return _build_langgraph()
    except Exception:
        pass
    graph = _FallbackGraph()
    graph.add_node("plan", node_plan)
    graph.add_node("discover", node_discover)
    graph.add_node("quote", node_quote)
    graph.add_node("pay", node_pay)
    graph.add_node("execute", node_execute)
    graph.add_node("verify", node_verify)
    graph.add_node("settle", node_settle)
    graph.add_node("report", node_report)

    graph.set_entry_point("plan")
    graph.add_edge("plan", "discover")
    graph.add_conditional_edges("discover", _after_discover,
                                {"discover": "discover", "quote": "quote", "report": "report"})
    graph.add_conditional_edges("quote", _after_quote,
                                {"pay": "pay", "discover": "discover", "report": "report"})
    graph.add_conditional_edges("pay", _after_pay,
                                {"execute": "execute", "discover": "discover", "report": "report"})
    graph.add_conditional_edges("execute", _after_execute,
                                {"verify": "verify", "discover": "discover", "report": "report"})
    graph.add_edge("verify", "settle")
    graph.add_conditional_edges("settle", _continue_or_report,
                                {"discover": "discover", "report": "report"})
    graph.add_edge("report", END)
    return graph.compile()


def engine_name() -> str:
    try:  # pragma: no cover
        import langgraph  # noqa: F401

        return "langgraph"
    except Exception:
        return "builtin"


def run_plan(db: Session, owner: User, goal: str, budget_micros: int | None = None,
             max_steps: int | None = None) -> Plan:
    budget = budget_micros or settings.agent_default_budget_micros
    balance = ledger.get_account(db, owner.id).available_micros
    budget = min(budget, balance)

    plan = Plan(owner_id=owner.id, goal=goal, budget_micros=budget, engine=engine_name())
    db.add(plan)
    db.flush()

    state = AgentState(
        db=db, owner=owner, plan=plan, goal=goal, budget_micros=budget,
        max_steps=max_steps or settings.agent_max_steps,
    )
    graph = build_agent_graph()
    try:
        graph.invoke(state)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("agent plan crashed")
        plan.status = "failed"
        plan.error = str(exc)[:500]
        plan.trace = state.trace
        agent_runs.labels("crashed").inc()
    db.flush()
    return plan
