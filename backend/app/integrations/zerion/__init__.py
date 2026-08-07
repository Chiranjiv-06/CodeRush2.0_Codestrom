"""Zerion Onchain Intelligence — a first-class external provider on the exchange.

Layout
------
``models``        capability catalog, address/chain validation, request spec
``client``        HTTP transport against the documented Zerion REST API
``cli``           subprocess transport against the official ``zerion`` CLI
``payment``       rail selection and the x402 payment adapter
``normalizer``    provider output -> one stable M2X envelope + SHA-256 integrity
``quota``         per-job / per-session / per-spend limits and budget checks
``demo``          deterministic fixtures for credential-free environments
``service``       the orchestrated request and the job executor
``registration``  marketplace provider + service rows, and executor wiring
``errors``        structured, credential-safe error types

Importing this package registers the executors; call
:func:`~app.integrations.zerion.registration.ensure_registered` to create the
marketplace rows.
"""
from .errors import (
    ZerionAuthError,
    ZerionBudgetError,
    ZerionConfigError,
    ZerionError,
    ZerionQuotaError,
    ZerionRateLimitError,
    ZerionTimeoutError,
    ZerionUnavailableError,
    ZerionValidationError,
)
from .models import (
    CAPABILITIES,
    PROVIDER_ID,
    PROVIDER_NAME,
    SLUG_TO_CAPABILITY,
    ZerionCapability,
    ZerionRequestSpec,
    capability_for,
    normalize_wallet,
    validate_payload,
)
from .normalizer import normalize, verify_envelope
from .payment import ZerionX402PaymentAdapter, active_rail, adapter, mode_report, transport_name
from .registration import capability_catalog, ensure_registered, register_executors
from .service import ZerionOutcome, execute_zerion_job, run_request, status_report

__all__ = [
    "CAPABILITIES",
    "PROVIDER_ID",
    "PROVIDER_NAME",
    "SLUG_TO_CAPABILITY",
    "ZerionAuthError",
    "ZerionBudgetError",
    "ZerionCapability",
    "ZerionConfigError",
    "ZerionError",
    "ZerionOutcome",
    "ZerionQuotaError",
    "ZerionRateLimitError",
    "ZerionRequestSpec",
    "ZerionTimeoutError",
    "ZerionUnavailableError",
    "ZerionValidationError",
    "ZerionX402PaymentAdapter",
    "active_rail",
    "adapter",
    "capability_catalog",
    "capability_for",
    "ensure_registered",
    "execute_zerion_job",
    "mode_report",
    "normalize",
    "normalize_wallet",
    "register_executors",
    "run_request",
    "status_report",
    "transport_name",
    "validate_payload",
    "verify_envelope",
]
