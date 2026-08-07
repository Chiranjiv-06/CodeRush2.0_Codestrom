"""Usage metering and price computation.

Quotes are upper bounds; settlement charges measured usage and refunds the
difference, so a consumer never pays more than the quote they accepted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import settings
from .models import Service

MICROS_PER_UNIT = 10 ** 6


@dataclass
class Usage:
    cpu_ms: int = 0
    wall_ms: int = 0
    peak_memory_mb: float = 0.0
    egress_bytes: int = 0
    invocations: int = 1
    exit_code: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PriceBreakdown:
    base_micros: int
    cpu_micros: int
    egress_micros: int
    subtotal_micros: int
    capped_micros: int
    platform_fee_micros: int
    provider_net_micros: int

    def as_dict(self) -> dict:
        return asdict(self)


def to_micros(units: float) -> int:
    return int(round(units * MICROS_PER_UNIT))


def to_units(micros: int) -> float:
    return round(micros / MICROS_PER_UNIT, 6)


def format_price(micros: int) -> str:
    return f"{to_units(micros):.6f}"


def platform_fee(micros: int) -> int:
    return (micros * settings.platform_fee_bps) // 10_000


def price_for_usage(service: Service, usage: Usage) -> PriceBreakdown:
    base = service.base_price_micros * max(usage.invocations, 1)
    cpu = int(service.price_per_cpu_second_micros * (usage.cpu_ms / 1000.0))
    egress = int(service.price_per_mb_egress_micros * (usage.egress_bytes / (1024 * 1024)))
    subtotal = base + cpu + egress
    capped = min(subtotal, service.max_price_micros)
    fee = platform_fee(capped)
    return PriceBreakdown(
        base_micros=base,
        cpu_micros=cpu,
        egress_micros=egress,
        subtotal_micros=subtotal,
        capped_micros=capped,
        platform_fee_micros=fee,
        provider_net_micros=capped - fee,
    )


def quote_service(service: Service, payload: dict | None = None) -> PriceBreakdown:
    """Worst-case estimate used for the x402 `maxAmountRequired`."""
    payload = payload or {}
    est_cpu_ms = int(payload.get("_estimated_cpu_ms", service.max_runtime_seconds * 1000 * 0.5))
    est_egress = int(payload.get("_estimated_egress_bytes", 64 * 1024))
    usage = Usage(cpu_ms=est_cpu_ms, egress_bytes=est_egress, invocations=1)
    return price_for_usage(service, usage)
