"""Payment rail taxonomy and the external-provider payment interface.

Two rails exist on this exchange and they never mix:

``M2X_ALGORAND``
    The rail every consumer pays on: x402 ``exact`` scheme, denominated in the
    mandated Algorand Standard Asset, escrowed and settled by
    :mod:`app.x402.facilitator`. Nothing in this module changes it.

``ZERION_X402``
    Zerion's own pay-per-request rail: USDC on Base or Solana, settled by
    Zerion's facilitator, with no Zerion API key required. Used only for the
    leg between *this exchange* and *Zerion*.

``API_KEY``
    Not a payment rail at all — the marker used when an external provider is
    reached with a subscription credential and no per-request payment occurs.
    Recorded explicitly so a receipt never leaves "how was this paid for?"
    implicit.

An adapter's job is narrow: decide whether it can pay, pay, and hand back
*normalized, secret-free* metadata the job/receipt/audit system can store.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from ..algorand import asset_descriptor


class PaymentRail(str, enum.Enum):
    """Which payment network a leg of a transaction settles on."""

    M2X_ALGORAND = "m2x_algorand"
    ZERION_X402 = "zerion_x402"
    API_KEY = "api_key"
    NONE = "none"


class PaymentRailError(RuntimeError):
    """An external rail could not authorize or settle a request."""

    code = "payment_failed"


@dataclass
class PaymentOutcome:
    """Normalized result of paying an external provider for one request.

    Deliberately carries no credential material: an adapter that has just used a
    private key must still be safe to serialize into a job result, a receipt and
    an audit row.
    """

    rail: PaymentRail
    status: str                       # settled | not_required | simulated | failed | skipped
    amount: str = "0"
    currency: str = "USDC"
    network: str = ""                 # base | solana | ""
    transaction: str = ""
    payment_id: str = ""
    settled: bool = False
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("settled", "not_required", "simulated")

    def as_dict(self) -> dict[str, Any]:
        body = asdict(self)
        body["rail"] = self.rail.value
        return body

    @classmethod
    def not_required(cls, detail: str = "") -> "PaymentOutcome":
        return cls(rail=PaymentRail.API_KEY, status="not_required", amount="0",
                   currency="USDC", settled=False, detail=detail or
                   "provider reached with a subscription credential; no per-request payment")

    @classmethod
    def failed(cls, rail: PaymentRail, detail: str) -> "PaymentOutcome":
        return cls(rail=rail, status="failed", settled=False, detail=detail)


class ExternalProviderPaymentAdapter(ABC):
    """Pays one external provider on that provider's own rail.

    Implementations must:

    * report which rail they are configured for *before* anything is charged, so
      a caller can refuse to start work it cannot pay for;
    * never accept credentials from user-controlled input;
    * never return, log or persist private keys or API keys;
    * return a :class:`PaymentOutcome` rather than raising for an ordinary
      declined payment, so the failure is recorded like any other job outcome.
    """

    #: Stable provider identifier, matching the marketplace provider slug.
    provider_id: str = ""

    @property
    @abstractmethod
    def rail(self) -> PaymentRail:
        """The rail this adapter is currently configured to pay on."""

    @abstractmethod
    def available(self) -> bool:
        """True when this adapter holds everything it needs to pay."""

    @abstractmethod
    def quote_micros(self) -> int:
        """Expected cost of one request, in micro-units of the quote currency."""

    @abstractmethod
    def pay(self, *, request_id: str, capability: str, **context: Any) -> PaymentOutcome:
        """Authorize one request. Never raises for a declined payment."""

    def status(self) -> dict[str, Any]:
        """Non-secret description of this adapter, safe for API and dashboard."""
        return {
            "provider": self.provider_id,
            "rail": self.rail.value,
            "available": self.available(),
            "quote_micros": self.quote_micros(),
        }


def m2x_rail_descriptor() -> dict[str, Any]:
    """The exchange's own rail, in the shape listings and receipts render."""
    return {
        "rail": PaymentRail.M2X_ALGORAND.value,
        "protocol": "x402",
        **asset_descriptor(),
    }
