"""AlgoKit integration router — live Algorand network, wallet and ASA queries.

Uses ``algokit-utils`` when available; falls back gracefully to the algod URL
configured in settings, or returns well-formed stub data so the dashboard
stays useful even on a bare machine with no algod configured.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..config import settings

log = logging.getLogger("m2x.algokit")

router = APIRouter(prefix="/v1/algokit", tags=["algokit"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _get_algorand_client():
    """Return an AlgorandClient (algokit-utils ≥ 2) or None."""
    try:
        from algokit_utils.algorand import AlgorandClient  # type: ignore

        network = settings.algorand_network
        if settings.algod_url:
            from algokit_utils import AlgoClientConfig  # type: ignore

            cfg = AlgoClientConfig(server=settings.algod_url, token=settings.algod_token)
            return AlgorandClient(algod_config=cfg)
        if network == "mainnet":
            return AlgorandClient.mainnet()
        if network in ("localnet", "sandbox"):
            return AlgorandClient.default_localnet()
        return AlgorandClient.testnet()
    except Exception as exc:  # pragma: no cover
        log.debug("algokit-utils not available: %s", exc)
        return None


def _algod_client():
    """Return a raw algosdk algod client or None."""
    try:
        from algosdk.v2client import algod  # type: ignore

        url = settings.algod_url or "https://testnet-api.algonode.cloud"
        token = settings.algod_token or ""
        return algod.AlgodClient(token, url, headers={"User-Agent": "m2x-exchange/1.0"})
    except Exception as exc:
        log.debug("algosdk not available: %s", exc)
        return None


def _stub_network_status() -> dict[str, Any]:
    return {
        "source": "stub",
        "network": settings.algorand_network,
        "note": "algod not configured — run `algokit localnet start` or set M2X_ALGOD_URL",
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.get("/status")
def algokit_status() -> dict:
    """Live Algorand node status (last round, catchup, etc.)."""
    client = _get_algorand_client()
    if client:
        try:
            raw = client.client.algod.status()
            return {
                "source": "algokit-utils",
                "network": settings.algorand_network,
                "last_round": raw.get("last-round"),
                "time_since_last_round_ms": raw.get("time-since-last-round"),
                "catchup_time": raw.get("catchup-time", 0),
                "stopped_at_unsupported_round": raw.get("stopped-at-unsupported-round", False),
                "raw": raw,
            }
        except Exception as exc:
            log.warning("algod status failed: %s", exc)
            return {**_stub_network_status(), "error": str(exc)}

    algod = _algod_client()
    if algod:
        try:
            raw = algod.status()
            return {
                "source": "algosdk",
                "network": settings.algorand_network,
                "last_round": raw.get("last-round"),
                "time_since_last_round_ms": raw.get("time-since-last-round"),
                "raw": raw,
            }
        except Exception as exc:
            return {**_stub_network_status(), "error": str(exc)}

    return _stub_network_status()


@router.get("/asset/{asset_id}")
def asset_info(asset_id: int) -> dict:
    """On-chain ASA metadata for a given asset id."""
    client = _get_algorand_client()
    try:
        if client:
            raw = client.client.algod.asset_info(asset_id)
        else:
            algod = _algod_client()
            if not algod:
                raise RuntimeError("algod not configured")
            raw = algod.asset_info(asset_id)
        params = raw.get("params", {})
        return {
            "asset_id": asset_id,
            "name": params.get("name", ""),
            "unit_name": params.get("unit-name", ""),
            "total": params.get("total"),
            "decimals": params.get("decimals"),
            "creator": params.get("creator", ""),
            "manager": params.get("manager", ""),
            "freeze": params.get("freeze", ""),
            "clawback": params.get("clawback", ""),
            "default_frozen": params.get("default-frozen", False),
            "url": params.get("url", ""),
            "is_mandated_asset": asset_id == settings.algorand_asset_id,
            "raw": raw,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"algod error: {exc}")


@router.get("/wallet/{address}")
def wallet_info(
    address: str,
    asset_id: int | None = Query(default=None, description="Filter to a specific ASA balance"),
) -> dict:
    """Balance, opted-in assets and mini-tx info for any Algorand address."""
    client = _get_algorand_client()
    try:
        if client:
            info = client.client.algod.account_info(address)
        else:
            algod = _algod_client()
            if not algod:
                raise HTTPException(status_code=503, detail="algod not configured")
            info = algod.account_info(address)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"algod error: {exc}")

    algo_balance = info.get("amount", 0)
    min_balance = info.get("min-balance", 0)
    assets_raw = info.get("assets", [])

    # Payment asset balance
    mandated = settings.algorand_asset_id
    payment_asset_balance = next(
        (a["amount"] for a in assets_raw if a["asset-id"] == mandated), None
    )

    # Optionally filter to requested ASA
    if asset_id is not None:
        assets_raw = [a for a in assets_raw if a["asset-id"] == asset_id]

    return {
        "address": address,
        "network": settings.algorand_network,
        "algo_balance_micro": algo_balance,
        "algo_balance": algo_balance / 1_000_000,
        "min_balance_micro": min_balance,
        "payment_asset_id": mandated,
        "payment_asset_balance_micro": payment_asset_balance,
        "payment_asset_balance": (payment_asset_balance / 10 ** settings.x402_asset_decimals
                                   if payment_asset_balance is not None else None),
        "opted_in_assets": len(info.get("assets", [])),
        "assets": [
            {
                "asset_id": a["asset-id"],
                "amount": a["amount"],
                "is_frozen": a.get("is-frozen", False),
                "is_payment_asset": a["asset-id"] == mandated,
            }
            for a in assets_raw
        ],
        "status": info.get("status"),
    }


@router.get("/network")
def network_info() -> dict:
    """Network parameters (genesis, consensus version, fee floor)."""
    client = _get_algorand_client()
    try:
        if client:
            params = client.client.algod.suggested_params()
            genesis = client.client.algod.genesis()
        else:
            algod = _algod_client()
            if not algod:
                return {**_stub_network_status(), "payment_asset": {
                    "asset_id": settings.algorand_asset_id,
                    "network": settings.algorand_network,
                }}
            params = algod.suggested_params()
            genesis = algod.genesis()
        return {
            "source": "algokit-utils" if client else "algosdk",
            "network": settings.algorand_network,
            "genesis_id": getattr(params, "gen", genesis.get("id", "")),
            "genesis_hash": getattr(params, "gh", ""),
            "first_valid": getattr(params, "first", 0),
            "last_valid": getattr(params, "last", 0),
            "min_fee": getattr(params, "fee", 1000),
            "flat_fee": getattr(params, "flat_fee", True),
            "payment_asset": {
                "asset_id": settings.algorand_asset_id,
                "network": settings.algorand_network,
                "x402_network": settings.x402_network,
                "unit_name": settings.algorand_asset_unit_name,
                "decimals": settings.x402_asset_decimals,
            },
        }
    except Exception as exc:
        return {**_stub_network_status(), "error": str(exc)}


@router.get("/block/{round_number}")
def block_info(round_number: int) -> dict:
    """Block header data for a given round."""
    client = _get_algorand_client()
    try:
        if client:
            raw = client.client.algod.block_info(round_number)
        else:
            algod = _algod_client()
            if not algod:
                raise HTTPException(status_code=503, detail="algod not configured")
            raw = algod.block_info(round_number)
        block = raw.get("block", raw)
        return {
            "round": round_number,
            "timestamp": block.get("ts"),
            "transactions_count": len(block.get("txns", [])),
            "proposer": block.get("prop", {}).get("oprop", ""),
            "previous_block_hash": block.get("prev", ""),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"algod error: {exc}")


@router.get("/configuration")
def configuration() -> dict:
    """AlgoKit configuration report: which network, which asset, and health checks."""
    from ..algorand import configuration_report
    report = configuration_report()

    # Annotate with AlgoKit availability
    try:
        import algokit_utils  # type: ignore
        report["algokit_utils_version"] = getattr(algokit_utils, "__version__", "installed")
        report["algokit_available"] = True
    except ImportError:
        report["algokit_available"] = False
        report["algokit_note"] = "Run `pip install algokit-utils` to enable on-chain queries"

    try:
        import algosdk  # type: ignore
        report["algosdk_version"] = getattr(algosdk, "__version__", "installed")
    except ImportError:
        report["algosdk_version"] = None

    report["algod_configured"] = bool(settings.algod_url)
    report["algod_url_hint"] = (settings.algod_url[:30] + "…") if settings.algod_url else None
    return report
