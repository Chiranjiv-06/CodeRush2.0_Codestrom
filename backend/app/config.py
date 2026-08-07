"""Runtime configuration.

Every external dependency (Postgres, Redis, MinIO, Docker, Algorand) has a
degraded-but-functional local fallback so the platform boots on a bare machine
with nothing but Python installed, and transparently upgrades when the real
services are present (docker compose up).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
VAR_DIR = Path(os.getenv("M2X_VAR_DIR", REPO_ROOT / "var"))

# --------------------------------------------------------------------------- #
# Payment asset (mandated)
# --------------------------------------------------------------------------- #
# Algorand is the payment network for this exchange and ASA #10458941 is the one
# asset every payment is denominated, authorized and settled in. It is a constant
# rather than a plain default so that nothing can silently fall back to another
# asset when configuration is missing; only an administrator setting
# ALGORAND_ASSET_ID overrides it, and every payment path re-validates the result.
BLOCKCHAIN = "Algorand"
DEFAULT_ALGORAND_NETWORK = "testnet"
ASSET_ID = 10458941
ASSET_DECIMALS = 6
ASSET_UNIT_NAME = "USDC"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="M2X_", env_file=(REPO_ROOT / ".env"), extra="ignore"
    )

    # --- identity -----------------------------------------------------------
    app_name: str = "M2X Compute & Tool Exchange"
    env: str = "local"
    version: str = "1.0.0"

    # --- api ----------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000"

    # --- security -----------------------------------------------------------
    jwt_secret: str = Field(default="dev-insecure-jwt-secret-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 14
    receipt_signing_key: str = Field(default="dev-insecure-receipt-key-change-me")
    payment_shared_secret: str = Field(default="dev-insecure-payment-secret")

    # --- datastores ---------------------------------------------------------
    # Postgres when provided, else a local SQLite file (same ORM, same code).
    database_url: str = Field(default="")
    redis_url: str = Field(default="")  # empty -> in-process cache backend

    # --- object storage (MinIO / S3) ---------------------------------------
    minio_endpoint: str = ""
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "m2x-artifacts"
    minio_secure: bool = False

    # --- sandbox workers ----------------------------------------------------
    sandbox_backend: str = "auto"          # auto | docker | subprocess
    sandbox_image_python: str = "python:3.12-slim"
    sandbox_image_node: str = "node:20-alpine"
    sandbox_timeout_seconds: int = 60
    sandbox_max_memory_mb: int = 512
    sandbox_max_output_bytes: int = 2 * 1024 * 1024
    sandbox_network: str = "none"
    worker_ttl_seconds: int = 900

    # --- algorand payment asset ---------------------------------------------
    # Read from the unprefixed ALGORAND_* names the deployment docs specify, and
    # from the M2X_-prefixed forms every other setting uses.
    algorand_network: str = Field(
        default=DEFAULT_ALGORAND_NETWORK,
        validation_alias=AliasChoices("ALGORAND_NETWORK", "M2X_ALGORAND_NETWORK"),
    )
    algorand_asset_id: int = Field(
        default=ASSET_ID,
        validation_alias=AliasChoices("ALGORAND_ASSET_ID", "M2X_ALGORAND_ASSET_ID"),
    )
    algorand_asset_unit_name: str = ASSET_UNIT_NAME

    # --- x402 payments ------------------------------------------------------
    x402_version: int = 1
    x402_asset_decimals: int = ASSET_DECIMALS
    x402_facilitator_url: str = ""         # empty -> built-in local facilitator
    x402_settlement_backend: str = "auto"  # auto | algorand | ledger
    x402_escrow_timeout_seconds: int = 900
    algod_url: str = ""
    algod_token: str = ""
    algorand_dispenser_mnemonic: str = ""

    # --- bazaar discovery (GoPlausible / @x402-avm/extensions) --------------
    bazaar_enabled: bool = True
    bazaar_base_url: str = "https://bazaar.goplausible.xyz"
    bazaar_list_path: str = "/api/v1/x402/list"
    bazaar_cache_ttl_seconds: int = 300
    bazaar_timeout_seconds: float = 6.0
    bazaar_publish_enabled: bool = False

    # --- zerion onchain intelligence (external provider) ---------------------
    # Zerion is an *external* provider: the consumer still pays this exchange in
    # the mandated ASA, and the exchange then pays Zerion on Zerion's own rail
    # (x402 / USDC on Base or Solana, or a plain API key). Read from the
    # unprefixed ZERION_* names Zerion's own tooling uses, and from the
    # M2X_-prefixed forms every other setting uses.
    zerion_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("ZERION_ENABLED", "M2X_ZERION_ENABLED"),
    )
    zerion_api_base_url: str = Field(
        default="https://api.zerion.io",
        validation_alias=AliasChoices("ZERION_API_BASE_URL", "M2X_ZERION_API_BASE_URL"),
    )
    # Secret. Never returned by /v1/config, never written to a database row,
    # never logged: only its presence is ever reported.
    zerion_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ZERION_API_KEY", "M2X_ZERION_API_KEY"),
    )
    zerion_use_x402: bool = Field(
        default=False,
        validation_alias=AliasChoices("ZERION_USE_X402", "ZERION_X402", "M2X_ZERION_USE_X402"),
    )
    # Secrets — same handling as zerion_api_key.
    zerion_evm_private_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ZERION_EVM_PRIVATE_KEY", "EVM_PRIVATE_KEY", "M2X_ZERION_EVM_PRIVATE_KEY"
        ),
    )
    zerion_solana_private_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ZERION_SOLANA_PRIVATE_KEY", "SOLANA_PRIVATE_KEY", "M2X_ZERION_SOLANA_PRIVATE_KEY"
        ),
    )
    zerion_x402_prefer_solana: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ZERION_X402_PREFER_SOLANA", "M2X_ZERION_X402_PREFER_SOLANA"
        ),
    )
    zerion_transport: str = Field(              # auto | api | cli
        default="auto",
        validation_alias=AliasChoices("ZERION_TRANSPORT", "M2X_ZERION_TRANSPORT"),
    )
    zerion_cli_command: str = Field(
        default="zerion",
        validation_alias=AliasChoices("ZERION_CLI_COMMAND", "M2X_ZERION_CLI_COMMAND"),
    )
    zerion_timeout_seconds: float = Field(
        default=15.0,
        validation_alias=AliasChoices("ZERION_TIMEOUT_SECONDS", "M2X_ZERION_TIMEOUT_SECONDS"),
    )
    zerion_max_retries: int = Field(
        default=2,
        validation_alias=AliasChoices("ZERION_MAX_RETRIES", "M2X_ZERION_MAX_RETRIES"),
    )
    zerion_default_chain: str = Field(
        default="ethereum",
        validation_alias=AliasChoices("ZERION_DEFAULT_CHAIN", "M2X_ZERION_DEFAULT_CHAIN"),
    )
    zerion_allowed_chains: str = Field(        # empty -> every chain Zerion supports
        default="",
        validation_alias=AliasChoices("ZERION_ALLOWED_CHAINS", "M2X_ZERION_ALLOWED_CHAINS"),
    )
    zerion_currency: str = Field(
        default="usd",
        validation_alias=AliasChoices("ZERION_CURRENCY", "M2X_ZERION_CURRENCY"),
    )
    zerion_history_limit: int = Field(
        default=20,
        validation_alias=AliasChoices("ZERION_HISTORY_LIMIT", "M2X_ZERION_HISTORY_LIMIT"),
    )
    # Demo mode serves deterministic, clearly-labelled fixture data when no
    # Zerion credential is configured, so the exchange still demonstrates the
    # full discover -> pay -> call -> verify path on a bare machine. It never
    # claims a payment settled that did not.
    zerion_demo_mode: bool = Field(
        default=True,
        validation_alias=AliasChoices("ZERION_DEMO_MODE", "M2X_ZERION_DEMO_MODE"),
    )

    # --- zerion quotas & cost control ---------------------------------------
    zerion_max_requests_per_job: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "ZERION_MAX_REQUESTS_PER_JOB", "MAX_ZERION_REQUESTS_PER_JOB",
            "M2X_ZERION_MAX_REQUESTS_PER_JOB",
        ),
    )
    zerion_max_requests_per_session: int = Field(
        default=25,
        validation_alias=AliasChoices(
            "ZERION_MAX_REQUESTS_PER_SESSION", "M2X_ZERION_MAX_REQUESTS_PER_SESSION"
        ),
    )
    zerion_max_spend_micros: int = Field(
        default=2_000_000,
        validation_alias=AliasChoices("ZERION_MAX_SPEND_MICROS", "M2X_ZERION_MAX_SPEND_MICROS"),
    )
    zerion_quota_window_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices(
            "ZERION_QUOTA_WINDOW_SECONDS", "M2X_ZERION_QUOTA_WINDOW_SECONDS"
        ),
    )
    # What one Zerion request costs *us* on Zerion's rail (0.01 USDC = 10000
    # micro-units), and what the exchange charges the consumer for it.
    zerion_cost_micros: int = Field(
        default=10_000,
        validation_alias=AliasChoices("ZERION_COST_MICROS", "M2X_ZERION_COST_MICROS"),
    )
    zerion_price_micros: int = Field(
        default=15_000,
        validation_alias=AliasChoices("ZERION_PRICE_MICROS", "M2X_ZERION_PRICE_MICROS"),
    )
    zerion_result_ttl_seconds: int = Field(
        default=60 * 60 * 24,
        validation_alias=AliasChoices("ZERION_RESULT_TTL_SECONDS", "M2X_ZERION_RESULT_TTL_SECONDS"),
    )

    # --- agent --------------------------------------------------------------
    agent_max_steps: int = 12
    agent_default_budget_micros: int = 5_000_000  # 5.00 units of the payment asset

    # --- economics ----------------------------------------------------------
    platform_fee_bps: int = 250            # 2.5%
    signup_grant_micros: int = 25_000_000  # 25.00 units of testnet credit
    dispute_window_seconds: int = 86_400

    # --- scheduler ----------------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_tick_seconds: float = 2.0
    artifact_ttl_seconds: int = 60 * 60 * 24 * 7

    # --- observability ------------------------------------------------------
    metrics_enabled: bool = True
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("algorand_asset_id")
    @classmethod
    def _positive_asset_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"ALGORAND_ASSET_ID must be a positive ASA id, got {v}")
        return v

    @field_validator("algorand_network")
    @classmethod
    def _known_network(cls, v: str) -> str:
        network = v.strip().lower().replace("algorand-", "")
        if network not in ("testnet", "mainnet", "betanet", "localnet"):
            raise ValueError(f"unsupported ALGORAND_NETWORK {v!r}")
        return network

    # --- derived payment identity ------------------------------------------
    # These are properties, not fields: the asset the exchange quotes, escrows
    # and settles in is always the configured ASA, and cannot drift apart from
    # it through a stray environment variable.
    @property
    def blockchain(self) -> str:
        return BLOCKCHAIN

    @property
    def network_label(self) -> str:
        """Human-facing network name, e.g. ``TestNet``."""
        return {"testnet": "TestNet", "mainnet": "MainNet",
                "betanet": "BetaNet", "localnet": "LocalNet"}[self.algorand_network]

    @property
    def x402_network(self) -> str:
        """x402 network identifier, e.g. ``algorand-testnet``."""
        return f"algorand-{self.algorand_network}"

    @property
    def bazaar_network(self) -> str:
        return self.x402_network

    @property
    def x402_asset(self) -> str:
        """The x402 ``asset`` field: the ASA id as a string."""
        return str(self.algorand_asset_id)

    @property
    def asset_display(self) -> str:
        return f"Algorand ASA #{self.algorand_asset_id}"

    # --- derived zerion identity -------------------------------------------
    # Properties, not fields: which credential a deployment actually holds is a
    # fact about the environment, and nothing may claim a payment rail it has no
    # key for. Only booleans are ever derived from a secret, never the secret.
    @property
    def zerion_x402_configured(self) -> bool:
        return bool(self.zerion_evm_private_key or self.zerion_solana_private_key)

    @property
    def zerion_api_key_configured(self) -> bool:
        return bool(self.zerion_api_key)

    @property
    def zerion_x402_chain(self) -> str:
        """Which chain a Zerion x402 request would settle on, given the keys held."""
        if self.zerion_x402_prefer_solana and self.zerion_solana_private_key:
            return "solana"
        if self.zerion_evm_private_key:
            return "base"
        return "solana" if self.zerion_solana_private_key else ""

    @property
    def zerion_allowed_chain_list(self) -> list[str]:
        return [c.strip().lower() for c in self.zerion_allowed_chains.split(",") if c.strip()]

    @property
    def zerion_api_url(self) -> str:
        return self.zerion_api_base_url.rstrip("/")

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        VAR_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(VAR_DIR / 'm2x.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def artifact_dir(self) -> Path:
        p = VAR_DIR / "artifacts"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def workspace_dir(self) -> Path:
        p = VAR_DIR / "workspaces"
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
