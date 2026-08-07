"""Deterministic demo fixtures.

When no Zerion credential is configured, the exchange still needs to demonstrate
the whole path — discover, quote, quota, pay, call, normalize, hash, receipt —
on a machine with nothing installed. Demo mode supplies the *provider response*
and nothing else: the payment adapter reports ``simulated`` rather than settled,
the envelope is stamped ``source: zerion_demo``, and every surface that shows the
result says so.

Fixtures are emitted in Zerion's documented JSON:API shape so they travel the
same normalization code path as a live response — the demo exercises the real
parser, not a shortcut around it.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from .client import ZerionRawResult
from .models import ZerionRequestSpec

DEMO_NOTICE = (
    "Demo fixture: no Zerion credential is configured, so this data is generated "
    "locally and is not real on-chain data."
)

_CHAINS = ("ethereum", "base", "arbitrum", "optimism", "polygon", "solana")
_TOKENS = (
    ("Ethereum", "ETH", 3120.44),
    ("USD Coin", "USDC", 1.0),
    ("Aave", "AAVE", 92.17),
    ("Lido Staked ETH", "stETH", 3104.88),
    ("Uniswap", "UNI", 7.42),
    ("Chainlink", "LINK", 14.05),
)
_PROTOCOLS = ("aave-v3", "uniswap-v3", "lido", "compound-v3")
_OPERATIONS = ("trade", "send", "receive", "deposit", "withdraw", "execute")


def _seed(wallet: str, salt: str = "") -> int:
    return int(hashlib.sha256(f"{wallet}|{salt}".encode()).hexdigest()[:12], 16)


def _spread(seed: int, index: int, low: float, high: float) -> float:
    """Deterministic pseudo-random value in [low, high) — same wallet, same numbers."""
    step = (seed >> (index % 24)) ^ (seed * (index + 7))
    return low + ((step % 100_000) / 100_000.0) * (high - low)


def _resource(kind: str, ident: str, attributes: dict, relationships: dict | None = None) -> dict:
    body = {"type": kind, "id": ident, "attributes": attributes}
    if relationships:
        body["relationships"] = relationships
    return body


def _chain_rel(chain: str) -> dict:
    return {"chain": {"data": {"type": "chains", "id": chain}}}


def _chain_split(wallet: str) -> dict[str, float]:
    seed = _seed(wallet, "portfolio")
    return {
        chain: round(_spread(seed, i, 120.0, 42_000.0), 2)
        for i, chain in enumerate(_CHAINS[: 3 + seed % 3])
    }


def portfolio_total(wallet: str) -> float:
    """The one total every fixture for this wallet is derived from.

    Positions and DeFi positions are allocated out of this number rather than
    generated independently, so the demo's figures tie out the way a real
    wallet's would — a demo that contradicts itself is worse than no demo.
    """
    return round(sum(_chain_split(wallet).values()), 2)


def portfolio_fixture(spec: ZerionRequestSpec) -> dict:
    by_chain = _chain_split(spec.wallet)
    total = portfolio_total(spec.wallet)
    seed = _seed(spec.wallet, "portfolio")
    return {
        "links": {"self": f"/v1/wallets/{spec.wallet}/portfolio"},
        "data": _resource("portfolio", spec.wallet, {
            "positions_distribution_by_type": {
                "wallet": round(total * 0.63, 2),
                "deposited": round(total * 0.21, 2),
                "staked": round(total * 0.11, 2),
                "borrowed": round(total * 0.03, 2),
                "locked": round(total * 0.02, 2),
            },
            "positions_distribution_by_chain": by_chain,
            "total": {"positions": total},
            "changes": {
                "absolute_1d": round(_spread(seed, 9, -2400.0, 3100.0), 2),
                "percent_1d": round(_spread(seed, 4, -8.5, 9.5), 2),
            },
        }),
        "_demo": DEMO_NOTICE,
    }


def positions_fixture(spec: ZerionRequestSpec, *, defi: bool = False) -> dict:
    seed = _seed(spec.wallet, "defi" if defi else "positions")
    # The wallet holds `wallet` + `deposited` + `staked` + `locked` of its total
    # in simple positions, and the DeFi share in protocol positions — the same
    # split portfolio_fixture advertises.
    total = portfolio_total(spec.wallet)
    share = total * (0.63 if not defi else 0.34)
    weights = [_spread(seed, i + 11, 0.05, 1.0) for i in range(len(_TOKENS))]
    weight_sum = sum(weights) or 1.0

    rows = []
    for i, (name, symbol, price) in enumerate(_TOKENS):
        value = round(share * weights[i] / weight_sum, 2)
        quantity = round(value / price, 6) if price else 0.0
        chain = _CHAINS[(seed + i) % len(_CHAINS)]
        rows.append(_resource(
            "positions", f"{spec.wallet}-{symbol}-{i}",
            {
                "name": name if not defi else f"{_PROTOCOLS[i % len(_PROTOCOLS)]} {symbol}",
                "protocol": _PROTOCOLS[i % len(_PROTOCOLS)] if defi else None,
                "position_type": ("deposit" if defi and i % 3 == 0
                                  else "staked" if defi and i % 3 == 1
                                  else "loan" if defi else "wallet"),
                "quantity": {"float": quantity, "decimals": 18, "numeric": str(quantity)},
                "price": price,
                "value": value,
                "changes": {"percent_1d": round(_spread(seed, i + 3, -12.0, 12.0), 2)},
                "fungible_info": {"name": name, "symbol": symbol},
                "flags": {"displayable": True, "is_trash": False},
            },
            _chain_rel(chain),
        ))
    return {"links": {"self": f"/v1/wallets/{spec.wallet}/positions/"},
            "data": rows, "_demo": DEMO_NOTICE}


def pnl_fixture(spec: ZerionRequestSpec) -> dict:
    seed = _seed(spec.wallet, "pnl")
    total = portfolio_total(spec.wallet)
    # Unrealized gain is current value minus cost basis by definition, so it is
    # derived from the portfolio total rather than drawn independently.
    invested = round(total * _spread(seed, 8, 0.45, 1.55), 2)
    unrealized = round(total - invested, 2)
    realized = round(_spread(seed, 1, -0.35, 0.9) * invested, 2)
    return {
        "data": _resource("pnl", spec.wallet, {
            "total_gain": round(realized + unrealized, 2),
            "realized_gain": realized,
            "unrealized_gain": unrealized,
            "total_fee": round(_spread(seed, 2, 0.002, 0.04) * invested, 2),
            "total_invested": invested,
            "net_invested": round(invested * 0.78, 2),
            "realized_cost_basis": round(invested * 0.42, 2),
            "relative_total_gain_percentage": round(
                ((realized + unrealized) / invested) * 100 if invested else 0.0, 2
            ),
        }),
        "_demo": DEMO_NOTICE,
    }


def transactions_fixture(spec: ZerionRequestSpec) -> dict:
    seed = _seed(spec.wallet, "history")
    rows = []
    now_ms = int(time.time() * 1000)
    for i in range(min(spec.limit, 25)):
        chain = _CHAINS[(seed + i) % len(_CHAINS)]
        digest = hashlib.sha256(f"{spec.wallet}:{i}".encode()).hexdigest()
        minted = now_ms - (i + 1) * 3_600_000
        rows.append(_resource(
            "transactions", f"{chain}-0x{digest[:40]}",
            {
                "operation_type": _OPERATIONS[(seed + i) % len(_OPERATIONS)],
                "hash": f"0x{digest[:64]}",
                "mined_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minted / 1000)),
                "status": "confirmed",
                "sent_from": spec.wallet,
                "sent_to": f"0x{digest[24:64]}",
                "fee": {"value": round(_spread(seed, i, 0.4, 24.0), 4)},
                "transfers": [{"direction": "out"}, {"direction": "in"}],
            },
            _chain_rel(chain),
        ))
    return {"links": {"self": f"/v1/wallets/{spec.wallet}/transactions/"},
            "data": rows, "_demo": DEMO_NOTICE}


def fungibles_fixture(spec: ZerionRequestSpec) -> dict:
    seed = _seed(spec.query, "search")
    rows = []
    for i, (name, symbol, price) in enumerate(_TOKENS):
        if spec.query.lower() not in f"{name} {symbol}".lower() and i > 2:
            continue
        rows.append(_resource("fungibles", f"demo-{symbol.lower()}", {
            "name": name,
            "symbol": symbol,
            "market_data": {
                "price": price,
                "market_cap": round(_spread(seed, i, 1e8, 4e11), 2),
                "changes": {"percent_1d": round(_spread(seed, i + 2, -9.0, 9.0), 2)},
            },
        }))
    return {"data": rows[: spec.limit], "_demo": DEMO_NOTICE}


def chains_fixture(_spec: ZerionRequestSpec) -> dict:
    return {
        "links": {"self": "/v1/chains/"},
        "data": [
            _resource("chains", chain, {
                "name": chain,
                "flags": {"supports_trading": chain != "solana",
                          "supports_sending": True, "supports_bridge": True},
            })
            for chain in _CHAINS
        ],
        "_demo": DEMO_NOTICE,
    }


class ZerionDemoClient:
    """Serves fixtures through the same interface as the live transports."""

    source = "zerion_demo"

    def execute(self, spec: ZerionRequestSpec) -> ZerionRawResult:
        started = time.perf_counter()
        key = spec.capability.key
        if key == "wallet_analysis":
            payloads: dict[str, Any] = {
                "portfolio": portfolio_fixture(spec),
                "positions": positions_fixture(spec),
                "transactions": transactions_fixture(spec),
                "pnl": pnl_fixture(spec),
            }
        else:
            payloads = {key: {
                "portfolio": lambda: portfolio_fixture(spec),
                "positions": lambda: positions_fixture(spec),
                "defi_positions": lambda: positions_fixture(spec, defi=True),
                "pnl": lambda: pnl_fixture(spec),
                "transactions": lambda: transactions_fixture(spec),
                "token_search": lambda: fungibles_fixture(spec),
                "chains": lambda: chains_fixture(spec),
            }[key]()}

        return ZerionRawResult(
            source=self.source,
            payloads=payloads,
            http_status=200,
            latency_ms=int((time.perf_counter() - started) * 1000),
            upstream_requests=spec.upstream_requests,
            warnings=[DEMO_NOTICE],
        )


demo_client = ZerionDemoClient()
