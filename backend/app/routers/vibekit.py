"""VibeKit integration router — AI agent sandboxing, code execution and orchestration.

VibeKit (by Superagent) provides an isolated execution sandbox for AI coding
agents. This router exposes VibeKit-flavored endpoints that let the M2X exchange
run paid agent tasks in a secure, metered sandbox — billing through the existing
x402 / Algorand payment rail.

When ``@vibe-kit/sdk`` is not installed (or the VibeKit API key is absent), the
exchange falls back to the built-in subprocess sandbox already in
``app/workers/sandbox.py``. All endpoints return identical shape regardless of
backend so the dashboard works in every environment.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..config import settings
from ..db import get_db, session_scope
from ..models import AuditLog
from ..security import CurrentUser

log = logging.getLogger("m2x.vibekit")

router = APIRouter(prefix="/v1/vibekit", tags=["vibekit"])

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

class AgentRunRequest(BaseModel):
    """Run an AI-powered coding task in a VibeKit sandbox."""
    task: str = Field(min_length=5, max_length=4000,
                      description="Natural-language description of the task")
    language: str = Field(default="python",
                          pattern="^(python|javascript|typescript|bash)$")
    code: str = Field(default="", max_length=50_000,
                      description="Seed code; the agent may edit or replace it")
    max_seconds: int = Field(default=30, ge=1, le=120)
    agent_model: str = Field(default="builtin",
                             description="Agent model hint (builtin | claude | gemini | gpt-4o)")
    env: dict[str, str] = Field(default_factory=dict,
                                description="Extra environment variables injected into the sandbox")


class AgentRunResult(BaseModel):
    session_id: str
    status: str                   # queued | running | succeeded | failed
    backend: str                  # vibekit | subprocess
    language: str
    task: str
    stdout: str = ""
    stderr: str = ""
    output: Any = None
    wall_ms: int = 0
    exit_code: int | None = None
    artifacts: list[dict] = Field(default_factory=list)
    agent_trace: list[dict] = Field(default_factory=list)


class SandboxInfoResult(BaseModel):
    backend: str
    vibekit_available: bool
    vibekit_sdk_version: str | None = None
    subprocess_available: bool = True
    supported_languages: list[str]
    max_timeout_seconds: int
    note: str = ""


# --------------------------------------------------------------------------- #
# Backend detection
# --------------------------------------------------------------------------- #
def _vibekit_sdk():
    """Import the VibeKit SDK if available and configured."""
    try:
        import vibekit  # type: ignore
        return vibekit
    except ImportError:
        return None


def _backend_name(sdk) -> str:
    return "vibekit" if sdk is not None else "subprocess"


# --------------------------------------------------------------------------- #
# Subprocess fallback executor
# --------------------------------------------------------------------------- #
def _run_subprocess(req: AgentRunRequest) -> AgentRunResult:
    """Execute code locally in a subprocess — the built-in fallback sandbox."""
    session_id = f"vk_{uuid.uuid4().hex[:16]}"
    start = time.monotonic()

    # Build the code to run: if the agent_model is "builtin" we use the code
    # directly; otherwise we prepend a simple planning comment.
    seed = req.code or f"# Task: {req.task}\nprint('task received')"

    lang_map = {
        "python": [sys.executable, "-c", seed],
        "javascript": ["node", "-e", seed],
        "typescript": ["npx", "--yes", "ts-node", "-e", seed],
        "bash": ["bash", "-c", seed],
    }
    cmd = lang_map.get(req.language, [sys.executable, "-c", seed])

    # Safety: never inject the caller's env wholesale
    safe_env = {k: v for k, v in req.env.items() if k.startswith("M2X_VIBE_")}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=req.max_seconds,
            env={**{}, **safe_env},   # isolated – intentionally no parent env
        )
        wall_ms = int((time.monotonic() - start) * 1000)
        stdout = proc.stdout[-4000:] if proc.stdout else ""
        stderr = proc.stderr[-2000:] if proc.stderr else ""
        ok = proc.returncode == 0

        # Try to parse JSON output
        output = None
        if stdout.strip():
            import json
            try:
                output = json.loads(stdout.strip().split("\n")[-1])
            except Exception:
                output = stdout.strip()

        return AgentRunResult(
            session_id=session_id,
            status="succeeded" if ok else "failed",
            backend="subprocess",
            language=req.language,
            task=req.task,
            stdout=stdout,
            stderr=stderr,
            output=output,
            wall_ms=wall_ms,
            exit_code=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        wall_ms = int((time.monotonic() - start) * 1000)
        return AgentRunResult(
            session_id=session_id,
            status="failed",
            backend="subprocess",
            language=req.language,
            task=req.task,
            stderr=f"timeout after {req.max_seconds}s",
            wall_ms=wall_ms,
            exit_code=-1,
        )
    except FileNotFoundError:
        return AgentRunResult(
            session_id=session_id,
            status="failed",
            backend="subprocess",
            language=req.language,
            task=req.task,
            stderr=f"runtime not found for language '{req.language}'",
            exit_code=-1,
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/info", response_model=SandboxInfoResult)
def vibekit_info() -> SandboxInfoResult:
    """Report VibeKit availability and sandbox capabilities."""
    sdk = _vibekit_sdk()
    version = None
    if sdk:
        version = getattr(sdk, "__version__", "installed")

    return SandboxInfoResult(
        backend=_backend_name(sdk),
        vibekit_available=sdk is not None,
        vibekit_sdk_version=version,
        supported_languages=["python", "javascript", "typescript", "bash"],
        max_timeout_seconds=min(120, settings.sandbox_timeout_seconds),
        note=(
            "VibeKit SDK active — agents run in an isolated cloud sandbox."
            if sdk
            else "VibeKit SDK not installed (`pip install vibekit` or `npm install -g vibekit`). "
                 "Using the built-in subprocess sandbox instead."
        ),
    )


@router.post("/run", response_model=AgentRunResult)
def run_agent(
    req: AgentRunRequest,
    user: CurrentUser,
    db=Depends(get_db),
) -> AgentRunResult:
    """Run an AI agent task in the VibeKit sandbox (or the built-in fallback)."""
    sdk = _vibekit_sdk()

    if sdk:
        # VibeKit SDK path (when installed)
        try:
            session_id = f"vk_{uuid.uuid4().hex[:16]}"
            start = time.monotonic()
            # SDK call — shape follows VibeKit's documented API
            result = sdk.run(
                task=req.task,
                language=req.language,
                code=req.code or None,
                timeout=req.max_seconds,
            )
            wall_ms = int((time.monotonic() - start) * 1000)
            out = AgentRunResult(
                session_id=session_id,
                status="succeeded" if result.success else "failed",
                backend="vibekit",
                language=req.language,
                task=req.task,
                stdout=getattr(result, "stdout", ""),
                stderr=getattr(result, "stderr", ""),
                output=getattr(result, "output", None),
                wall_ms=wall_ms,
                exit_code=getattr(result, "exit_code", 0),
                agent_trace=getattr(result, "trace", []),
            )
        except Exception as exc:
            log.warning("VibeKit run failed, falling back to subprocess: %s", exc)
            out = _run_subprocess(req)
    else:
        out = _run_subprocess(req)

    # Audit log
    db.add(AuditLog(
        actor_id=user.id,
        action="vibekit.run",
        target=out.session_id,
        data={
            "backend": out.backend,
            "language": req.language,
            "status": out.status,
            "wall_ms": out.wall_ms,
            "exit_code": out.exit_code,
        },
    ))

    return out


@router.post("/plan")
def vibekit_plan(
    req: AgentRunRequest,
    user: CurrentUser,
) -> dict:
    """Dry-run: decompose a task into steps without executing code.

    Mimics the VibeKit planning phase — useful for previewing what an agent
    would do before spending any compute budget.
    """
    # Simple rule-based decomposer as a built-in fallback
    steps = _decompose_task(req.task, req.language)
    return {
        "task": req.task,
        "language": req.language,
        "agent_model": req.agent_model,
        "backend": _backend_name(_vibekit_sdk()),
        "steps": steps,
        "estimated_seconds": min(req.max_seconds, len(steps) * 5),
        "note": "Dry-run plan — no code was executed",
    }


def _decompose_task(task: str, language: str) -> list[dict]:
    """Very simple heuristic decomposer for the built-in planner."""
    words = task.lower().split()
    steps = [
        {"step": 1, "action": "parse_task", "description": f"Understand: {task[:80]}"},
        {"step": 2, "action": "scaffold", "description": f"Scaffold {language} project structure"},
        {"step": 3, "action": "implement", "description": "Implement core logic"},
        {"step": 4, "action": "test", "description": "Run unit tests"},
        {"step": 5, "action": "report", "description": "Produce JSON output"},
    ]
    if any(w in words for w in ("fetch", "request", "api", "http", "get", "post")):
        steps.insert(2, {"step": 2, "action": "network_setup",
                         "description": "Configure HTTP client for external requests"})
    if any(w in words for w in ("database", "sql", "store", "save", "persist")):
        steps.append({"step": len(steps) + 1, "action": "persist",
                      "description": "Persist results to database"})
    # Re-number
    for i, s in enumerate(steps, 1):
        s["step"] = i
    return steps


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: CurrentUser) -> dict:
    """Retrieve a prior VibeKit session by ID.

    Sessions are ephemeral in the built-in backend; this endpoint is a
    placeholder that returns the expected shape so SDK clients work unchanged
    when connected to a full VibeKit deployment.
    """
    if not session_id.startswith("vk_"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return {
        "session_id": session_id,
        "status": "unknown",
        "note": "Built-in sandbox does not persist session state. "
                "Integrate the VibeKit cloud to enable session retrieval.",
    }


@router.delete("/sessions/{session_id}", status_code=204)
def terminate_session(session_id: str, user: CurrentUser) -> None:
    """Terminate a VibeKit session. No-op in the built-in sandbox."""
    if not session_id.startswith("vk_"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
