"""Normalization: Zerion output -> one stable M2X envelope.

The API returns JSON:API documents (``data.attributes``); the CLI returns its own
JSON and is explicitly allowed to change shape between releases. Everything the
exchange stores, hashes, receipts and shows a consumer goes through here first,
so a job result means the same thing whichever transport produced it.

Extraction is defensive by design: an unrecognised field is dropped, never
guessed at, and the untouched provider document is kept under ``data.raw`` so an
audit can always go back to what Zerion actually said.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...integrity import canonical_json, sha256_hex
from .models import PROVIDER_ID, ZerionRequestSpec

INTEGRITY_NOTE = (
    "SHA-256 over the canonical JSON of this normalized response. It proves the "
    "integrity of what M2X received and stored; it is not a proof of on-chain truth."
)


# --------------------------------------------------------------------------- #
# JSON:API helpers
# --------------------------------------------------------------------------- #
def _payload(doc: Any) -> Any:
    """Unwrap the content of a provider document.

    Both shapes nest the interesting part under ``data`` — the API because
    JSON:API says so, the CLI because it mirrors the API — so unwrapping once is
    correct for either. :func:`_attrs` then absorbs the remaining difference:
    a JSON:API resource keeps its fields under ``attributes``, CLI output does
    not.
    """
    if isinstance(doc, dict) and isinstance(doc.get("data"), (list, dict)):
        return doc["data"]
    return doc


def _attrs(item: Any) -> dict:
    if isinstance(item, dict):
        inner = item.get("attributes")
        if isinstance(inner, dict):
            return inner
        return item
    return {}


def _rel_id(item: Any, name: str) -> str:
    if not isinstance(item, dict):
        return ""
    rel = (item.get("relationships") or {}).get(name) or {}
    data = rel.get("data") or {}
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    # CLI shape: the chain may simply be a string field.
    value = item.get(name) or _attrs(item).get(name)
    return str(value) if isinstance(value, (str, int)) else ""


def _num(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(mapping: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return default


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "positions", "transactions", "tokens", "results", "list", "chains"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _money(value: Any) -> float:
    return round(_num(value, 0.0) or 0.0, 6)


# --------------------------------------------------------------------------- #
# Per-capability extraction
# --------------------------------------------------------------------------- #
def _portfolio(doc: Any, currency: str) -> dict:
    attrs = _attrs(_payload(doc))
    totals = _first(attrs, "total", default={}) or {}
    changes = _first(attrs, "changes", default={}) or {}
    total_value = _num(_first(totals, "positions", "value", "total"))
    if total_value is None:
        total_value = _num(_first(attrs, "total_value", "totalValue", "value"), 0.0)
    return {
        "total_value": _money(total_value),
        "currency": currency,
        "changes": {
            "absolute_1d": _money(_first(changes, "absolute_1d", "absolute1d")),
            "percent_1d": _money(_first(changes, "percent_1d", "percent1d")),
        },
        "by_chain": _first(attrs, "positions_distribution_by_chain", "by_chain", default={}) or {},
        "by_type": _first(attrs, "positions_distribution_by_type", "by_type", default={}) or {},
    }


def _one_position(item: Any) -> dict:
    attrs = _attrs(item)
    fungible = _first(attrs, "fungible_info", "fungible", default={}) or {}
    quantity = _first(attrs, "quantity", default={}) or {}
    changes = _first(attrs, "changes", default={}) or {}
    return {
        "name": str(_first(fungible, "name", default=_first(attrs, "name", default="")) or ""),
        "symbol": str(_first(fungible, "symbol", default=_first(attrs, "symbol", default="")) or ""),
        "chain": _rel_id(item, "chain"),
        "protocol": _first(attrs, "protocol", default="") or "",
        "position_type": _first(attrs, "position_type", "positionType", default="wallet") or "wallet",
        "quantity": _num(_first(quantity, "float", "numeric", default=_first(attrs, "amount")), 0.0),
        "price": _money(_first(attrs, "price")),
        "value": _money(_first(attrs, "value")),
        "change_1d_percent": _money(_first(changes, "percent_1d", "percent1d")),
    }


def _positions(doc: Any, currency: str, *, defi: bool) -> dict:
    items = _as_list(_payload(doc))
    positions = [_one_position(item) for item in items]
    positions.sort(key=lambda p: p["value"], reverse=True)
    total = round(sum(p["value"] for p in positions), 6)
    body: dict[str, Any] = {
        "count": len(positions),
        "total_value": total,
        "currency": currency,
        "positions": positions[:100],
    }
    if defi:
        grouped: dict[str, dict] = {}
        for position in positions:
            key = position["protocol"] or "unknown"
            bucket = grouped.setdefault(key, {"protocol": key, "value": 0.0, "positions": []})
            bucket["value"] = round(bucket["value"] + position["value"], 6)
            bucket["positions"].append(position)
        body["protocols"] = sorted(grouped.values(), key=lambda g: g["value"], reverse=True)
        body["protocol_count"] = len(grouped)
    return body


def _pnl(doc: Any, currency: str) -> dict:
    attrs = _attrs(_payload(doc))
    return {
        "currency": currency,
        "total_gain": _money(_first(attrs, "total_gain", "totalGain")),
        "realized_gain": _money(_first(attrs, "realized_gain", "realizedGain")),
        "unrealized_gain": _money(_first(attrs, "unrealized_gain", "unrealizedGain")),
        "total_fee": _money(_first(attrs, "total_fee", "totalFee")),
        "total_invested": _money(_first(attrs, "total_invested", "totalInvested")),
        "net_invested": _money(_first(attrs, "net_invested", "netInvested")),
        "realized_cost_basis": _money(_first(attrs, "realized_cost_basis", "realizedCostBasis")),
        "relative_total_gain_percentage": _money(
            _first(attrs, "relative_total_gain_percentage", "relativeTotalGainPercentage")
        ),
        "method": "FIFO",
    }


def _one_transaction(item: Any) -> dict:
    attrs = _attrs(item)
    fee = _first(attrs, "fee", default={}) or {}
    transfers = _as_list(_first(attrs, "transfers", default=[]))
    return {
        "hash": str(_first(attrs, "hash", "tx_hash", "txHash", default="") or ""),
        "operation_type": str(_first(attrs, "operation_type", "operationType", "type",
                                     default="") or ""),
        "status": str(_first(attrs, "status", default="") or ""),
        "mined_at": str(_first(attrs, "mined_at", "minedAt", "timestamp", default="") or ""),
        "chain": _rel_id(item, "chain"),
        "sent_from": str(_first(attrs, "sent_from", "sentFrom", "from", default="") or ""),
        "sent_to": str(_first(attrs, "sent_to", "sentTo", "to", default="") or ""),
        "fee_value": _money(_first(fee, "value") if isinstance(fee, dict) else fee),
        "transfer_count": len(transfers),
    }


def _transactions(doc: Any, limit: int) -> dict:
    items = _as_list(_payload(doc))
    rows = [_one_transaction(item) for item in items][:limit]
    return {"count": len(rows), "limit": limit, "transactions": rows}


def _tokens(doc: Any, currency: str) -> dict:
    items = _as_list(_payload(doc))
    tokens = []
    for item in items:
        attrs = _attrs(item)
        market = _first(attrs, "market_data", "marketData", default={}) or {}
        changes = _first(market, "changes", default={}) or {}
        tokens.append({
            "id": str(item.get("id", "")) if isinstance(item, dict) else "",
            "name": str(_first(attrs, "name", default="") or ""),
            "symbol": str(_first(attrs, "symbol", default="") or ""),
            "price": _money(_first(market, "price", default=_first(attrs, "price"))),
            "market_cap": _money(_first(market, "market_cap", "marketCap")),
            "change_1d_percent": _money(_first(changes, "percent_1d", "percent1d")),
        })
    return {"count": len(tokens), "currency": currency, "tokens": tokens[:100]}


def _chains(doc: Any) -> dict:
    items = _as_list(_payload(doc))
    chains = []
    for item in items:
        attrs = _attrs(item)
        flags = _first(attrs, "flags", default={}) or {}
        chains.append({
            "id": str(item.get("id", "")) if isinstance(item, dict) else "",
            "name": str(_first(attrs, "name", default="") or ""),
            "supports_trading": bool(_first(flags, "supports_trading", "supportsTrading",
                                            default=False)),
            "supports_sending": bool(_first(flags, "supports_sending", "supportsSending",
                                            default=False)),
        })
    return {"count": len(chains), "chains": chains}


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
def _summary(request_type: str, data: dict, wallet: str) -> str:
    who = wallet or "the query"
    currency = str(data.get("currency", "usd")).upper()
    if request_type == "portfolio":
        change = data.get("changes", {}).get("percent_1d", 0)
        return (f"{who} holds {data.get('total_value', 0):,.2f} {currency} "
                f"across {len(data.get('by_chain') or {})} chain(s), {change:+.2f}% in 24h.")
    if request_type == "positions":
        return (f"{who} holds {data.get('count', 0)} token position(s) worth "
                f"{data.get('total_value', 0):,.2f} {currency}.")
    if request_type == "defi_positions":
        return (f"{who} has {data.get('count', 0)} DeFi position(s) across "
                f"{data.get('protocol_count', 0)} protocol(s) worth "
                f"{data.get('total_value', 0):,.2f} {currency}.")
    if request_type == "pnl":
        return (f"{who} PnL: total {data.get('total_gain', 0):,.2f} {currency} "
                f"(realized {data.get('realized_gain', 0):,.2f}, unrealized "
                f"{data.get('unrealized_gain', 0):,.2f}), fees "
                f"{data.get('total_fee', 0):,.2f}.")
    if request_type == "transactions":
        return f"{who} has {data.get('count', 0)} recent transaction(s)."
    if request_type == "token_search":
        return f"{data.get('count', 0)} token(s) matched."
    if request_type == "chains":
        return f"Zerion indexes {data.get('count', 0)} chain(s)."
    if request_type == "wallet_analysis":
        parts = []
        if "portfolio" in data:
            parts.append(f"portfolio {data['portfolio'].get('total_value', 0):,.2f} {currency}")
        if "positions" in data:
            parts.append(f"{data['positions'].get('count', 0)} token position(s)")
        if "pnl" in data:
            parts.append(f"total PnL {data['pnl'].get('total_gain', 0):,.2f}")
        if "transactions" in data:
            parts.append(f"{data['transactions'].get('count', 0)} recent transaction(s)")
        return f"{who}: " + ", ".join(parts) + "." if parts else f"{who}: no data returned."
    return f"{request_type} completed for {who}."


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
def normalize(
    spec: ZerionRequestSpec,
    raw: Any,
    *,
    source: str,
    payment: dict | None = None,
    warnings: list[str] | None = None,
    include_raw: bool = True,
) -> dict:
    """Build the M2X envelope for one Zerion request, integrity hash included.

    ``raw`` is a mapping of leg name -> provider document (``wallet_analysis``
    has four legs; everything else has one).
    """
    key = spec.capability.key
    payloads = raw if isinstance(raw, dict) else {key: raw}

    if key == "wallet_analysis":
        data: dict[str, Any] = {"currency": spec.currency}
        if "portfolio" in payloads:
            data["portfolio"] = _portfolio(payloads["portfolio"], spec.currency)
        if "positions" in payloads:
            data["positions"] = _positions(payloads["positions"], spec.currency, defi=False)
        if "transactions" in payloads:
            data["transactions"] = _transactions(payloads["transactions"], spec.limit)
        if "pnl" in payloads:
            data["pnl"] = _pnl(payloads["pnl"], spec.currency)
    else:
        doc = payloads.get(key, next(iter(payloads.values()), None))
        if key == "portfolio":
            data = _portfolio(doc, spec.currency)
        elif key == "positions":
            data = _positions(doc, spec.currency, defi=False)
        elif key == "defi_positions":
            data = _positions(doc, spec.currency, defi=True)
        elif key == "pnl":
            data = _pnl(doc, spec.currency)
        elif key == "transactions":
            data = _transactions(doc, spec.limit)
        elif key == "token_search":
            data = _tokens(doc, spec.currency)
        elif key == "chains":
            data = _chains(doc)
        else:  # pragma: no cover - capability registry invariant
            data = {"raw": doc}

    data["summary"] = _summary(key, data, spec.wallet or spec.query)
    if include_raw:
        data["raw"] = payloads

    envelope: dict[str, Any] = {
        "provider": PROVIDER_ID,
        "wallet": spec.wallet,
        "request_type": key,
        "chain": spec.chain or "all",
        "currency": spec.currency,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "data": data,
        "payment": payment or {},
    }
    if warnings:
        envelope["warnings"] = warnings[:8]

    envelope["integrity"] = integrity_block(envelope)
    return envelope


def integrity_block(envelope: dict) -> dict:
    """SHA-256 over the canonical JSON of everything except the block itself."""
    body = {k: v for k, v in envelope.items() if k != "integrity"}
    digest = sha256_hex(canonical_json(body))
    return {
        "algorithm": "sha256",
        "hash": digest,
        "verified": True,
        "scope": "normalized_response",
        "canonical_length": len(canonical_json(body)),
        "note": INTEGRITY_NOTE,
    }


def verify_envelope(envelope: dict) -> dict:
    """Recompute the hash of a stored envelope and report whether it still matches."""
    claimed = (envelope.get("integrity") or {}).get("hash", "")
    recomputed = integrity_block(envelope)["hash"]
    return {
        "algorithm": "sha256",
        "claimed_hash": claimed,
        "computed_hash": recomputed,
        "valid": bool(claimed) and claimed == recomputed,
        "note": INTEGRITY_NOTE,
    }


def summary_line(envelope: dict) -> str:
    return str(((envelope.get("data") or {}).get("summary")) or "")
