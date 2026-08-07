"""Zerion HTTP API client.

Talks to ``https://api.zerion.io/v1`` using the authentication scheme Zerion
documents: HTTP Basic, with the API key as the username and an empty password.

Retries are deliberately conservative and rail-aware. On the API-key rail a
retry costs nothing, so throttles (429), cold-cache responses (202/503) and
transport hiccups are retried with backoff. On a pay-per-request rail a retry
costs real money, so it is never issued automatically.

The key is read from settings at call time, used to build one header, and never
stored, returned or logged.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ...config import settings
from ...payments.rails import PaymentRail
from .errors import (
    ZerionAuthError,
    ZerionConfigError,
    ZerionRateLimitError,
    ZerionResponseError,
    ZerionTimeoutError,
    ZerionUnavailableError,
    sanitize,
)
from .models import ZerionRequestSpec
from .payment import active_rail

log = logging.getLogger("m2x.zerion.client")

RETRYABLE_STATUS = (202, 429, 500, 502, 503, 504)
RATE_LIMIT_HEADERS = (
    "RateLimit-Org-Second-Limit",
    "RateLimit-Org-Second-Remaining",
    "RateLimit-Org-Second-Reset",
    "RateLimit-Org-Day-Remaining",
    "RateLimit-Org-Month-Remaining",
    "RateLimit-Org-Tier",
)


@dataclass
class ZerionRawResult:
    """Whatever the transport returned, before normalization."""

    source: str                      # zerion_api | zerion_cli | zerion_demo
    payloads: dict[str, Any] = field(default_factory=dict)
    http_status: int = 0
    latency_ms: int = 0
    upstream_requests: int = 0
    provider_request_id: str = ""
    rate_limit: dict[str, str] = field(default_factory=dict)
    payment_evidence: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _auth_header() -> str:
    """``Basic base64("<key>:")`` — built per call, never cached or logged."""
    key = settings.zerion_api_key
    if not key:
        raise ZerionConfigError("no ZERION_API_KEY configured for API-key mode")
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode()


class ZerionApiClient:
    """Thin, synchronous client for the documented Zerion REST endpoints."""

    source = "zerion_api"

    def __init__(self, *, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.zerion_api_url).rstrip("/")
        self.timeout = timeout or settings.zerion_timeout_seconds

    # -- request plumbing --------------------------------------------------- #
    @property
    def _max_attempts(self) -> int:
        # Never auto-retry a request that is paid for individually.
        if active_rail() is PaymentRail.ZERION_X402:
            return 1
        return max(1, settings.zerion_max_retries + 1)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[dict, int, dict]:
        import httpx

        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": _auth_header(),
            "Accept": "application/json",
            "User-Agent": f"m2x/{settings.version} (+zerion-integration)",
        }
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}

        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.get(url, params=clean, headers=headers)
            except httpx.TimeoutException:
                last_error = ZerionTimeoutError(
                    f"Zerion did not answer within {self.timeout}s", path=path
                )
            except httpx.HTTPError as exc:
                last_error = ZerionUnavailableError(
                    f"could not reach Zerion: {sanitize(exc)}", path=path
                )
            else:
                meta = {h: resp.headers[h] for h in RATE_LIMIT_HEADERS if h in resp.headers}
                if resp.status_code == 200:
                    try:
                        return resp.json(), resp.status_code, meta
                    except ValueError as exc:
                        raise ZerionResponseError(
                            f"Zerion returned a non-JSON body: {sanitize(exc)}",
                            path=path, http_status=resp.status_code,
                        )
                if resp.status_code in (401, 403):
                    # Never echo the body: it can quote the credential back.
                    raise ZerionAuthError(
                        "Zerion rejected the configured credential",
                        path=path, http_status=resp.status_code,
                    )
                if resp.status_code == 429:
                    last_error = ZerionRateLimitError(
                        "Zerion rate limit reached",
                        path=path, http_status=429,
                        reset=meta.get("RateLimit-Org-Second-Reset"),
                    )
                elif resp.status_code in RETRYABLE_STATUS:
                    last_error = ZerionUnavailableError(
                        f"Zerion returned {resp.status_code}",
                        path=path, http_status=resp.status_code,
                    )
                else:
                    raise ZerionResponseError(
                        f"Zerion returned {resp.status_code}",
                        path=path, http_status=resp.status_code,
                    )

            if attempt + 1 < self._max_attempts:
                time.sleep(min(0.4 * (2 ** attempt), 2.0))

        raise last_error or ZerionUnavailableError("Zerion request failed", path=path)

    # -- documented endpoints ---------------------------------------------- #
    def portfolio(self, spec: ZerionRequestSpec) -> dict:
        body, _s, _m = self._get(
            f"/v1/wallets/{spec.wallet}/portfolio",
            {"currency": spec.currency, "filter[positions]": "no_filter"},
        )
        return body

    def positions(self, spec: ZerionRequestSpec, *, defi_only: bool = False) -> dict:
        body, _s, _m = self._get(
            f"/v1/wallets/{spec.wallet}/positions/",
            {
                "currency": spec.currency,
                "filter[positions]": "only_complex" if defi_only else "only_simple",
                "filter[chain_ids]": spec.chain or None,
                "filter[trash]": "only_non_trash",
                "sort": "-value",
            },
        )
        return body

    def pnl(self, spec: ZerionRequestSpec) -> dict:
        body, _s, _m = self._get(
            f"/v1/wallets/{spec.wallet}/pnl",
            {"currency": spec.currency, "filter[chain_ids]": spec.chain or None},
        )
        return body

    def transactions(self, spec: ZerionRequestSpec) -> dict:
        body, _s, _m = self._get(
            f"/v1/wallets/{spec.wallet}/transactions/",
            {
                "currency": spec.currency,
                "page[size]": spec.limit,
                "filter[chain_ids]": spec.chain or None,
                "filter[trash]": "only_non_trash",
            },
        )
        return body

    def fungibles(self, spec: ZerionRequestSpec) -> dict:
        body, _s, _m = self._get(
            "/v1/fungibles/",
            {
                "currency": spec.currency,
                "filter[search_query]": spec.query,
                "page[size]": spec.limit,
            },
        )
        return body

    def chains(self, spec: ZerionRequestSpec) -> dict:
        body, _s, _m = self._get("/v1/chains/", {})
        return body

    # -- capability dispatch ------------------------------------------------ #
    def execute(self, spec: ZerionRequestSpec) -> ZerionRawResult:
        """Run one capability. ``wallet_analysis`` fans out to four endpoints."""
        started = time.perf_counter()
        key = spec.capability.key
        payloads: dict[str, Any] = {}
        upstream = 0

        if key == "wallet_analysis":
            # Sequential on purpose: a partial answer is more useful than none,
            # so a failing leg is recorded and the rest still return.
            legs = (
                ("portfolio", lambda: self.portfolio(spec)),
                ("positions", lambda: self.positions(spec)),
                ("transactions", lambda: self.transactions(spec)),
                ("pnl", lambda: self.pnl(spec)),
            )
            warnings: list[str] = []
            for name, call in legs:
                try:
                    payloads[name] = call()
                    upstream += 1
                except (ZerionAuthError, ZerionConfigError):
                    raise
                except Exception as exc:
                    warnings.append(f"{name}: {sanitize(exc)}")
            if not payloads:
                raise ZerionUnavailableError(
                    "every leg of the wallet analysis failed", detail_legs=warnings[:4]
                )
            result = ZerionRawResult(
                source=self.source, payloads=payloads, http_status=200,
                upstream_requests=upstream, warnings=warnings,
            )
        else:
            caller = {
                "portfolio": lambda: self.portfolio(spec),
                "positions": lambda: self.positions(spec),
                "defi_positions": lambda: self.positions(spec, defi_only=True),
                "pnl": lambda: self.pnl(spec),
                "transactions": lambda: self.transactions(spec),
                "token_search": lambda: self.fungibles(spec),
                "chains": lambda: self.chains(spec),
            }[key]
            payloads[key] = caller()
            result = ZerionRawResult(
                source=self.source, payloads=payloads, http_status=200, upstream_requests=1,
            )

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result


client = ZerionApiClient()
