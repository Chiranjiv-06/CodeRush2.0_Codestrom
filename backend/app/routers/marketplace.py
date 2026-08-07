"""Provider marketplace: providers and their priced services."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..algorand import asset_descriptor
from ..algorand import asset_id as mandated_asset_id
from ..bazaar.discovery import publish_local_services
from ..db import get_db
from ..integrity import sha256_hex
from ..models import AuditLog, Job, Provider, Role, Service
from ..schemas import (
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
)
from ..security import CurrentUser, get_optional_user
from ..services.reputation import provider_stats

router = APIRouter(prefix="/v1", tags=["marketplace"])


def _owned_provider(db: Session, provider_id: str, user) -> Provider:
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    if provider.owner_id != user.id and user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your provider")
    return provider


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
@router.get("/providers", response_model=list[ProviderOut])
def list_providers(
    db: Session = Depends(get_db),
    q: str = "",
    active_only: bool = True,
    min_reputation: float = 0.0,
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> list[ProviderOut]:
    stmt = select(Provider).where(Provider.reputation_score >= min_reputation)
    if active_only:
        stmt = stmt.where(Provider.is_active.is_(True))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Provider.name).like(like) | func.lower(Provider.slug).like(like)
        )
    rows = db.scalars(
        stmt.order_by(Provider.reputation_score.desc()).offset(offset).limit(limit)
    ).all()
    return [ProviderOut.model_validate(r) for r in rows]


@router.post("/providers", response_model=ProviderOut, status_code=201)
def create_provider(body: ProviderCreate, user: CurrentUser,
                    db: Session = Depends(get_db)) -> ProviderOut:
    if db.scalar(select(Provider).where(Provider.slug == body.slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, "provider slug already taken")
    provider = Provider(
        owner_id=user.id,
        slug=body.slug,
        name=body.name,
        description=body.description,
        endpoint_url=body.endpoint_url,
        payout_address=body.payout_address or user.wallet_address,
        # Registration is on the exchange's terms: providers are paid in the
        # mandated asset. ProviderCreate rejects any other id outright.
        payment_asset_id=body.payment_asset_id or mandated_asset_id(),
        regions=body.regions,
        capabilities=body.capabilities,
    )
    db.add(provider)
    if user.role == Role.consumer:
        user.role = Role.provider
    db.add(AuditLog(actor_id=user.id, action="provider.create", target=body.slug,
                    data={"payment_asset_id": provider.payment_asset_id}))
    db.flush()
    return ProviderOut.model_validate(provider)


@router.get("/providers/{provider_id}", response_model=ProviderOut)
def get_provider(provider_id: str, db: Session = Depends(get_db)) -> ProviderOut:
    provider = db.get(Provider, provider_id) or db.scalar(
        select(Provider).where(Provider.slug == provider_id)
    )
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    return ProviderOut.model_validate(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: str, body: ProviderUpdate, user: CurrentUser,
                    db: Session = Depends(get_db)) -> ProviderOut:
    provider = _owned_provider(db, provider_id, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(provider, field, value)
    db.flush()
    publish_local_services(db)
    return ProviderOut.model_validate(provider)


@router.get("/providers/{provider_id}/stats")
def provider_statistics(provider_id: str, db: Session = Depends(get_db)) -> dict:
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    stats = provider_stats(db, provider)
    stats["revenue_micros"] = int(
        db.scalar(
            select(func.coalesce(func.sum(Job.final_price_micros), 0)).where(
                Job.provider_id == provider.id
            )
        )
        or 0
    )
    stats["services"] = db.scalar(
        select(func.count(Service.id)).where(Service.provider_id == provider.id)
    )
    stats["payment_asset"] = asset_descriptor()
    return stats


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
@router.get("/services", response_model=list[ServiceOut])
def list_services(
    db: Session = Depends(get_db),
    q: str = "",
    category: str = "",
    runtime: str = "",
    provider_id: str = "",
    max_price_micros: int | None = None,
    active_only: bool = True,
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> list[ServiceOut]:
    stmt = select(Service)
    if active_only:
        stmt = stmt.where(Service.is_active.is_(True))
    if category:
        stmt = stmt.where(Service.category == category)
    if runtime:
        stmt = stmt.where(Service.runtime == runtime)
    if provider_id:
        stmt = stmt.where(Service.provider_id == provider_id)
    if max_price_micros is not None:
        stmt = stmt.where(Service.max_price_micros <= max_price_micros)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Service.name).like(like)
            | func.lower(Service.description).like(like)
            | func.lower(Service.slug).like(like)
        )
    rows = db.scalars(stmt.order_by(Service.created_at.desc()).offset(offset).limit(limit)).all()
    return [ServiceOut.model_validate(r) for r in rows]


@router.post("/providers/{provider_id}/services", response_model=ServiceOut, status_code=201)
def create_service(provider_id: str, body: ServiceCreate, user: CurrentUser,
                   db: Session = Depends(get_db)) -> ServiceOut:
    provider = _owned_provider(db, provider_id, user)
    if db.scalar(select(Service).where(Service.provider_id == provider.id,
                                       Service.slug == body.slug)):
        raise HTTPException(status.HTTP_409_CONFLICT, "service slug already used by this provider")
    service = Service(
        provider_id=provider.id,
        source_hash=sha256_hex(body.entrypoint),
        **body.model_dump(),
    )
    db.add(service)
    db.flush()
    publish_local_services(db)
    db.add(AuditLog(actor_id=user.id, action="service.create", target=service.id,
                    data={"slug": service.slug, "source_hash": service.source_hash}))
    return ServiceOut.model_validate(service)


@router.get("/services/{service_id}", response_model=ServiceOut)
def get_service(service_id: str, db: Session = Depends(get_db)) -> ServiceOut:
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    return ServiceOut.model_validate(service)


@router.patch("/services/{service_id}", response_model=ServiceOut)
def update_service(service_id: str, body: ServiceUpdate, user: CurrentUser,
                   db: Session = Depends(get_db)) -> ServiceOut:
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    _owned_provider(db, service.provider_id, user)
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(service, field, value)
    if "entrypoint" in updates:
        service.source_hash = sha256_hex(service.entrypoint)
    db.flush()
    publish_local_services(db)
    return ServiceOut.model_validate(service)


@router.delete("/services/{service_id}", status_code=204)
def deactivate_service(service_id: str, user: CurrentUser, db: Session = Depends(get_db)) -> None:
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    _owned_provider(db, service.provider_id, user)
    service.is_active = False
    db.flush()
    publish_local_services(db)


@router.get("/services/{service_id}/source")
def service_source(service_id: str, db: Session = Depends(get_db),
                   user=Depends(get_optional_user)) -> dict:
    """Source + its SHA-256, so consumers can pin exactly what they paid to run."""
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "service not found")
    return {
        "service_id": service.id,
        "runtime": service.runtime,
        "source_hash": service.source_hash,
        "recomputed_hash": sha256_hex(service.entrypoint),
        "source": service.entrypoint,
        "matches": sha256_hex(service.entrypoint) == service.source_hash,
    }
