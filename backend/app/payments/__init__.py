"""Payment rails.

The exchange settles with its *consumers* on exactly one rail — the mandated
Algorand ASA over x402 (:mod:`app.x402`). External providers the exchange resells
may charge on a rail of their own; those live behind
:class:`~app.payments.rails.ExternalProviderPaymentAdapter` so provider-specific
money handling never leaks into the job lifecycle.
"""
from .rails import (
    ExternalProviderPaymentAdapter,
    PaymentOutcome,
    PaymentRail,
    m2x_rail_descriptor,
)

__all__ = [
    "ExternalProviderPaymentAdapter",
    "PaymentOutcome",
    "PaymentRail",
    "m2x_rail_descriptor",
]
