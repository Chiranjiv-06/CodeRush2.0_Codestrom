"""Wire format for the x402 protocol.

Follows the x402 spec shape used by the official packages:

  server  -> 402 {x402Version, accepts: [PaymentRequirements], error}
  client  -> retry with `X-PAYMENT: base64(json(PaymentPayload))`
  server  -> 200 + `X-PAYMENT-RESPONSE: base64(json(SettleResponse))`

Atomic amounts are strings (JS-safe), decimals carried in `extra`.
"""
from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..algorand import asset_id as mandated_asset_id
from ..algorand import assert_asset, normalize_asset
from ..config import settings
from ..integrity import canonical_json, sign, verify_signature


class X402Error(ValueError):
    """Malformed or unacceptable payment material."""


@dataclass
class PaymentRequirements:
    scheme: str
    network: str
    maxAmountRequired: str
    resource: str
    description: str
    mimeType: str
    payTo: str
    maxTimeoutSeconds: int
    asset: str
    outputSchema: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @property
    def asset_id(self) -> int | None:
        """The ASA id these requirements are denominated in, if well-formed."""
        return normalize_asset(self.asset) or normalize_asset(self.extra.get("assetId"))


@dataclass
class PaymentPayload:
    x402Version: int
    scheme: str
    network: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_requirements(
    *,
    amount_micros: int,
    resource: str,
    description: str,
    pay_to: str,
    nonce: str,
    job_id: str | None = None,
    scheme: str = "exact",
    network: str | None = None,
    asset: str | None = None,
    timeout_seconds: int | None = None,
    output_schema: dict | None = None,
) -> PaymentRequirements:
    # An explicit asset is honoured only if it is the mandated ASA; anything else
    # is refused here rather than quoted and rejected later at settlement.
    asset_id = assert_asset(asset, context="payment requirements") if asset else mandated_asset_id()
    timeout = timeout_seconds or settings.x402_escrow_timeout_seconds
    return PaymentRequirements(
        scheme=scheme,
        network=network or settings.x402_network,
        maxAmountRequired=str(amount_micros),
        resource=resource,
        description=description,
        mimeType="application/json",
        payTo=pay_to,
        maxTimeoutSeconds=timeout,
        asset=str(asset_id),
        outputSchema=output_schema,
        extra={
            "blockchain": settings.blockchain,
            "assetId": asset_id,
            "name": settings.algorand_asset_unit_name,
            "display": settings.asset_display,
            "decimals": settings.x402_asset_decimals,
            "nonce": nonce,
            "jobId": job_id,
            "validAfter": int(time.time()) - 30,
            "validBefore": int(time.time()) + timeout,
        },
    )


def build_payment_required(
    requirements: list[PaymentRequirements], error: str = "payment required"
) -> dict[str, Any]:
    return {
        "x402Version": settings.x402_version,
        "error": error,
        "accepts": [r.as_dict() for r in requirements],
    }


def new_nonce() -> str:
    return secrets.token_hex(16)


# --------------------------------------------------------------------------- #
# Header codecs
# --------------------------------------------------------------------------- #
def encode_payment_header(payload: PaymentPayload | dict) -> str:
    body = payload.as_dict() if isinstance(payload, PaymentPayload) else payload
    return base64.b64encode(canonical_json(body).encode()).decode()


def decode_payment_header(header: str) -> PaymentPayload:
    if not header:
        raise X402Error("empty X-PAYMENT header")
    try:
        raw = base64.b64decode(header, validate=True)
        data = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise X402Error(f"X-PAYMENT is not base64 json: {exc}") from exc
    for required in ("x402Version", "scheme", "network", "payload"):
        if required not in data:
            raise X402Error(f"X-PAYMENT missing field '{required}'")
    if int(data["x402Version"]) != settings.x402_version:
        raise X402Error(f"unsupported x402Version {data['x402Version']}")
    return PaymentPayload(
        x402Version=int(data["x402Version"]),
        scheme=str(data["scheme"]),
        network=str(data["network"]),
        payload=dict(data["payload"]),
    )


def encode_settlement_header(result: dict[str, Any]) -> str:
    return base64.b64encode(canonical_json(result).encode()).decode()


def decode_settlement_header(header: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(header))


# --------------------------------------------------------------------------- #
# Authorization signing (`exact` scheme, ledger/mock settlement)
# --------------------------------------------------------------------------- #
def make_authorization(
    *, payer: str, pay_to: str, value_micros: int, nonce: str, resource: str,
    asset: Any = None, valid_after: int | None = None, valid_before: int | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    # ``asset`` is inside the signed body on purpose: swapping the asset a payer
    # authorized is then a signature failure, not just a field mismatch.
    asset_id = assert_asset(asset, context="authorization") if asset else mandated_asset_id()
    return {
        "from": payer,
        "to": pay_to,
        "value": str(value_micros),
        "asset": str(asset_id),
        "network": settings.x402_network,
        "nonce": nonce,
        "resource": resource,
        "validAfter": valid_after if valid_after is not None else now - 30,
        "validBefore": valid_before
        if valid_before is not None
        else now + settings.x402_escrow_timeout_seconds,
    }


def authorization_digest(authorization: dict[str, Any]) -> str:
    trimmed = {k: v for k, v in authorization.items() if k != "signature"}
    return canonical_json(trimmed)


def sign_authorization(authorization: dict[str, Any], payer_secret: str) -> str:
    return sign(authorization_digest(authorization), key=payer_secret)


def verify_authorization_signature(
    authorization: dict[str, Any], signature: str, payer_secret: str
) -> bool:
    return verify_signature(authorization_digest(authorization), signature, key=payer_secret)


def build_exact_payload(
    *, payer: str, pay_to: str, value_micros: int, nonce: str, resource: str, payer_secret: str,
    network: str | None = None, asset: Any = None,
) -> PaymentPayload:
    """Client-side helper: produce a signed `exact`-scheme payment payload."""
    auth = make_authorization(
        payer=payer, pay_to=pay_to, value_micros=value_micros, nonce=nonce, resource=resource,
        asset=asset,
    )
    return PaymentPayload(
        x402Version=settings.x402_version,
        scheme="exact",
        network=network or settings.x402_network,
        payload={
            "asset": auth["asset"],
            "authorization": auth,
            "signature": sign_authorization(auth, payer_secret),
        },
    )
