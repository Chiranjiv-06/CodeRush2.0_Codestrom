"""External service-provider integrations.

Each integration registers itself as an ordinary marketplace provider so the
existing catalog, Bazaar discovery, quoting, job lifecycle, metering, integrity
and receipt machinery apply to it unchanged. Only two provider-agnostic hooks
exist (:mod:`app.integrations.registry`): payload validation before a job is
priced, and execution dispatch instead of a sandbox worker.
"""
from .registry import (
    ExternalService,
    external_metadata_for,
    external_service_for,
    is_external_service,
    register_external_service,
    registered_external_services,
)

__all__ = [
    "ExternalService",
    "external_metadata_for",
    "external_service_for",
    "is_external_service",
    "register_external_service",
    "registered_external_services",
]
