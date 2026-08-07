"""Zerion-side payment: rail selection and the x402 adapter.

This module owns the *second* leg of a Zerion job. The first leg — consumer pays
the exchange in the mandated Algorand ASA over x402 — is untouched and lives in
:mod:`app.x402`. This leg is the exchange paying Zerion on Zerion's own rail.

Zerion documents three ways in: an API key (``zk_...``, HTTP Basic), x402
pay-per-request in USDC on Base or Solana with no API key, and MPP on Tempo.
The exchange supports the first two.

x402 settlement is performed by the Zerion CLI (``zerion <command> --x402``),
which is the integration path Zerion documents as handling signing and the 402
retry for you. That keeps private keys inside a process that already knows how
to use them, and out of this codebase's request path entirely: nothing here ever
reads, copies, returns or logs a key — it only reports *whether* one is present.
"""
from __future__ import annotations

import logging
import shutil
from typing import Any

from ...config import settings
from ...payments.rails import (
    ExternalProviderPaymentAdapter,
    PaymentOutcome,
    PaymentRail,
)
from .errors import ZerionPaymentError, sanitize
from .models import PROVIDER_ID

log = logging.getLogger("m2x.zerion.payment")


# --------------------------------------------------------------------------- #
# Rail & transport resolution
# --------------------------------------------------------------------------- #
def active_rail() -> PaymentRail:
    """Which rail this deployment can actually pay Zerion on, right now.

    x402 wins when it is switched on *and* a signing key is present; a key
    without the switch, or the switch without a key, is not a usable rail and is
    reported as such rather than silently falling through to something else.
    """
    if not settings.zerion_enabled:
        return PaymentRail.NONE
    if settings.zerion_use_x402 and settings.zerion_x402_configured:
        return PaymentRail.ZERION_X402
    if settings.zerion_api_key_configured:
        return PaymentRail.API_KEY
    return PaymentRail.NONE


class _CliProbe:
    """Caches whether the Zerion CLI is on PATH."""

    def __init__(self) -> None:
        self._path: str | None = None
        self._checked = False

    def path(self) -> str | None:
        if not self._checked:
            self._checked = True
            command = settings.zerion_cli_command.strip()
            # Only a bare command name is ever resolved: a path from
            # configuration is fine, but nothing user-supplied reaches here.
            self._path = shutil.which(command) if command else None
        return self._path

    def refresh(self) -> str | None:
        self._checked = False
        return self.path()


cli_probe = _CliProbe()


def cli_available() -> bool:
    return cli_probe.path() is not None


def transport_name() -> str:
    """``cli`` | ``api`` | ``demo`` | ``unavailable``."""
    if not settings.zerion_enabled:
        return "unavailable"
    rail = active_rail()
    preference = (settings.zerion_transport or "auto").strip().lower()

    if rail is PaymentRail.NONE:
        return "demo" if settings.zerion_demo_mode else "unavailable"

    if preference == "cli":
        if cli_available():
            return "cli"
        return "demo" if settings.zerion_demo_mode else "unavailable"

    if preference == "api":
        # The HTTP API can be reached with an API key. x402 over plain HTTP
        # needs a signer this process does not have, so it is not offered.
        if rail is PaymentRail.API_KEY:
            return "api"
        return "cli" if cli_available() else (
            "demo" if settings.zerion_demo_mode else "unavailable"
        )

    # auto
    if rail is PaymentRail.ZERION_X402:
        if cli_available():
            return "cli"
        return "demo" if settings.zerion_demo_mode else "unavailable"
    if cli_available() and preference == "cli":
        return "cli"
    return "api"


def mode_report() -> dict[str, Any]:
    """Non-secret status block for /v1/config, the router and the dashboard."""
    rail = active_rail()
    transport = transport_name()
    return {
        "provider": PROVIDER_ID,
        "enabled": settings.zerion_enabled,
        "rail": rail.value,
        "transport": transport,
        "mode": "demo" if transport == "demo" else f"{rail.value}:{transport}",
        "operational": transport in ("cli", "api", "demo"),
        "live": transport in ("cli", "api"),
        # Presence only. The values themselves are never exposed anywhere.
        "api_key_configured": settings.zerion_api_key_configured,
        "x402_enabled": settings.zerion_use_x402,
        "x402_keys_configured": settings.zerion_x402_configured,
        "x402_evm_key_configured": bool(settings.zerion_evm_private_key),
        "x402_solana_key_configured": bool(settings.zerion_solana_private_key),
        "x402_network": settings.zerion_x402_chain or None,
        "cli_available": cli_available(),
        "cli_command": settings.zerion_cli_command,
        "api_base_url": settings.zerion_api_url,
        "demo_mode": settings.zerion_demo_mode,
        "timeout_seconds": settings.zerion_timeout_seconds,
        "reason": _unavailable_reason(rail, transport),
    }


def _unavailable_reason(rail: PaymentRail, transport: str) -> str:
    if not settings.zerion_enabled:
        return "ZERION_ENABLED is false"
    if transport == "demo":
        return (
            "no Zerion credential configured — serving labelled demo fixtures. "
            "Set ZERION_API_KEY, or ZERION_USE_X402=true with ZERION_EVM_PRIVATE_KEY / "
            "ZERION_SOLANA_PRIVATE_KEY plus the Zerion CLI, for live data."
        )
    if transport == "unavailable":
        if rail is PaymentRail.ZERION_X402:
            return (
                "x402 is enabled but the Zerion CLI is not on PATH. Install it with "
                "`npm install -g zerion-cli` (the CLI performs x402 signing and the 402 retry)."
            )
        return "no usable Zerion credential and ZERION_DEMO_MODE is false"
    return ""


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class ZerionX402PaymentAdapter(ExternalProviderPaymentAdapter):
    """Pays Zerion for one request on whichever rail is configured.

    Payment is authorized *before* the request leaves the process
    (:meth:`preauthorize`) and described *after* the transport answers
    (:meth:`finalize`), because on the x402 rail the settlement happens inside
    the CLI's own 402 handshake — the honest thing to report is what the
    transport actually did, not what we hoped it would do.
    """

    provider_id = PROVIDER_ID

    @property
    def rail(self) -> PaymentRail:
        return active_rail()

    def available(self) -> bool:
        return transport_name() in ("cli", "api", "demo")

    def quote_micros(self) -> int:
        """Cost of one upstream Zerion request, in micro-units of USDC."""
        return settings.zerion_cost_micros if self.rail is PaymentRail.ZERION_X402 else 0

    # -- authorization ------------------------------------------------------ #
    def preauthorize(self, *, capability: str, upstream_requests: int = 1) -> dict[str, Any]:
        """Refuse now if this request could not be paid for.

        Raises :class:`ZerionPaymentError` rather than returning, because a
        request we cannot pay for must not be attempted at all.
        """
        transport = transport_name()
        if transport == "unavailable":
            raise ZerionPaymentError(
                _unavailable_reason(self.rail, transport) or "no Zerion payment rail available",
                capability=capability,
                rail=self.rail.value,
            )
        return {
            "rail": self.rail.value,
            "transport": transport,
            "quoted_micros": self.quote_micros() * max(upstream_requests, 1),
            "currency": "USDC",
            "network": settings.zerion_x402_chain or None,
        }

    # -- settlement description --------------------------------------------- #
    def finalize(
        self,
        *,
        request_id: str,
        capability: str,
        transport: str,
        upstream_requests: int = 1,
        succeeded: bool = True,
        evidence: dict[str, Any] | None = None,
    ) -> PaymentOutcome:
        evidence = evidence or {}
        rail = self.rail
        units = max(upstream_requests, 1)

        if transport == "demo":
            return PaymentOutcome(
                rail=PaymentRail.NONE,
                status="simulated",
                amount="0",
                currency="USDC",
                payment_id=request_id,
                settled=False,
                detail="demo mode — no external request was made and no payment settled",
                extra={"demo": True, "capability": capability},
            )

        if rail is PaymentRail.API_KEY:
            outcome = PaymentOutcome.not_required(
                "Zerion reached with an API key (HTTP Basic); billing is by subscription, "
                "not per request"
            )
            outcome.payment_id = request_id
            outcome.extra = {"capability": capability, "transport": transport}
            return outcome

        if rail is PaymentRail.ZERION_X402:
            if not succeeded:
                return PaymentOutcome(
                    rail=rail, status="failed", amount="0", currency="USDC",
                    network=settings.zerion_x402_chain, payment_id=request_id, settled=False,
                    detail=sanitize(evidence.get("error") or "the x402 request did not complete"),
                    extra={"capability": capability, "transport": transport},
                )
            # The Zerion CLI only returns data once its 402 handshake settled, so
            # a clean exit with --x402 is the settlement evidence. Any transaction
            # id the CLI surfaces is carried through; we never fabricate one.
            amount_micros = settings.zerion_cost_micros * units
            return PaymentOutcome(
                rail=rail,
                status="settled",
                amount=f"{amount_micros / 1_000_000:.6f}",
                currency="USDC",
                network=str(evidence.get("network") or settings.zerion_x402_chain or ""),
                transaction=sanitize(evidence.get("transaction") or ""),
                payment_id=request_id,
                settled=True,
                detail="settled by the Zerion CLI x402 handshake",
                extra={
                    "capability": capability,
                    "transport": transport,
                    "upstream_requests": units,
                    "amount_micros": amount_micros,
                    "evidence": "cli_exit_ok" if not evidence.get("transaction") else "cli_reported_tx",
                },
            )

        return PaymentOutcome(
            rail=PaymentRail.NONE, status="skipped", settled=False,
            payment_id=request_id,
            detail="no Zerion payment rail is configured",
        )

    # -- ABC entry point ---------------------------------------------------- #
    def pay(self, *, request_id: str, capability: str, **context: Any) -> PaymentOutcome:
        """Authorize and describe a payment in one step.

        Used by callers that are not driving a transport themselves (and by
        tests). Never raises for a declined payment.
        """
        upstream = int(context.get("upstream_requests", 1) or 1)
        try:
            self.preauthorize(capability=capability, upstream_requests=upstream)
        except ZerionPaymentError as exc:
            return PaymentOutcome.failed(self.rail, exc.detail)
        return self.finalize(
            request_id=request_id,
            capability=capability,
            transport=str(context.get("transport") or transport_name()),
            upstream_requests=upstream,
            succeeded=bool(context.get("succeeded", True)),
            evidence=context.get("evidence"),
        )

    def status(self) -> dict[str, Any]:
        return {**super().status(), **mode_report()}


adapter = ZerionX402PaymentAdapter()
