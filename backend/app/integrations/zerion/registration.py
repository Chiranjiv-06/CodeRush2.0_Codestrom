"""Registering Zerion on the exchange.

Zerion is not a parallel marketplace: each capability becomes an ordinary
:class:`~app.models.Service` row under an ordinary
:class:`~app.models.Provider`, so the existing catalog, Bazaar advertisement,
ranking, quoting, x402 escrow, metering, integrity, receipt and cleanup paths
all apply to it with no special-casing. The only thing that differs is *where
the work happens*, which is what :mod:`app.integrations.registry` expresses.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...integrity import sha256_hex
from ...models import Provider, Role, Service, User
from ..registry import ExternalService, register_external_service
from .errors import ZerionValidationError
from .models import (
    CAPABILITIES,
    PROVIDER_CATEGORY,
    PROVIDER_DESCRIPTION,
    PROVIDER_HOMEPAGE,
    PROVIDER_ID,
    PROVIDER_NAME,
    SLUG_TO_CAPABILITY,
    ZerionCapability,
    validate_payload,
)

log = logging.getLogger("m2x.zerion.registration")

PROVIDER_EMAIL = "provider@zerion.integration"
PROVIDER_REPUTATION = 82.0

# Every Zerion service runs at Zerion, never in a worker container. The runtime
# is recorded as "http" (already one of the runtimes the Service model names) so
# nothing tries to hand this entrypoint to a sandbox.
RUNTIME = "http"


def capability_for_service(service: Service, payload: dict | None = None) -> str:
    """Which capability a job is for: the service slug, or an explicit override."""
    slug = str(getattr(service, "slug", "") or "")
    if slug in SLUG_TO_CAPABILITY:
        return SLUG_TO_CAPABILITY[slug].key
    requested = str((payload or {}).get("capability") or "").strip().lower()
    if requested in CAPABILITIES:
        return requested
    raise ZerionValidationError(f"service {slug!r} is not a Zerion capability")


def _entrypoint(capability: ZerionCapability) -> str:
    """Human-readable description of the remote call, stored as the entrypoint.

    Services are content-addressed by ``source_hash``; for an external service
    the honest "source" is the contract with the provider, not executable code.
    """
    target = capability.api_path or "(fan-out: portfolio + positions + transactions + pnl)"
    return (
        f"zerion://{capability.key}\n"
        f"api: GET {settings.zerion_api_url}{target}\n"
        f"cli: zerion {capability.cli_command} <wallet> --json [--x402]\n"
        f"docs: {PROVIDER_HOMEPAGE}"
    )


def capability_catalog() -> list[dict[str, Any]]:
    """Discovery view of every Zerion capability (non-secret)."""
    return [capability.advertisement() for capability in CAPABILITIES.values()]


# --------------------------------------------------------------------------- #
# Marketplace registration
# --------------------------------------------------------------------------- #
def _provider_owner(db: Session) -> User:
    from ...security import hash_password

    owner = db.scalar(select(User).where(User.email == PROVIDER_EMAIL))
    if owner is not None:
        return owner
    owner = User(
        email=PROVIDER_EMAIL,
        display_name=PROVIDER_NAME,
        # A random, unusable password: this principal exists to own the provider
        # record and receive payouts, and is never signed into.
        password_hash=hash_password(secrets.token_urlsafe(32)),
        role=Role.provider,
        wallet_address=f"M2X{secrets.token_hex(20).upper()}",
        payment_secret=secrets.token_urlsafe(32),
    )
    db.add(owner)
    db.flush()
    return owner


def ensure_registered(db: Session) -> dict[str, Any]:
    """Create or refresh the Zerion provider and its services. Idempotent."""
    if not settings.zerion_enabled:
        return {"provider": PROVIDER_ID, "enabled": False, "services": 0}

    owner = _provider_owner(db)
    provider = db.scalar(select(Provider).where(Provider.slug == PROVIDER_ID))
    if provider is None:
        provider = Provider(
            owner_id=owner.id,
            slug=PROVIDER_ID,
            name=PROVIDER_NAME,
            description=PROVIDER_DESCRIPTION,
            endpoint_url=settings.zerion_api_url,
            payout_address=owner.wallet_address,
            regions=["global"],
            capabilities=sorted(CAPABILITIES),
            is_verified=True,
            reputation_score=PROVIDER_REPUTATION,
        )
        db.add(provider)
        db.flush()
    else:
        provider.name = PROVIDER_NAME
        provider.description = PROVIDER_DESCRIPTION
        provider.endpoint_url = settings.zerion_api_url
        provider.capabilities = sorted(CAPABILITIES)
        provider.is_active = True

    created = 0
    for capability in CAPABILITIES.values():
        service = db.scalar(
            select(Service).where(
                Service.provider_id == provider.id, Service.slug == capability.slug
            )
        )
        entrypoint = _entrypoint(capability)
        if service is None:
            service = Service(provider_id=provider.id, slug=capability.slug)
            db.add(service)
            created += 1
        service.name = capability.name
        service.description = capability.description
        service.category = PROVIDER_CATEGORY
        service.runtime = RUNTIME
        service.entrypoint = entrypoint
        service.source_hash = sha256_hex(entrypoint)
        service.input_schema = capability.input_schema
        service.output_schema = capability.output_schema
        service.tags = list(capability.tags)
        service.base_price_micros = capability.price_micros
        # Priced per call, not per CPU-second: an external data request costs the
        # same whether Zerion answers in 40ms or 4s.
        service.price_per_cpu_second_micros = 0
        service.price_per_mb_egress_micros = 0
        service.max_price_micros = capability.max_price_micros
        service.max_runtime_seconds = capability.timeout_seconds
        service.memory_mb = 64
        service.concurrency_limit = 8
        service.network_access = True
        service.is_active = True

    db.flush()
    log.info("zerion: %s capabilities registered (%s new)", len(CAPABILITIES), created)
    return {
        "provider": PROVIDER_ID,
        "provider_id": provider.id,
        "enabled": True,
        "services": len(CAPABILITIES),
        "created": created,
    }


# --------------------------------------------------------------------------- #
# Executor registration
# --------------------------------------------------------------------------- #
def _validator(capability: ZerionCapability):
    def validate(payload: dict) -> None:
        # Raises ZerionValidationError (a ValueError) before a payment record
        # exists, so a malformed wallet costs the consumer nothing.
        validate_payload(capability.key, payload)

    return validate


def _executor(db: Session, job: Any, service: Any):
    from .service import execute_zerion_job

    return execute_zerion_job(db, job, service)


def register_executors() -> int:
    """Wire every Zerion capability into the external-service registry."""
    for capability in CAPABILITIES.values():
        register_external_service(
            ExternalService(
                provider_slug=PROVIDER_ID,
                service_slug=capability.slug,
                capability=capability.key,
                executor=_executor,
                validate=_validator(capability),
                metadata=capability.advertisement,
                fields={"external_provider": PROVIDER_ID},
            )
        )
    return len(CAPABILITIES)


register_executors()
