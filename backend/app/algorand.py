"""Algorand payment asset policy.

Every price, authorization, escrow, settlement, refund and receipt on this
exchange is denominated in one Algorand Standard Asset — by default ASA
#10458941 on TestNet. This module is the only place that decides what "the
payment asset" means; the x402 protocol layer, the facilitator, the job
lifecycle, Bazaar discovery and the marketplace API all ask here rather than
carrying their own copy of the id.

Two rules hold everywhere:

* an asset that is not the configured ASA is never accepted — it is rejected
  before any money moves and before any compute runs;
* an *absent* asset is not the same as a matching one, so payloads that simply
  omit the field are refused rather than defaulted.
"""
from __future__ import annotations

from typing import Any

from .config import ASSET_ID as MANDATED_ASSET_ID
from .config import settings


class AssetPolicyError(ValueError):
    """Payment material that names the wrong asset, or no asset at all."""


def asset_id() -> int:
    """The ASA id every payment on this exchange must use."""
    return settings.algorand_asset_id


def asset_descriptor() -> dict[str, Any]:
    """The payment-asset block embedded in configs, listings and receipts."""
    return {
        "blockchain": settings.blockchain,
        "network": settings.network_label,
        "x402_network": settings.x402_network,
        "asset_id": asset_id(),
        "asset": settings.x402_asset,
        "unit_name": settings.algorand_asset_unit_name,
        "decimals": settings.x402_asset_decimals,
        "display": settings.asset_display,
        "label": "Payment Asset",
    }


def normalize_asset(value: Any) -> int | None:
    """Coerce an asset field to an ASA id, or ``None`` when it is not one.

    Accepts the shapes x402 payloads use in the wild: an integer id, the id as a
    string, ``"asa:10458941"`` / ``"algorand:10458941"`` prefixes, and a nested
    object carrying ``assetId``/``asset_id``/``id``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        for key in ("assetId", "asset_id", "asset", "id"):
            if key in value:
                nested = normalize_asset(value[key])
                if nested is not None:
                    return nested
        return None
    if isinstance(value, str):
        token = value.strip().lower()
        if not token:
            return None
        for prefix in ("asa:", "asa#", "asa-", "algorand:", "algo:", "#"):
            if token.startswith(prefix):
                token = token[len(prefix):]
                break
        token = token.strip()
        return int(token) if token.isdigit() else None
    return None


def is_mandated_asset(value: Any) -> bool:
    return normalize_asset(value) == asset_id()


def assert_asset(value: Any, *, context: str = "payment") -> int:
    """Return the asset id, or raise if it is missing or not the mandated ASA."""
    found = normalize_asset(value)
    if found is None:
        raise AssetPolicyError(
            f"{context}: no Algorand asset id supplied; "
            f"{settings.asset_display} is required"
        )
    if found != asset_id():
        raise AssetPolicyError(
            f"{context}: asset {found} is not accepted; "
            f"this exchange settles only in {settings.asset_display}"
        )
    return found


def normalize_network(value: Any) -> str:
    return str(value or "").strip().lower()


def is_mandated_network(value: Any) -> bool:
    return normalize_network(value) == settings.x402_network


def assert_network(value: Any, *, context: str = "payment") -> str:
    network = normalize_network(value)
    if not network:
        raise AssetPolicyError(f"{context}: no network supplied; expected {settings.x402_network}")
    if network != settings.x402_network:
        raise AssetPolicyError(
            f"{context}: network {network!r} is not accepted; expected {settings.x402_network}"
        )
    return network


def check(label: str, ok: bool, detail: str = "") -> dict[str, Any]:
    """One row of a validation report."""
    return {"check": label, "ok": bool(ok), "detail": detail}


def configuration_report() -> dict[str, Any]:
    """Startup self-check: is this deployment configured for the mandated asset?"""
    configured = asset_id()
    checks = [
        check("blockchain", settings.blockchain == "Algorand", settings.blockchain),
        check("network", settings.algorand_network in ("testnet", "mainnet", "betanet", "localnet"),
              settings.x402_network),
        check("asset_id", configured == MANDATED_ASSET_ID,
              f"configured {configured}, mandated {MANDATED_ASSET_ID}"),
        check("decimals", settings.x402_asset_decimals > 0, str(settings.x402_asset_decimals)),
    ]
    return {
        "asset": asset_descriptor(),
        "mandated_asset_id": MANDATED_ASSET_ID,
        "overridden_by_administrator": configured != MANDATED_ASSET_ID,
        "checks": checks,
        "ok": all(c["ok"] for c in checks if c["check"] != "asset_id"),
    }
