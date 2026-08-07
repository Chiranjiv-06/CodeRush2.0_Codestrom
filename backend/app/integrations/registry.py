"""Provider-agnostic registry of externally-executed services.

A service registered here is a normal :class:`~app.models.Service` row — priced,
quoted, paid for and receipted exactly like a sandbox service — except that its
work happens at a third party instead of in a worker container. The job
lifecycle asks this registry two questions and nothing more:

* :func:`external_service_for` — should this job be executed by an integration?
* :meth:`ExternalService.validate` — is the payload usable, *before* a payment
  record is created?

Keeping both behind one small module is what lets provider-specific logic stay
out of :mod:`app.services.jobs`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from ..models import Provider, Service


class ExternalExecutor(Protocol):
    """Runs one job at a third party and reports it in sandbox-result shape."""

    def __call__(self, db: Session, job: Any, service: Service) -> Any: ...


@dataclass
class ExternalService:
    """One externally-executed capability offered on the exchange."""

    provider_slug: str
    service_slug: str
    capability: str
    executor: ExternalExecutor
    #: Raises ``ValueError`` when the payload cannot be served. Called before a
    #: quote is committed, so a bad request costs the consumer nothing.
    validate: Callable[[dict], None] | None = None
    #: Extra, non-secret advertisement data merged into Bazaar listings.
    metadata: Callable[[], dict[str, Any]] | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider_slug, self.service_slug)


_REGISTRY: dict[tuple[str, str], ExternalService] = {}


def register_external_service(entry: ExternalService) -> ExternalService:
    _REGISTRY[entry.key] = entry
    return entry


def registered_external_services() -> list[ExternalService]:
    return list(_REGISTRY.values())


def _key_for(db: Session, service: Service) -> tuple[str, str] | None:
    provider = db.get(Provider, service.provider_id)
    if provider is None:
        return None
    return (provider.slug, service.slug)


def external_service_for(db: Session, service: Service) -> ExternalService | None:
    key = _key_for(db, service)
    return _REGISTRY.get(key) if key else None


def is_external_service(db: Session, service: Service) -> bool:
    return external_service_for(db, service) is not None


def external_metadata_for(db: Session, service: Service) -> dict[str, Any]:
    """Advertisement block for a listing, or ``{}`` for an ordinary service."""
    entry = external_service_for(db, service)
    if entry is None or entry.metadata is None:
        return {}
    try:
        return entry.metadata()
    except Exception:  # pragma: no cover - a listing must never fail to publish
        return {}
