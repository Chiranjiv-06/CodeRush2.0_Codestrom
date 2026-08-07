"""On-chain intent detection for the planner.

Turns "Analyze wallet 0xABC…", "what tokens does 0xABC hold", "show its DeFi
positions" and "how much profit has this wallet made" into a provider hint plus
the parameters a blockchain-data service needs.

The bar for routing a step to a paid on-chain provider is deliberately high:
**a wallet identifier must be resolvable**, either in the step itself or carried
over from an earlier step of the same plan. Keyword enthusiasm alone is not
enough — "analyze this text" and "compute a hash" must never reach a paid data
provider, and a step that merely mentions crypto without an address has nothing
to look up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Same patterns the integration validates against; matched here in free text.
EVM_IN_TEXT = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
ENS_IN_TEXT = re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}(?:\.[a-z0-9-]{1,62})*\.eth\b", re.IGNORECASE)
# Base58, 32-44 chars. Anchored on word boundaries and filtered below, because
# the alphabet overlaps ordinary words.
SOLANA_IN_TEXT = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

CHAIN_NAMES = (
    "ethereum", "base", "arbitrum", "optimism", "polygon", "solana", "avalanche",
    "bsc", "zksync", "linea", "scroll", "blast", "gnosis", "fantom", "celo",
)

# Ordered: the first capability whose cues appear wins, so "DeFi positions"
# beats the plain "positions" reading and "profit/loss" beats "analyze".
CAPABILITY_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pnl", ("pnl", "p&l", "profit", "loss", "gains", "gain", "realized", "unrealized",
             "performance", "made money", "lost money", "roi", "return")),
    ("defi_positions", ("defi", "de-fi", "protocol", "protocols", "staked", "staking",
                        "lending", "borrowed", "liquidity", "yield", "farm", "vault",
                        "deposited", "aave", "uniswap", "lido", "compound")),
    ("transactions", ("transaction", "transactions", "txs", "tx", "history", "activity",
                      "transfers", "recent", "sent", "received", "trades")),
    ("portfolio", ("portfolio", "net worth", "worth", "balance", "balances", "value",
                   "total value", "how much")),
    ("positions", ("position", "positions", "token", "tokens", "holding", "holdings",
                   "holds", "hold", "assets", "coins", "erc20", "spl")),
    ("token_search", ("search token", "find token", "token price", "look up token",
                      "search for the token")),
    ("chains", ("supported chains", "which chains", "list chains", "networks supported")),
    ("wallet_analysis", ("analyze", "analyse", "analysis", "full read", "everything about",
                         "overview", "summarize wallet", "check wallet", "audit wallet",
                         "look at", "review")),
)

# Words that make an on-chain reading implausible even when an address-shaped
# token appears — these steps are about local compute, not a wallet lookup.
NEGATIVE_CUES = ("sha256", "hash the", "checksum", "digest of", "prime", "fibonacci",
                 "sort the", "csv", "json schema", "validate the schema")

WALLET_WORDS = ("wallet", "address", "account", "holder", "whale", "0x")


@dataclass
class OnchainIntent:
    """What an on-chain reading of one step found."""

    matched: bool = False
    capability: str = ""
    wallet: str = ""
    chain: str = ""
    limit: int | None = None
    query: str = ""
    reason: str = ""
    params: dict = field(default_factory=dict)


def _plausible_solana(token: str, text: str) -> bool:
    """Reject ordinary words that happen to be base58-legal.

    A real Solana address mixes cases and digits; an English word does not, and
    a false positive here would send a plan to a paid provider for nothing.
    """
    if not any(c.isdigit() for c in token):
        return False
    if not (any(c.isupper() for c in token) and any(c.islower() for c in token)):
        return False
    # Only trust it when the sentence is talking about a wallet at all.
    return any(word in text.lower() for word in WALLET_WORDS)


def extract_wallet(text: str) -> str:
    """First wallet identifier in ``text``, or ``""``."""
    evm = EVM_IN_TEXT.search(text or "")
    if evm:
        return evm.group(0).lower()
    ens = ENS_IN_TEXT.search(text or "")
    if ens:
        return ens.group(0).lower()
    for candidate in SOLANA_IN_TEXT.findall(text or ""):
        if _plausible_solana(candidate, text or ""):
            return candidate
    return ""


def extract_chain(text: str) -> str:
    lowered = (text or "").lower()
    for chain in CHAIN_NAMES:
        if re.search(rf"\b{re.escape(chain)}\b", lowered):
            return chain
    return ""


def extract_limit(text: str) -> int | None:
    """`last 20 transactions` -> 20."""
    match = re.search(r"\b(?:last|latest|recent|top|first)\s+(\d{1,3})\b", (text or "").lower())
    if match:
        return max(1, min(int(match.group(1)), 100))
    return None


def pick_capability(text: str) -> str:
    lowered = f" {(text or '').lower()} "
    for capability, cues in CAPABILITY_CUES:
        for cue in cues:
            if cue in lowered:
                return capability
    return ""


def detect_onchain_intent(text: str, *, carried_wallet: str = "") -> OnchainIntent:
    """Decide whether one plan step is an on-chain data request.

    ``carried_wallet`` is the address an earlier step of the same plan named, so
    "Analyze wallet 0xABC, then show its DeFi positions" resolves both steps
    against the same wallet.
    """
    raw = text or ""
    lowered = raw.lower()

    if any(cue in lowered for cue in NEGATIVE_CUES):
        return OnchainIntent(reason="step reads as local compute, not an on-chain lookup")

    wallet = extract_wallet(raw) or carried_wallet
    capability = pick_capability(raw)

    if not capability:
        return OnchainIntent(reason="no on-chain capability cue found")

    if capability == "chains":
        return OnchainIntent(matched=True, capability="chains",
                             reason="asks which chains are supported", params={})

    if capability == "token_search":
        match = re.search(r"(?:token|coin)\s+(?:called\s+|named\s+)?[\"']?([A-Za-z0-9 .\-]{2,32})",
                          raw, re.IGNORECASE)
        query = (match.group(1).strip() if match else "").strip(" .")
        if not query:
            return OnchainIntent(reason="token search without a searchable term")
        return OnchainIntent(matched=True, capability="token_search", query=query,
                             reason="token lookup", params={"query": query})

    if not wallet:
        # The decisive gate: no address, no paid lookup.
        return OnchainIntent(
            reason="on-chain wording but no wallet address, ENS name or carried-over wallet"
        )

    # "analyze" on its own is only an on-chain analysis when a wallet is present,
    # which is exactly the condition we just established.
    intent = OnchainIntent(
        matched=True,
        capability=capability,
        wallet=wallet,
        chain=extract_chain(raw),
        limit=extract_limit(raw),
        reason=f"on-chain {capability} request for {wallet}",
    )
    intent.params = {"capability": capability, "wallet": wallet}
    if intent.chain:
        intent.params["chain"] = intent.chain
    if intent.limit:
        intent.params["limit"] = intent.limit
    return intent
