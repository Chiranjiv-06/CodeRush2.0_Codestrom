"""Bazaar discovery API (local index + GoPlausible federation)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..algorand import asset_descriptor
from ..bazaar.discovery import discovery
from ..db import get_db
from ..security import CurrentUser

router = APIRouter(prefix="/v1/bazaar", tags=["discovery"])


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict:
    return discovery.status(db)


@router.post("/refresh")
def refresh(user: CurrentUser, db: Session = Depends(get_db), force: bool = False) -> dict:
    result = discovery.refresh(db, force=force)
    db.commit()
    return result


@router.get("/listings")
def listings(
    db: Session = Depends(get_db),
    q: str = "",
    source: str = "",
    network: str = "",
    max_price_micros: int | None = None,
    min_reputation: float | None = None,
    tags: str = "",
    asset_id: int | None = None,
    payable_only: bool = False,
    limit: int = Query(50, le=200),
) -> dict:
    items = discovery.search(
        db,
        query=q,
        source=source or None,
        network=network or None,
        max_price_micros=max_price_micros,
        min_reputation=min_reputation,
        tags=[t for t in tags.split(",") if t.strip()],
        asset_id=asset_id,
        payable_only=payable_only,
        limit=limit,
    )
    return {
        "count": len(items),
        "items": items,
        "payment_asset": asset_descriptor(),
        "index": discovery.status(db)["counts"],
    }


@router.get("/best")
def best(capability: str, budget_micros: int = 1_000_000, min_reputation: float = 0.0,
         db: Session = Depends(get_db)) -> dict:
    match = discovery.best_for(db, capability, budget_micros=budget_micros,
                               min_reputation=min_reputation)
    return {"capability": capability, "budget_micros": budget_micros, "match": match}
