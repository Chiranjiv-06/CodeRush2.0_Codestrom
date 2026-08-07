"""Zerion CLI transport.

Runs the officially distributed ``zerion`` CLI as a subprocess. This is the path
Zerion documents for x402 pay-per-request: the CLI performs the 402 handshake,
signs the USDC transfer and retries, so signing keys stay inside a tool built to
hold them.

Execution rules, all enforced here:

* the binary is resolved with :func:`shutil.which` — never invoked through a shell;
* arguments are passed as a list, never interpolated into a command string;
* only subcommands in :data:`~app.integrations.zerion.models.ALLOWED_CLI_COMMANDS`
  may run, and only with flags this module constructs itself;
* every value came from a :class:`ZerionRequestSpec`, so it already matched a
  strict address / chain / query pattern;
* a wall-clock timeout always applies, and the child is killed on expiry;
* the child gets a scrubbed environment containing only the variables the CLI
  needs — and nothing is ever logged from it.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any

from ...config import settings
from .client import ZerionRawResult
from .errors import (
    ZerionAuthError,
    ZerionRateLimitError,
    ZerionResponseError,
    ZerionTimeoutError,
    ZerionUnavailableError,
    sanitize,
)
from .models import ALLOWED_CLI_COMMANDS, ZerionRequestSpec
from .payment import cli_probe

log = logging.getLogger("m2x.zerion.cli")

# Error codes the CLI emits on stderr, mapped onto our structured errors.
_ERROR_CODES = {
    "unauthorized": ZerionAuthError,
    "invalid_api_key": ZerionAuthError,
    "payment_required": ZerionUnavailableError,
    "rate_limited": ZerionRateLimitError,
    "too_many_requests": ZerionRateLimitError,
}

# Environment the child is allowed to see. Credentials are forwarded because the
# CLI cannot work without them; nothing here is ever written to a log, a job
# result, a receipt or an API response.
_CREDENTIAL_ENV = (
    "ZERION_API_KEY",
    "WALLET_PRIVATE_KEY",
    "EVM_PRIVATE_KEY",
    "SOLANA_PRIVATE_KEY",
    "ETH_RPC_URL",
    "SOLANA_RPC_URL",
)
_PASSTHROUGH_ENV = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT",
    "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL", "NODE_PATH", "XDG_CONFIG_HOME",
)


def _child_env(*, use_x402: bool) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
    for key in _CREDENTIAL_ENV:
        if os.environ.get(key):
            env[key] = os.environ[key]

    # Settings win over ambient environment: a deployment that configured the
    # exchange explicitly should not be overridden by a stray shell variable.
    if settings.zerion_api_key and not use_x402:
        env["ZERION_API_KEY"] = settings.zerion_api_key
    if use_x402:
        # In x402 mode the API key is withheld so the CLI cannot silently fall
        # back to subscription billing when we asked to pay per request.
        env.pop("ZERION_API_KEY", None)
        env["ZERION_X402"] = "true"
        if settings.zerion_evm_private_key:
            env["EVM_PRIVATE_KEY"] = settings.zerion_evm_private_key
        if settings.zerion_solana_private_key:
            env["SOLANA_PRIVATE_KEY"] = settings.zerion_solana_private_key
        if settings.zerion_x402_prefer_solana:
            env["ZERION_X402_PREFER_SOLANA"] = "true"
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    return env


def _build_args(spec: ZerionRequestSpec, *, use_x402: bool) -> list[str]:
    """Assemble the argument vector. Every element is a literal or validated."""
    command = spec.capability.cli_command
    if command not in ALLOWED_CLI_COMMANDS:  # pragma: no cover - registry invariant
        raise ZerionResponseError(f"refusing to run unlisted Zerion command {command!r}")

    args: list[str] = [command]
    if spec.capability.needs_wallet:
        args.append(spec.wallet)
    elif spec.capability.needs_query:
        args.append(spec.query)

    if spec.capability.key == "defi_positions":
        args += ["--positions", "defi", "--defi"]
    elif spec.capability.key == "positions":
        args += ["--positions", "simple"]
    if spec.capability.key == "transactions":
        args += ["--limit", str(spec.limit)]
    if spec.chain:
        args += ["--chain", spec.chain]

    args.append("--json")
    if use_x402:
        args.append("--x402")
    return args


def _parse_json_stream(text: str) -> Any:
    """Last complete JSON document on the stream wins."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _payment_evidence(stdout_doc: Any, stderr_doc: Any) -> dict[str, Any]:
    """Pull any x402 settlement detail the CLI surfaced. Never invents one."""
    evidence: dict[str, Any] = {}
    for doc in (stdout_doc, stderr_doc):
        if not isinstance(doc, dict):
            continue
        block = doc.get("x402") or doc.get("payment") or {}
        if isinstance(block, dict):
            for src, dst in (("transaction", "transaction"), ("txHash", "transaction"),
                             ("hash", "transaction"), ("network", "network"),
                             ("chain", "network"), ("amount", "amount"),
                             ("currency", "currency")):
                if block.get(src) and dst not in evidence:
                    evidence[dst] = sanitize(block[src])
    return evidence


class ZerionCliClient:
    """Executes one allowlisted Zerion CLI command per request."""

    source = "zerion_cli"

    def __init__(self, *, command_path: str | None = None, timeout: float | None = None) -> None:
        self._command_path = command_path
        self.timeout = timeout or settings.zerion_timeout_seconds

    @property
    def command_path(self) -> str | None:
        return self._command_path or cli_probe.path()

    def available(self) -> bool:
        return self.command_path is not None

    def version(self) -> str:
        """Best-effort CLI version string; empty when the CLI is absent."""
        path = self.command_path
        if not path:
            return ""
        try:
            proc = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10,
                env=_child_env(use_x402=False),
            )
            return sanitize((proc.stdout or proc.stderr or "").strip())[:64]
        except Exception:
            return ""

    def execute(self, spec: ZerionRequestSpec, *, use_x402: bool) -> ZerionRawResult:
        path = self.command_path
        if not path:
            raise ZerionUnavailableError(
                "the Zerion CLI is not installed or not on PATH "
                "(`npm install -g zerion-cli`)",
                command=settings.zerion_cli_command,
            )

        args = _build_args(spec, use_x402=use_x402)
        timeout = max(self.timeout * max(spec.upstream_requests, 1), 10.0)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                [path, *args],                 # list form: no shell, no injection surface
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=_child_env(use_x402=use_x402),
                stdin=subprocess.DEVNULL,      # the CLI must never block on a prompt
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise ZerionTimeoutError(
                f"the Zerion CLI did not finish within {timeout:.0f}s",
                capability=spec.capability.key,
            )
        except FileNotFoundError:
            cli_probe.refresh()
            raise ZerionUnavailableError("the Zerion CLI binary disappeared from PATH")
        except OSError as exc:
            raise ZerionUnavailableError(f"could not start the Zerion CLI: {sanitize(exc)}")

        latency_ms = int((time.perf_counter() - started) * 1000)
        stdout_doc = _parse_json_stream(proc.stdout)
        stderr_doc = _parse_json_stream(proc.stderr)

        if proc.returncode != 0:
            code = ""
            message = ""
            if isinstance(stderr_doc, dict):
                code = str(stderr_doc.get("code") or "").lower()
                message = str(stderr_doc.get("message") or stderr_doc.get("error") or "")
            failure = _ERROR_CODES.get(code, ZerionUnavailableError)
            raise failure(
                message or f"the Zerion CLI exited with code {proc.returncode}",
                capability=spec.capability.key,
                cli_code=code or None,
                exit_code=proc.returncode,
            )

        if stdout_doc is None:
            raise ZerionResponseError(
                "the Zerion CLI produced no JSON on stdout",
                capability=spec.capability.key,
            )

        return ZerionRawResult(
            source=self.source,
            payloads={spec.capability.key: stdout_doc},
            http_status=200,
            latency_ms=latency_ms,
            upstream_requests=spec.upstream_requests,
            payment_evidence=_payment_evidence(stdout_doc, stderr_doc),
            warnings=([sanitize(proc.stderr)] if proc.stderr and stderr_doc is None else []),
        )


cli_client = ZerionCliClient()
