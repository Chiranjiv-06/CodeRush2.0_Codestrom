"""Bazaar discovery.

Federates two sources into one index using the discovery record shape emitted by
``@x402-avm/extensions`` (GoPlausible Bazaar):

    { "resource": "...", "lastUpdated": ..., "x402Version": 1,
      "accepts": [ PaymentRequirements, ... ], "metadata": {...} }

* ``local``       — services registered on this exchange, published in Bazaar shape.
* ``goplausible`` — remote Bazaar listings pulled over HTTP and cached.

Remote sync degrades gracefully: on network failure the last good snapshot keeps
serving and the response is flagged ``degraded`` rather than erroring.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..algorand import asset_descriptor
from ..algorand import asset_id as mandated_asset_id
from ..algorand import normalize_asset
from ..cache import cache_get, cache_set
from ..config import settings
from ..integrations.registry import external_metadata_for
from ..models import BazaarListing, Provider, Service
from ..observability import bazaar_discoveries

log = logging.getLogger("m2x.bazaar")

REMOTE_CACHE_KEY = "bazaar:remote:snapshot"
STATUS_CACHE_KEY = "bazaar:remote:status"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Local publication
# --------------------------------------------------------------------------- #
def _service_to_record(service: Service, provider: Provider,
                       external: dict[str, Any] | None = None) -> dict[str, Any]:
    price = service.max_price_micros
    asset = asset_descriptor()
    record = {
        "resource": f"/v1/services/{service.id}/invoke",
        "type": "http",
        "x402Version": settings.x402_version,
        "lastUpdated": int(utcnow().timestamp()),
        "accepts": [
            {
                "scheme": "exact",
                "network": settings.x402_network,
                "maxAmountRequired": str(price),
                "resource": f"/v1/services/{service.id}/invoke",
                "description": service.description or service.name,
                "mimeType": "application/json",
                "payTo": provider.payout_address or provider.owner_id,
                "maxTimeoutSeconds": service.max_runtime_seconds,
                "asset": settings.x402_asset,
                "extra": {
                    "blockchain": asset["blockchain"],
                    "assetId": asset["asset_id"],
                    "name": asset["unit_name"],
                    "display": asset["display"],
                    "decimals": asset["decimals"],
                },
            }
        ],
        "metadata": {
            "serviceId": service.id,
            "name": service.name,
            "category": service.category,
            "runtime": service.runtime,
            "tags": service.tags or [],
            # Advertised so a federated consumer can tell, without fetching the
            # resource, that this provider is payable in the mandated asset.
            "payment": asset,
            "provider": {"id": provider.id, "slug": provider.slug, "name": provider.name,
                         "reputation": provider.reputation_score,
                         "paymentAssetId": provider.payment_asset_id or asset["asset_id"]},
            "inputSchema": service.input_schema,
            "outputSchema": service.output_schema,
            "sourceHash": service.source_hash,
        },
    }
    # An externally-executed service advertises the provider behind it: which
    # rail *it* is paid on, which chains it covers, and the quota that applies.
    # Consumers still pay this exchange in the mandated asset either way, so the
    # x402 `accepts` block above is unchanged.
    if external:
        record["metadata"]["external"] = external
        record["metadata"]["executedBy"] = external.get("provider", "")
    return record


def publish_local_services(db: Session) -> int:
    """Mirror every active local service into the discovery index."""
    count = 0
    services = db.scalars(select(Service).where(Service.is_active.is_(True))).all()
    live_resources = set()
    for service in services:
        provider = db.get(Provider, service.provider_id)
        if provider is None or not provider.is_active:
            continue
        record = _service_to_record(service, provider, external_metadata_for(db, service))
        resource = record["accepts"][0]["resource"]
        live_resources.add(resource)
        listing = db.scalar(
            select(BazaarListing).where(
                BazaarListing.source == "local", BazaarListing.resource == resource
            )
        )
        if listing is None:
            listing = BazaarListing(source="local", resource=resource)
            db.add(listing)
        listing.name = service.name
        listing.description = service.description
        listing.network = settings.x402_network
        listing.asset = settings.x402_asset
        listing.asset_id = mandated_asset_id()
        listing.price_micros = service.max_price_micros
        listing.pay_to = record["accepts"][0]["payTo"]
        listing.accepts = record["accepts"]
        listing.service_id = service.id
        listing.tags = service.tags or []
        listing.reputation_score = provider.reputation_score
        listing.last_seen_at = utcnow()
        listing.raw = record
        service.bazaar_listed = True
        count += 1

    # de-list services that went inactive
    for stale in db.scalars(select(BazaarListing).where(BazaarListing.source == "local")).all():
        if stale.resource not in live_resources:
            db.delete(stale)
    db.flush()
    bazaar_discoveries.labels("local", "ok").inc()
    return count


# --------------------------------------------------------------------------- #
# Remote (GoPlausible Bazaar)
# --------------------------------------------------------------------------- #
class BazaarClient:
    """Thin HTTP client for the Bazaar list endpoint."""

    def __init__(self) -> None:
        self.base_url = settings.bazaar_base_url.rstrip("/")
        self.path = settings.bazaar_list_path

    def fetch(self) -> list[dict[str, Any]]:
        import httpx

        url = f"{self.base_url}{self.path}"
        params = {"network": settings.bazaar_network, "limit": 200}
        headers = {"Accept": "application/json", "User-Agent": f"m2x/{settings.version}"}
        with httpx.Client(timeout=settings.bazaar_timeout_seconds, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return normalize_payload(resp.json())


def normalize_payload(data: Any) -> list[dict[str, Any]]:
    """Accept the several shapes Bazaar deployments return."""
    if isinstance(data, dict):
        for key in ("items", "resources", "list", "data", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data] if data.get("accepts") else []
    if not isinstance(data, list):
        return []

    records: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        accepts = item.get("accepts") or item.get("paymentRequirements") or []
        if isinstance(accepts, dict):
            accepts = [accepts]
        if not accepts:
            continue
        resource = (
            item.get("resource")
            or accepts[0].get("resource")
            or item.get("url")
            or item.get("id")
        )
        if not resource:
            continue
        records.append(
            {
                "resource": str(resource),
                "x402Version": item.get("x402Version", settings.x402_version),
                "lastUpdated": item.get("lastUpdated") or item.get("updatedAt"),
                "accepts": accepts,
                "metadata": item.get("metadata") or item.get("meta") or {},
            }
        )
    return records


def _price_of(accepts: list[dict]) -> int:
    for a in accepts:
        try:
            return int(a.get("maxAmountRequired", 0))
        except (TypeError, ValueError):
            continue
    return 0


def _asset_of(accepts: list[dict]) -> int:
    """First readable ASA id across a listing's payment options, else 0.

    Prefers the mandated asset when a listing offers several, so a provider that
    accepts it among other assets is correctly indexed as payable here.
    """
    found = []
    for a in accepts:
        asset = normalize_asset(a.get("asset")) or normalize_asset(
            (a.get("extra") or {}).get("assetId")
        )
        if asset:
            found.append(asset)
    if mandated_asset_id() in found:
        return mandated_asset_id()
    return found[0] if found else 0


def sync_remote_listings(db: Session, force: bool = False) -> dict[str, Any]:
    if not settings.bazaar_enabled:
        return {"source": "goplausible", "enabled": False, "synced": 0, "degraded": False}

    cached = cache_get(REMOTE_CACHE_KEY)
    if cached and not force:
        return {"source": "goplausible", "synced": len(cached), "cached": True,
                "degraded": bool((cache_get(STATUS_CACHE_KEY) or {}).get("degraded"))}

    try:
        records = BazaarClient().fetch()
        degraded = False
        error = ""
    except Exception as exc:
        log.warning("bazaar sync failed: %s", exc)
        bazaar_discoveries.labels("goplausible", "error").inc()
        records = cached or []
        degraded = True
        error = str(exc)[:200]

    for record in records:
        accepts = record["accepts"]
        first = accepts[0]
        listing = db.scalar(
            select(BazaarListing).where(
                BazaarListing.source == "goplausible",
                BazaarListing.resource == record["resource"],
            )
        )
        if listing is None:
            listing = BazaarListing(source="goplausible", resource=record["resource"])
            db.add(listing)
        meta = record.get("metadata") or {}
        listing.name = str(meta.get("name") or first.get("description") or record["resource"])[:200]
        listing.description = str(first.get("description") or meta.get("description") or "")
        listing.network = str(first.get("network") or settings.bazaar_network)
        listing.asset = str(first.get("asset") or "")
        # A federated listing keeps whatever asset it advertises; 0 when it is
        # not an ASA id we can read. Only the mandated id is payable here, and
        # search filters on that rather than silently coercing it.
        listing.asset_id = _asset_of(accepts)
        listing.price_micros = _price_of(accepts)
        listing.pay_to = str(first.get("payTo") or "")
        listing.accepts = accepts
        listing.tags = list(meta.get("tags") or [])
        listing.reputation_score = float(meta.get("reputation") or 50.0)
        listing.last_seen_at = utcnow()
        listing.raw = record
    db.flush()

    if not degraded:
        cache_set(REMOTE_CACHE_KEY, records, ttl=settings.bazaar_cache_ttl_seconds)
        bazaar_discoveries.labels("goplausible", "ok").inc()
    cache_set(STATUS_CACHE_KEY, {"degraded": degraded, "error": error, "at": utcnow().isoformat()},
              ttl=settings.bazaar_cache_ttl_seconds)

    return {
        "source": "goplausible",
        "synced": len(records),
        "cached": False,
        "degraded": degraded,
        "error": error,
        "endpoint": f"{settings.bazaar_base_url}{settings.bazaar_list_path}",
    }


# --------------------------------------------------------------------------- #
# Unified search
# --------------------------------------------------------------------------- #
def search_listings(
    db: Session,
    *,
    query: str = "",
    source: str | None = None,
    network: str | None = None,
    max_price_micros: int | None = None,
    min_reputation: float | None = None,
    tags: list[str] | None = None,
    asset_id: int | None = None,
    payable_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    stmt = select(BazaarListing)
    if source:
        stmt = stmt.where(BazaarListing.source == source)
    if network:
        stmt = stmt.where(BazaarListing.network == network)
    if payable_only and asset_id is None:
        asset_id = mandated_asset_id()
    if asset_id is not None:
        stmt = stmt.where(BazaarListing.asset_id == asset_id)
    if max_price_micros is not None:
        stmt = stmt.where(BazaarListing.price_micros <= max_price_micros)
    if min_reputation is not None:
        stmt = stmt.where(BazaarListing.reputation_score >= min_reputation)

    rows = db.scalars(stmt.order_by(BazaarListing.reputation_score.desc()).limit(500)).all()
    # Multi-word queries are matched per token, not as one literal substring:
    # "hash sha256 digest" must still find a listing tagged only "sha256".
    terms = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) > 1]
    wanted_tags = {t.lower() for t in (tags or [])}

    scored: list[tuple[int, dict]] = []
    for row in rows:
        haystack = " ".join(
            [row.name or "", row.description or "", row.resource or "", " ".join(row.tags or [])]
        ).lower()
        hits = sum(1 for term in terms if term in haystack)
        if terms and hits == 0:
            continue
        if wanted_tags and not wanted_tags & {t.lower() for t in (row.tags or [])}:
            continue
        scored.append((hits, listing_to_dict(row)))

    scored.sort(key=lambda pair: (-pair[0], -pair[1]["reputation_score"]))
    return [item for _hits, item in scored[:limit]]


def listing_to_dict(row: BazaarListing) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "resource": row.resource,
        "name": row.name,
        "description": row.description,
        "network": row.network,
        "asset": row.asset,
        "asset_id": row.asset_id,
        # False for a federated listing quoting some other asset: discoverable,
        # but not something this exchange can pay for.
        "payable": row.asset_id == mandated_asset_id(),
        "payment_asset": asset_descriptor() if row.asset_id == mandated_asset_id() else None,
        "price_micros": row.price_micros,
        "pay_to": row.pay_to,
        "accepts": row.accepts,
        "service_id": row.service_id,
        "tags": row.tags,
        "reputation_score": row.reputation_score,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        # Present only for services executed at a third party: the provider
        # behind the listing, its own payment rail, chains, quota and schemas.
        "external": ((row.raw or {}).get("metadata") or {}).get("external") or None,
    }


class Discovery:
    """Facade used by the agent, MCP tools and the REST API."""

    def refresh(self, db: Session, force: bool = False) -> dict[str, Any]:
        local = publish_local_services(db)
        remote = sync_remote_listings(db, force=force)
        return {"local_published": local, "remote": remote}

    def search(self, db: Session, **kwargs) -> list[dict[str, Any]]:
        return search_listings(db, **kwargs)

    def best_for(
        self, db: Session, capability: str, *, budget_micros: int, min_reputation: float = 0.0
    ) -> dict[str, Any] | None:
        """Rank candidates by reputation-per-price and return the winner.

        Only listings payable in the mandated asset are considered — a cheaper
        provider quoting another ASA is not a candidate, because this exchange
        could not settle with it.
        """
        candidates = search_listings(
            db, query=capability, source="local", max_price_micros=budget_micros,
            min_reputation=min_reputation, payable_only=True, limit=25,
        )
        if not candidates:
            candidates = search_listings(db, query=capability, max_price_micros=budget_micros,
                                         min_reputation=min_reputation, payable_only=True,
                                         limit=25)
        if not candidates:
            return None
        def score(c: dict) -> float:
            price = max(c["price_micros"], 1)
            return (c["reputation_score"] + 1) / (price ** 0.35)
        return max(candidates, key=score)

    def status(self, db: Session) -> dict[str, Any]:
        local = db.scalar(
            select(BazaarListing).where(BazaarListing.source == "local").limit(1)
        )
        counts = {}
        for src in ("local", "goplausible"):
            counts[src] = len(
                db.scalars(select(BazaarListing.id).where(BazaarListing.source == src)).all()
            )
        remote_status = cache_get(STATUS_CACHE_KEY) or {}
        payable = len(
            db.scalars(
                select(BazaarListing.id).where(BazaarListing.asset_id == mandated_asset_id())
            ).all()
        )
        return {
            "enabled": settings.bazaar_enabled,
            "endpoint": f"{settings.bazaar_base_url}{settings.bazaar_list_path}",
            "network": settings.bazaar_network,
            "asset": asset_descriptor(),
            "counts": counts,
            "payable_listings": payable,
            "has_local_index": local is not None,
            "remote": remote_status,
            "extension": "@x402-avm/extensions",
        }


discovery = Discovery()
