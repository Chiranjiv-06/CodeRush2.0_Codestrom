"""Zerion capability catalog, input validation and request/response types.

One :class:`ZerionCapability` per thing the exchange sells. Everything the
marketplace, the Bazaar advertisement, the agent planner, the quota ledger and
the dashboard need to know about a Zerion capability is declared here once:
identifier, schemas, price, rail, quota, timeout and cleanup policy.

The command allowlist lives here too. A capability's CLI subcommand and API path
are chosen from this table by key — never assembled from anything a user typed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ...config import settings
from .errors import ZerionValidationError

PROVIDER_ID = "zerion"
PROVIDER_NAME = "Zerion Onchain Intelligence"
PROVIDER_CATEGORY = "blockchain-data"
PROVIDER_DESCRIPTION = (
    "Real-time wallet and on-chain intelligence across 40+ chains: portfolio value, "
    "token and DeFi positions, transaction history, realized/unrealized PnL and token "
    "metadata. Paid per request over Zerion's x402 rail (USDC on Base or Solana) or "
    "with a Zerion API key."
)
PROVIDER_HOMEPAGE = "https://developers.zerion.io/"

# --------------------------------------------------------------------------- #
# Address validation
# --------------------------------------------------------------------------- #
# Deliberately strict. These patterns are the only thing that ever reaches a
# subprocess argument or a URL path segment, so anything that is not obviously
# an address is refused rather than escaped.
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
ENS_NAME = re.compile(r"^(?=.{3,255}$)[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9][a-z0-9-]{0,62})*\.eth$")
CHAIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
SEARCH_QUERY = re.compile(r"^[\w .\-+:/]{1,64}$", re.UNICODE)


def normalize_wallet(value: Any) -> str:
    """Validate a wallet identifier and return its canonical form.

    Accepts a checksummed or lowercase EVM address, a base58 Solana address, or
    an ENS name. Raises :class:`ZerionValidationError` for anything else — an
    invalid address must never reach a paid request.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ZerionValidationError("a wallet address or ENS name is required")
    if EVM_ADDRESS.match(raw):
        return raw.lower()
    if ENS_NAME.match(raw.lower()):
        return raw.lower()
    if SOLANA_ADDRESS.match(raw):
        return raw
    raise ZerionValidationError(
        f"{raw[:64]!r} is not a valid EVM address, Solana address or .eth name",
        wallet=raw[:64],
    )


def wallet_kind(wallet: str) -> str:
    if EVM_ADDRESS.match(wallet):
        return "evm"
    if wallet.endswith(".eth"):
        return "ens"
    return "solana"


def normalize_chain(value: Any, *, required: bool = False) -> str:
    """Validate an optional chain id against the configured allowlist."""
    raw = str(value or "").strip().lower()
    if not raw:
        if required:
            raise ZerionValidationError("a chain id is required")
        return ""
    if not CHAIN_ID.match(raw):
        raise ZerionValidationError(f"{raw[:32]!r} is not a valid chain id", chain=raw[:32])
    allowed = settings.zerion_allowed_chain_list
    if allowed and raw not in allowed:
        raise ZerionValidationError(
            f"chain {raw!r} is not permitted by this deployment",
            chain=raw, allowed_chains=allowed,
        )
    return raw


def normalize_query(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ZerionValidationError("a search query is required")
    if not SEARCH_QUERY.match(raw):
        raise ZerionValidationError("search query contains unsupported characters")
    return raw


def normalize_limit(value: Any, *, default: int, maximum: int = 100) -> int:
    try:
        limit = int(value) if value is not None else default
    except (TypeError, ValueError):
        raise ZerionValidationError("limit must be an integer")
    return max(1, min(limit, maximum))


# --------------------------------------------------------------------------- #
# Capability catalog
# --------------------------------------------------------------------------- #
_WALLET_INPUT = {
    "type": "object",
    "required": ["wallet"],
    "properties": {
        "wallet": {"type": "string",
                   "description": "EVM address, Solana address or .eth ENS name"},
        "chain": {"type": "string", "description": "optional chain id filter, e.g. 'base'"},
        "currency": {"type": "string", "default": "usd"},
    },
}

_ENVELOPE_OUTPUT = {
    "type": "object",
    "required": ["provider", "request_type", "data", "integrity"],
    "properties": {
        "provider": {"const": "zerion"},
        "wallet": {"type": "string"},
        "request_type": {"type": "string"},
        "chain": {"type": "string"},
        "timestamp": {"type": "string", "format": "date-time"},
        "data": {"type": "object"},
        "source": {"enum": ["zerion_api", "zerion_cli", "zerion_demo"]},
        "payment": {"type": "object"},
        "integrity": {"type": "object"},
    },
}


@dataclass(frozen=True)
class ZerionCapability:
    """One priced, callable Zerion capability."""

    key: str                       # internal capability name
    slug: str                      # marketplace service slug (unique per provider)
    name: str
    description: str
    #: Zerion CLI subcommand. Fixed strings only — this is the command allowlist.
    cli_command: str
    #: ``/v1``-relative API path template; ``{wallet}`` is the only substitution
    #: and is always a value that passed :func:`normalize_wallet`.
    api_path: str = ""
    needs_wallet: bool = True
    needs_query: bool = False
    tags: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=lambda: dict(_WALLET_INPUT))
    output_schema: dict[str, Any] = field(default_factory=lambda: dict(_ENVELOPE_OUTPUT))
    #: How many upstream Zerion requests one invocation costs (``analyze`` fans out).
    upstream_requests: int = 1

    # --- commercial terms -------------------------------------------------- #
    @property
    def price_micros(self) -> int:
        """What the exchange charges a consumer, in mandated-ASA micro-units."""
        return settings.zerion_price_micros * self.upstream_requests

    @property
    def max_price_micros(self) -> int:
        return self.price_micros * 4

    @property
    def cost_micros(self) -> int:
        """What one invocation costs the exchange on Zerion's rail."""
        return settings.zerion_cost_micros * self.upstream_requests

    @property
    def timeout_seconds(self) -> int:
        return max(5, int(settings.zerion_timeout_seconds * self.upstream_requests) + 10)

    def advertisement(self) -> dict[str, Any]:
        """Non-secret discovery record for this capability."""
        from .payment import active_rail, transport_name

        return {
            "provider": PROVIDER_ID,
            "provider_name": PROVIDER_NAME,
            "capability": self.key,
            "service_slug": self.slug,
            "name": self.name,
            "description": self.description,
            "category": PROVIDER_CATEGORY,
            "price_micros": self.price_micros,
            "max_price_micros": self.max_price_micros,
            "consumer_payment_rail": "m2x_algorand",
            "provider_payment_rail": active_rail().value,
            "provider_cost": {
                "micros": self.cost_micros,
                "currency": "USDC",
                "network": settings.zerion_x402_chain or None,
            },
            "transport": transport_name(),
            "supported_chains": settings.zerion_allowed_chain_list or "all",
            "default_chain": settings.zerion_default_chain,
            "quota": {
                "max_requests_per_job": settings.zerion_max_requests_per_job,
                "max_requests_per_session": settings.zerion_max_requests_per_session,
                "max_spend_micros": settings.zerion_max_spend_micros,
                "window_seconds": settings.zerion_quota_window_seconds,
            },
            "timeout_seconds": self.timeout_seconds,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "integrity": {
                "algorithm": "sha256",
                "scope": "canonical JSON of the normalized response as received by M2X",
                "receipt": "hash-chained M2X settlement receipt",
            },
            "cleanup": {
                "result_ttl_seconds": settings.zerion_result_ttl_seconds,
                "artifact_ttl_seconds": settings.artifact_ttl_seconds,
                "workspace": "no sandbox worker is provisioned for external services",
            },
            "docs": PROVIDER_HOMEPAGE,
        }


CAPABILITIES: dict[str, ZerionCapability] = {
    c.key: c
    for c in (
        ZerionCapability(
            key="wallet_analysis",
            slug="zerion-wallet-analysis",
            name="Zerion Wallet Analysis",
            description=(
                "Full read of a wallet: portfolio value, token and DeFi positions, "
                "recent transactions and realized/unrealized PnL, in one call."
            ),
            cli_command="analyze",
            api_path="",                       # fan-out; composed from the calls below
            tags=("zerion", "onchain", "wallet", "analyze", "blockchain-data", "portfolio"),
            upstream_requests=4,
        ),
        ZerionCapability(
            key="portfolio",
            slug="zerion-portfolio",
            name="Zerion Wallet Portfolio",
            description="Total portfolio value, distribution by chain and position type, 24h change.",
            cli_command="portfolio",
            api_path="/v1/wallets/{wallet}/portfolio",
            tags=("zerion", "onchain", "portfolio", "balances", "blockchain-data"),
        ),
        ZerionCapability(
            key="positions",
            slug="zerion-positions",
            name="Zerion Token Positions",
            description="Fungible token positions with quantity, price, value and 24h change.",
            cli_command="positions",
            api_path="/v1/wallets/{wallet}/positions/",
            tags=("zerion", "onchain", "positions", "tokens", "holdings", "blockchain-data"),
        ),
        ZerionCapability(
            key="defi_positions",
            slug="zerion-defi-positions",
            name="Zerion DeFi Positions",
            description=(
                "Protocol positions only — deposits, loans, staked, locked and rewards — "
                "grouped by DeFi protocol."
            ),
            cli_command="positions",
            api_path="/v1/wallets/{wallet}/positions/",
            tags=("zerion", "onchain", "defi", "positions", "protocol", "blockchain-data"),
        ),
        ZerionCapability(
            key="pnl",
            slug="zerion-pnl",
            name="Zerion Wallet PnL",
            description="Realized, unrealized and fee profit/loss with net invested capital (FIFO).",
            cli_command="pnl",
            api_path="/v1/wallets/{wallet}/pnl",
            tags=("zerion", "onchain", "pnl", "profit", "loss", "performance", "blockchain-data"),
        ),
        ZerionCapability(
            key="transactions",
            slug="zerion-transactions",
            name="Zerion Transaction History",
            description="Recent transactions with operation type, counterparties, fees and transfers.",
            cli_command="history",
            api_path="/v1/wallets/{wallet}/transactions/",
            tags=("zerion", "onchain", "transactions", "history", "activity", "blockchain-data"),
        ),
        ZerionCapability(
            key="token_search",
            slug="zerion-token-search",
            name="Zerion Token Search",
            description="Find fungible assets by name or symbol, with price and market data.",
            cli_command="search",
            api_path="/v1/fungibles/",
            needs_wallet=False,
            needs_query=True,
            tags=("zerion", "onchain", "token", "search", "market", "blockchain-data"),
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 64},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "currency": {"type": "string", "default": "usd"},
                },
            },
        ),
        ZerionCapability(
            key="chains",
            slug="zerion-chains",
            name="Zerion Supported Chains",
            description="Every chain Zerion indexes, with explorer and trading capability flags.",
            cli_command="chains",
            api_path="/v1/chains/",
            needs_wallet=False,
            tags=("zerion", "onchain", "chains", "networks", "blockchain-data"),
            input_schema={"type": "object", "properties": {}},
        ),
    )
}

#: Marketplace service slug -> capability. Used to route a job to its capability.
SLUG_TO_CAPABILITY: dict[str, ZerionCapability] = {c.slug: c for c in CAPABILITIES.values()}

#: The only CLI subcommands this integration will ever execute.
ALLOWED_CLI_COMMANDS: frozenset[str] = frozenset(c.cli_command for c in CAPABILITIES.values())


def capability_for(key: str) -> ZerionCapability:
    """Look up a capability by name or by marketplace slug."""
    token = str(key or "").strip().lower().replace("-", "_")
    if token in CAPABILITIES:
        return CAPABILITIES[token]
    slug = str(key or "").strip().lower()
    if slug in SLUG_TO_CAPABILITY:
        return SLUG_TO_CAPABILITY[slug]
    raise ZerionValidationError(
        f"unknown Zerion capability {str(key)[:48]!r}",
        supported=sorted(CAPABILITIES),
    )


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #
@dataclass
class ZerionRequestSpec:
    """A validated, ready-to-execute Zerion request.

    Constructing one is the validation gate: every field has already been
    checked against the patterns above, so downstream code can interpolate them
    into a URL path or pass them as subprocess arguments without further
    escaping.
    """

    capability: ZerionCapability
    wallet: str = ""
    chain: str = ""
    currency: str = "usd"
    query: str = ""
    limit: int = 20

    @classmethod
    def from_payload(cls, capability_key: str, payload: dict | None) -> "ZerionRequestSpec":
        payload = payload or {}
        capability = capability_for(capability_key)

        wallet = ""
        if capability.needs_wallet:
            wallet = normalize_wallet(
                payload.get("wallet") or payload.get("address") or payload.get("account")
            )

        query = normalize_query(payload.get("query") or payload.get("q")) if capability.needs_query else ""

        currency = str(payload.get("currency") or settings.zerion_currency).strip().lower()
        if not re.match(r"^[a-z]{3}$", currency):
            raise ZerionValidationError(f"unsupported currency {currency[:8]!r}")

        return cls(
            capability=capability,
            wallet=wallet,
            chain=normalize_chain(payload.get("chain")),
            currency=currency,
            query=query,
            limit=normalize_limit(payload.get("limit"), default=settings.zerion_history_limit),
        )

    @property
    def upstream_requests(self) -> int:
        return self.capability.upstream_requests

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.key,
            "wallet": self.wallet,
            "chain": self.chain,
            "currency": self.currency,
            "query": self.query,
            "limit": self.limit,
        }


def validate_payload(capability_key: str, payload: dict | None) -> ZerionRequestSpec:
    """Public validation entry point used before a job is priced."""
    return ZerionRequestSpec.from_payload(capability_key, payload)
