"""Zerion Onchain Intelligence integration.

Covers configuration, address validation, discovery, capability routing, both
transports, normalization, quotas, budgets, payment failure, provider outage,
rate limiting, integrity hashing, receipts, agent intent routing, the dashboard
API and the mocked end-to-end flow.

No test here spends real money or touches the network: the API client is driven
through a stubbed ``httpx``, the CLI client through a stubbed ``subprocess.run``,
and the x402 rail through the adapter's own evidence contract. The one test that
would use a real credential is skipped unless the environment supplies it.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from app.agent.onchain import detect_onchain_intent, extract_wallet
from app.agent.planner import decompose
from app.config import settings
from app.integrations.zerion import (
    CAPABILITIES,
    PROVIDER_ID,
    ZerionQuotaError,
    ZerionValidationError,
    capability_for,
    normalize_wallet,
)
from app.integrations.zerion import client as zerion_client_module
from app.integrations.zerion import quota as quota_service
from app.integrations.zerion import service as zerion_service
from app.integrations.zerion.cli import ZerionCliClient
from app.integrations.zerion.client import ZerionApiClient
from app.integrations.zerion.demo import demo_client
from app.integrations.zerion.errors import (
    ZerionAuthError,
    ZerionRateLimitError,
    ZerionTimeoutError,
    ZerionUnavailableError,
    sanitize,
)
from app.integrations.zerion.models import ZerionRequestSpec
from app.integrations.zerion.normalizer import normalize, verify_envelope
from app.integrations.zerion.payment import ZerionX402PaymentAdapter, active_rail
from app.payments.rails import PaymentOutcome, PaymentRail

VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


def spec_for(capability: str, **payload) -> ZerionRequestSpec:
    return ZerionRequestSpec.from_payload(capability, payload)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_configuration_exposes_zerion_without_leaking_secrets(client, monkeypatch):
    # Configure every secret, then assert none of their *values* can be served.
    monkeypatch.setattr(settings, "zerion_api_key", "zk_supersecret_value")
    monkeypatch.setattr(settings, "zerion_evm_private_key", "0x" + "ef" * 32)
    monkeypatch.setattr(settings, "zerion_solana_private_key", "SoLaNaSecretValue")

    config = client.get("/v1/config").json()
    zerion = config["zerion"]
    assert zerion["provider"] == "zerion"
    assert zerion["enabled"] is True
    assert zerion["api_key_configured"] is True
    assert zerion["x402_keys_configured"] is True

    serialized = json.dumps(config)
    for secret in ("zk_supersecret_value", "0x" + "ef" * 32, "SoLaNaSecretValue"):
        assert secret not in serialized
    # Presence flags only; no field carries a value.
    for key in ("api_key", "evm_private_key", "solana_private_key", "zerion_api_key"):
        assert zerion.get(key) is None


def test_health_reports_the_zerion_provider(client):
    body = client.get("/health").json()
    zerion = body["components"]["zerion"]
    assert zerion["transport"] in ("cli", "api", "demo", "unavailable")
    assert isinstance(zerion["operational"], bool)


def test_rail_selection_follows_the_credentials_actually_held(monkeypatch):
    monkeypatch.setattr(settings, "zerion_enabled", True)
    monkeypatch.setattr(settings, "zerion_use_x402", False)
    monkeypatch.setattr(settings, "zerion_api_key", "")
    monkeypatch.setattr(settings, "zerion_evm_private_key", "")
    monkeypatch.setattr(settings, "zerion_solana_private_key", "")
    assert active_rail() is PaymentRail.NONE

    monkeypatch.setattr(settings, "zerion_api_key", "zk_test_key")
    assert active_rail() is PaymentRail.API_KEY

    # x402 switched on but no key is *not* a usable x402 rail.
    monkeypatch.setattr(settings, "zerion_use_x402", True)
    assert active_rail() is PaymentRail.API_KEY

    monkeypatch.setattr(settings, "zerion_evm_private_key", "0x" + "ab" * 32)
    assert active_rail() is PaymentRail.ZERION_X402
    assert settings.zerion_x402_chain == "base"

    monkeypatch.setattr(settings, "zerion_solana_private_key", "So1anaKey")
    monkeypatch.setattr(settings, "zerion_x402_prefer_solana", True)
    assert settings.zerion_x402_chain == "solana"


# --------------------------------------------------------------------------- #
# Wallet & input validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    (VITALIK, VITALIK.lower()),
    ("0x" + "A" * 40, "0x" + "a" * 40),
    ("vitalik.eth", "vitalik.eth"),
    ("VITALIK.ETH", "vitalik.eth"),
    ("7cVfgArCheMR6Cs4t6vz5rfnqd56vZq4ndaBrY5xkxXy", "7cVfgArCheMR6Cs4t6vz5rfnqd56vZq4ndaBrY5xkxXy"),
])
def test_valid_wallets_are_accepted_and_canonicalized(value, expected):
    assert normalize_wallet(value) == expected


@pytest.mark.parametrize("value", [
    "", "   ", "0x123", "0x" + "z" * 40, "not-an-address", "vitalik.com",
    "0x" + "a" * 41, "; rm -rf /", "0xabc$(whoami)", "../../etc/passwd",
    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 && curl evil.sh",
])
def test_invalid_wallets_are_refused(value):
    with pytest.raises(ZerionValidationError):
        normalize_wallet(value)


def test_chain_allowlist_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "zerion_allowed_chains", "base,ethereum")
    assert spec_for("portfolio", wallet=VITALIK, chain="base").chain == "base"
    with pytest.raises(ZerionValidationError):
        spec_for("portfolio", wallet=VITALIK, chain="polygon")


def test_unknown_capability_is_refused():
    with pytest.raises(ZerionValidationError):
        capability_for("drain_wallet")


def test_capability_lookup_accepts_key_or_service_slug():
    assert capability_for("pnl").key == "pnl"
    assert capability_for("zerion-defi-positions").key == "defi_positions"


def test_error_messages_never_echo_credentials():
    dirty = "failed with key zk_livesecret123456 and 0x" + "c" * 64
    clean = sanitize(dirty)
    assert "zk_livesecret123456" not in clean
    assert "0x" + "c" * 64 not in clean
    assert "[redacted]" in clean


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def test_zerion_capabilities_are_in_the_service_catalog(client):
    services = client.get("/v1/services", params={"limit": 200}).json()
    slugs = {s["slug"] for s in services}
    for capability in CAPABILITIES.values():
        assert capability.slug in slugs, f"{capability.slug} missing from the catalog"

    portfolio = next(s for s in services if s["slug"] == "zerion-portfolio")
    assert portfolio["category"] == "blockchain-data"
    assert portfolio["max_price_micros"] > 0
    assert portfolio["input_schema"]["required"] == ["wallet"]


def test_zerion_is_discoverable_through_the_bazaar_index(client):
    listings = client.get("/v1/bazaar/listings", params={"q": "wallet onchain zerion"}).json()
    zerion = [i for i in listings["items"] if (i.get("external") or {}).get("provider") == "zerion"]
    assert zerion, "no Zerion listing in the discovery index"

    item = zerion[0]
    assert item["payable"] is True                      # payable in the mandated ASA
    external = item["external"]
    assert external["consumer_payment_rail"] == "m2x_algorand"
    assert external["provider_payment_rail"] in ("zerion_x402", "api_key", "none")
    assert external["quota"]["max_requests_per_job"] >= 1
    assert external["input_schema"] and external["output_schema"]
    assert "supported_chains" in external


def test_capabilities_endpoint_lists_price_rail_and_service_id(client):
    body = client.get("/v1/zerion/capabilities").json()
    assert body["count"] == len(CAPABILITIES)
    by_key = {i["capability"]: i for i in body["items"]}
    assert set(by_key) == set(CAPABILITIES)
    assert all(i["service_id"] for i in body["items"])
    assert by_key["pnl"]["price_micros"] > 0
    # wallet_analysis fans out to four upstream calls, so it costs more.
    assert by_key["wallet_analysis"]["price_micros"] > by_key["portfolio"]["price_micros"]


def test_status_endpoint_reports_mode_and_no_secrets(client):
    body = client.get("/v1/zerion/status").json()
    assert body["provider"] == PROVIDER_ID
    assert body["registered"] is True
    assert body["transport"] in ("cli", "api", "demo", "unavailable")
    assert "zk_" not in json.dumps(body)


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"data": []}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeHttpxClient:
    """Stands in for ``httpx.Client``; records every request it is given.

    ``responses`` is held by reference, not copied: the client is rebuilt for
    every request the code under test makes, and the queue has to advance across
    those rebuilds for a retry or a fan-out to be exercised at all.
    """

    def __init__(self, responses, recorder):
        self._responses = responses
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None):
        self._recorder.append({"url": url, "params": params or {}, "headers": headers or {}})
        item = self._responses.pop(0) if self._responses else _FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


def _patch_httpx(monkeypatch, responses, recorder):
    import sys

    import httpx

    queue = list(responses)

    class _Module:
        Client = staticmethod(lambda **kw: _FakeHttpxClient(queue, recorder))
        TimeoutException = httpx.TimeoutException
        HTTPError = httpx.HTTPError

    monkeypatch.setattr(zerion_client_module, "httpx", _Module, raising=False)
    monkeypatch.setitem(sys.modules, "httpx", _Module)
    return recorder


@pytest.fixture()
def api_key_mode(monkeypatch):
    monkeypatch.setattr(settings, "zerion_enabled", True)
    monkeypatch.setattr(settings, "zerion_api_key", "zk_unit_test_key")
    monkeypatch.setattr(settings, "zerion_use_x402", False)
    monkeypatch.setattr(settings, "zerion_transport", "api")
    return settings


def test_api_client_uses_basic_auth_and_documented_paths(monkeypatch, api_key_mode):
    calls: list[dict] = []
    _patch_httpx(monkeypatch, [_FakeResponse(200, {"data": {"attributes": {}}})], calls)

    ZerionApiClient().portfolio(spec_for("portfolio", wallet=VITALIK))

    assert len(calls) == 1
    call = calls[0]
    assert call["url"].endswith(f"/v1/wallets/{VITALIK.lower()}/portfolio")
    assert call["headers"]["Authorization"].startswith("Basic ")
    # `key:` base64-encoded, exactly as Zerion documents.
    import base64
    decoded = base64.b64decode(call["headers"]["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "zk_unit_test_key:"
    assert call["params"]["currency"] == "usd"


def test_api_client_sends_defi_filter_for_defi_positions(monkeypatch, api_key_mode):
    calls: list[dict] = []
    _patch_httpx(monkeypatch, [_FakeResponse(200, {"data": []})], calls)

    ZerionApiClient().positions(spec_for("defi_positions", wallet=VITALIK), defi_only=True)
    assert calls[0]["params"]["filter[positions]"] == "only_complex"

    calls.clear()
    _patch_httpx(monkeypatch, [_FakeResponse(200, {"data": []})], calls)
    ZerionApiClient().positions(spec_for("positions", wallet=VITALIK))
    assert calls[0]["params"]["filter[positions]"] == "only_simple"


def test_api_client_retries_a_throttle_then_succeeds(monkeypatch, api_key_mode):
    monkeypatch.setattr(settings, "zerion_max_retries", 2)
    calls: list[dict] = []
    _patch_httpx(
        monkeypatch,
        [_FakeResponse(429, headers={"RateLimit-Org-Second-Reset": "1"}),
         _FakeResponse(200, {"data": {"attributes": {"total": {"positions": 10}}}})],
        calls,
    )
    body = ZerionApiClient().portfolio(spec_for("portfolio", wallet=VITALIK))
    assert len(calls) == 2
    assert body["data"]["attributes"]["total"]["positions"] == 10


def test_api_client_surfaces_a_persistent_rate_limit(monkeypatch, api_key_mode):
    monkeypatch.setattr(settings, "zerion_max_retries", 1)
    _patch_httpx(monkeypatch, [_FakeResponse(429), _FakeResponse(429)], [])
    with pytest.raises(ZerionRateLimitError) as excinfo:
        ZerionApiClient().portfolio(spec_for("portfolio", wallet=VITALIK))
    assert excinfo.value.retryable is True


def test_api_client_reports_auth_failure_without_echoing_the_body(monkeypatch, api_key_mode):
    _patch_httpx(monkeypatch, [_FakeResponse(401, {"error": "bad key zk_unit_test_key"})], [])
    with pytest.raises(ZerionAuthError) as excinfo:
        ZerionApiClient().pnl(spec_for("pnl", wallet=VITALIK))
    assert "zk_unit_test_key" not in excinfo.value.detail


def test_api_client_maps_timeouts_and_outages(monkeypatch, api_key_mode):
    import httpx

    monkeypatch.setattr(settings, "zerion_max_retries", 0)
    _patch_httpx(monkeypatch, [httpx.TimeoutException("too slow")], [])
    with pytest.raises(ZerionTimeoutError):
        ZerionApiClient().portfolio(spec_for("portfolio", wallet=VITALIK))

    _patch_httpx(monkeypatch, [_FakeResponse(503)], [])
    with pytest.raises(ZerionUnavailableError):
        ZerionApiClient().portfolio(spec_for("portfolio", wallet=VITALIK))


def test_analyze_fans_out_and_survives_one_failing_leg(monkeypatch, api_key_mode):
    monkeypatch.setattr(settings, "zerion_max_retries", 0)
    calls: list[dict] = []
    _patch_httpx(
        monkeypatch,
        [_FakeResponse(200, {"data": {"attributes": {"total": {"positions": 5}}}}),  # portfolio
         _FakeResponse(200, {"data": []}),                                            # positions
         _FakeResponse(500),                                                          # transactions
         _FakeResponse(200, {"data": {"attributes": {"total_gain": 1.5}}})],          # pnl
        calls,
    )
    result = ZerionApiClient().execute(spec_for("wallet_analysis", wallet=VITALIK))
    assert set(result.payloads) == {"portfolio", "positions", "pnl"}
    assert result.upstream_requests == 3
    assert any("transactions" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# CLI client
# --------------------------------------------------------------------------- #
class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _patch_subprocess(monkeypatch, result, recorder):
    def fake_run(args, **kwargs):
        recorder.append({"args": args, "kwargs": kwargs})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_cli_builds_a_safe_argument_vector(monkeypatch):
    calls: list[dict] = []
    _patch_subprocess(monkeypatch, _FakeCompleted(0, json.dumps({"data": []})), calls)
    cli = ZerionCliClient(command_path="/usr/local/bin/zerion")

    cli.execute(spec_for("transactions", wallet=VITALIK, chain="base", limit=7), use_x402=True)

    call = calls[0]
    assert call["args"][0] == "/usr/local/bin/zerion"
    assert call["args"][1] == "history"          # the documented subcommand for history
    assert call["args"][2] == VITALIK.lower()
    assert "--json" in call["args"] and "--x402" in call["args"]
    assert "--limit" in call["args"] and "7" in call["args"]
    assert "--chain" in call["args"] and "base" in call["args"]
    # Never a shell, never a string command, never an inherited stdin.
    assert call["kwargs"]["shell"] is False
    assert isinstance(call["args"], list)
    assert call["kwargs"]["timeout"] > 0
    assert call["kwargs"]["stdin"] == subprocess.DEVNULL


def test_cli_never_passes_a_shell_metacharacter_through(monkeypatch):
    calls: list[dict] = []
    _patch_subprocess(monkeypatch, _FakeCompleted(0, "{}"), calls)
    cli = ZerionCliClient(command_path="/usr/local/bin/zerion")

    # The injection attempt is rejected at validation; no process ever starts.
    with pytest.raises(ZerionValidationError):
        cli.execute(spec_for("portfolio", wallet=f"{VITALIK}; cat /etc/passwd"), use_x402=False)
    assert calls == []


def test_cli_x402_mode_withholds_the_api_key_from_the_child(monkeypatch):
    monkeypatch.setattr(settings, "zerion_api_key", "zk_should_not_be_used")
    monkeypatch.setattr(settings, "zerion_evm_private_key", "0x" + "ab" * 32)
    monkeypatch.setattr(settings, "zerion_use_x402", True)
    calls: list[dict] = []
    _patch_subprocess(monkeypatch, _FakeCompleted(0, json.dumps({"data": []})), calls)

    ZerionCliClient(command_path="/usr/local/bin/zerion").execute(
        spec_for("portfolio", wallet=VITALIK), use_x402=True
    )
    env = calls[0]["kwargs"]["env"]
    assert "ZERION_API_KEY" not in env          # no silent fallback to subscription billing
    assert env["ZERION_X402"] == "true"
    assert env["EVM_PRIVATE_KEY"] == "0x" + "ab" * 32
    # The child gets a scrubbed environment, not the whole parent one.
    assert "PYTHONPATH" not in env or os.environ.get("PYTHONPATH") is None


def test_cli_parses_json_stdout_and_x402_evidence(monkeypatch):
    payload = {"data": [{"attributes": {"name": "ETH"}}],
               "x402": {"transaction": "0xdeadbeef", "network": "base"}}
    _patch_subprocess(monkeypatch, _FakeCompleted(0, "warming up\n" + json.dumps(payload)), [])
    result = ZerionCliClient(command_path="/bin/zerion").execute(
        spec_for("positions", wallet=VITALIK), use_x402=True
    )
    assert result.source == "zerion_cli"
    assert result.payment_evidence["transaction"] == "0xdeadbeef"
    assert result.payment_evidence["network"] == "base"


def test_cli_maps_structured_stderr_errors(monkeypatch):
    _patch_subprocess(
        monkeypatch,
        _FakeCompleted(1, "", json.dumps({"code": "rate_limited", "message": "slow down"})),
        [],
    )
    with pytest.raises(ZerionRateLimitError):
        ZerionCliClient(command_path="/bin/zerion").execute(
            spec_for("pnl", wallet=VITALIK), use_x402=False
        )


def test_cli_enforces_a_timeout(monkeypatch):
    _patch_subprocess(monkeypatch, subprocess.TimeoutExpired(cmd="zerion", timeout=1), [])
    with pytest.raises(ZerionTimeoutError):
        ZerionCliClient(command_path="/bin/zerion").execute(
            spec_for("portfolio", wallet=VITALIK), use_x402=False
        )


def test_cli_absence_is_reported_not_guessed():
    cli = ZerionCliClient(command_path=None)
    if cli.available():          # a machine that really has the CLI installed
        pytest.skip("the Zerion CLI is installed on this host")
    with pytest.raises(ZerionUnavailableError):
        cli.execute(spec_for("portfolio", wallet=VITALIK), use_x402=False)


# --------------------------------------------------------------------------- #
# Normalization & integrity
# --------------------------------------------------------------------------- #
def test_normalizer_reads_the_json_api_shape():
    raw = {"portfolio": {"data": {"type": "portfolio", "id": VITALIK, "attributes": {
        "total": {"positions": 1234.56},
        "positions_distribution_by_chain": {"ethereum": 1000.0, "base": 234.56},
        "positions_distribution_by_type": {"wallet": 1234.56},
        "changes": {"absolute_1d": 12.3, "percent_1d": 1.02},
    }}}}
    envelope = normalize(spec_for("portfolio", wallet=VITALIK), raw, source="zerion_api")

    assert envelope["provider"] == "zerion"
    assert envelope["wallet"] == VITALIK.lower()
    assert envelope["request_type"] == "portfolio"
    assert envelope["source"] == "zerion_api"
    assert envelope["data"]["total_value"] == 1234.56
    assert envelope["data"]["changes"]["percent_1d"] == 1.02
    assert "1,234.56" in envelope["data"]["summary"]


def test_normalizer_reads_a_flat_cli_shape_to_the_same_envelope():
    cli_raw = {"positions": {"data": [
        {"name": "Ether", "symbol": "ETH", "quantity": {"float": 2.0}, "price": 3000.0,
         "value": 6000.0, "chain": "ethereum"},
    ]}}
    envelope = normalize(spec_for("positions", wallet=VITALIK), cli_raw, source="zerion_cli")
    position = envelope["data"]["positions"][0]
    assert position["symbol"] == "ETH"
    assert position["value"] == 6000.0
    assert position["chain"] == "ethereum"
    assert envelope["data"]["total_value"] == 6000.0
    assert envelope["source"] == "zerion_cli"


def test_defi_positions_are_grouped_by_protocol():
    raw = {"defi_positions": {"data": [
        {"attributes": {"protocol": "aave-v3", "value": 100.0, "position_type": "deposit",
                        "fungible_info": {"symbol": "USDC"}, "quantity": {"float": 100.0}}},
        {"attributes": {"protocol": "aave-v3", "value": 50.0, "position_type": "loan",
                        "fungible_info": {"symbol": "ETH"}, "quantity": {"float": 0.01}}},
        {"attributes": {"protocol": "lido", "value": 400.0, "position_type": "staked",
                        "fungible_info": {"symbol": "stETH"}, "quantity": {"float": 0.13}}},
    ]}}
    envelope = normalize(spec_for("defi_positions", wallet=VITALIK), raw, source="zerion_api")
    data = envelope["data"]
    assert data["protocol_count"] == 2
    assert data["protocols"][0]["protocol"] == "lido"          # sorted by value
    assert data["protocols"][0]["value"] == 400.0


def test_unknown_provider_fields_are_dropped_not_guessed():
    raw = {"pnl": {"data": {"attributes": {"total_gain": 5.0, "brand_new_field": "???"}}}}
    envelope = normalize(spec_for("pnl", wallet=VITALIK), raw, source="zerion_api")
    assert envelope["data"]["total_gain"] == 5.0
    assert "brand_new_field" not in envelope["data"]
    assert envelope["data"]["realized_gain"] == 0.0            # absent, not invented


def test_integrity_hash_is_stable_and_detects_tampering():
    raw = {"pnl": {"data": {"attributes": {"total_gain": 42.0}}}}
    spec = spec_for("pnl", wallet=VITALIK)
    envelope = normalize(spec, raw, source="zerion_api")

    assert len(envelope["integrity"]["hash"]) == 64
    assert envelope["integrity"]["algorithm"] == "sha256"
    assert verify_envelope(envelope)["valid"] is True
    # The hash is explicitly scoped: it proves what we received, not on-chain truth.
    assert "not a proof of on-chain truth" in envelope["integrity"]["note"]

    envelope["data"]["total_gain"] = 999.0
    report = verify_envelope(envelope)
    assert report["valid"] is False
    assert report["claimed_hash"] != report["computed_hash"]


def test_the_same_response_hashes_identically_twice():
    raw = {"portfolio": {"data": {"attributes": {"total": {"positions": 7.0}}}}}
    spec = spec_for("portfolio", wallet=VITALIK)
    first = normalize(spec, raw, source="zerion_api")
    second = normalize(spec, raw, source="zerion_api")
    # Timestamps differ, so compare the part the hash actually covers.
    second["timestamp"] = first["timestamp"]
    from app.integrations.zerion.normalizer import integrity_block

    assert integrity_block(first)["hash"] == integrity_block(second)["hash"]


def test_demo_fixtures_travel_the_real_normalizer():
    spec = spec_for("wallet_analysis", wallet=VITALIK)
    raw = demo_client.execute(spec)
    envelope = normalize(spec, raw.payloads, source=raw.source)
    assert envelope["source"] == "zerion_demo"
    assert envelope["data"]["portfolio"]["total_value"] > 0
    assert envelope["data"]["positions"]["count"] > 0
    assert envelope["data"]["transactions"]["count"] > 0
    assert "pnl" in envelope["data"]
    assert verify_envelope(envelope)["valid"] is True


def test_demo_fixtures_are_deterministic_per_wallet():
    spec = spec_for("portfolio", wallet=VITALIK)
    first = demo_client.execute(spec).payloads["portfolio"]
    second = demo_client.execute(spec).payloads["portfolio"]
    assert first == second

    other = demo_client.execute(spec_for("portfolio", wallet="0x" + "1" * 40)).payloads["portfolio"]
    assert other != first


# --------------------------------------------------------------------------- #
# Payment adapter
# --------------------------------------------------------------------------- #
def test_adapter_reports_not_required_on_the_api_key_rail(monkeypatch):
    monkeypatch.setattr(settings, "zerion_use_x402", False)
    monkeypatch.setattr(settings, "zerion_api_key", "zk_test")
    monkeypatch.setattr(settings, "zerion_transport", "api")

    outcome = ZerionX402PaymentAdapter().pay(request_id="req_1", capability="portfolio")
    assert outcome.rail is PaymentRail.API_KEY
    assert outcome.status == "not_required"
    assert outcome.settled is False
    assert outcome.ok is True


def test_adapter_settles_on_the_x402_rail_and_carries_the_cli_transaction(monkeypatch):
    monkeypatch.setattr(settings, "zerion_use_x402", True)
    monkeypatch.setattr(settings, "zerion_evm_private_key", "0x" + "cd" * 32)
    monkeypatch.setattr(settings, "zerion_cost_micros", 10_000)

    outcome = ZerionX402PaymentAdapter().finalize(
        request_id="req_2", capability="pnl", transport="cli", upstream_requests=1,
        succeeded=True, evidence={"transaction": "0xabc123", "network": "base"},
    )
    assert outcome.rail is PaymentRail.ZERION_X402
    assert outcome.status == "settled"
    assert outcome.settled is True
    assert outcome.currency == "USDC"
    assert outcome.amount == "0.010000"
    assert outcome.transaction == "0xabc123"
    assert outcome.network == "base"


def test_adapter_reports_a_failed_x402_leg_rather_than_claiming_settlement(monkeypatch):
    monkeypatch.setattr(settings, "zerion_use_x402", True)
    monkeypatch.setattr(settings, "zerion_evm_private_key", "0x" + "cd" * 32)

    outcome = ZerionX402PaymentAdapter().finalize(
        request_id="req_3", capability="pnl", transport="cli",
        succeeded=False, evidence={"error": "insufficient USDC"},
    )
    assert outcome.status == "failed"
    assert outcome.settled is False
    assert outcome.ok is False


def test_demo_mode_never_claims_a_settled_payment(monkeypatch):
    monkeypatch.setattr(settings, "zerion_use_x402", False)
    monkeypatch.setattr(settings, "zerion_api_key", "")
    monkeypatch.setattr(settings, "zerion_evm_private_key", "")
    monkeypatch.setattr(settings, "zerion_solana_private_key", "")
    monkeypatch.setattr(settings, "zerion_demo_mode", True)

    outcome = ZerionX402PaymentAdapter().finalize(
        request_id="req_4", capability="portfolio", transport="demo",
    )
    assert outcome.status == "simulated"
    assert outcome.settled is False
    assert outcome.amount == "0"


def test_payment_metadata_carries_no_credential_material():
    outcome = PaymentOutcome(
        rail=PaymentRail.ZERION_X402, status="settled", amount="0.01",
        currency="USDC", network="base", transaction="0xabc",
    )
    serialized = json.dumps(outcome.as_dict())
    assert "private" not in serialized.lower()
    assert "zk_" not in serialized


# --------------------------------------------------------------------------- #
# Quotas & budget
# --------------------------------------------------------------------------- #
def test_quota_blocks_a_runaway_loop_before_paying(client, consumer, monkeypatch):
    """The per-job quota stops an agent looping on a paid endpoint."""
    from app.db import SessionLocal

    monkeypatch.setattr(settings, "zerion_max_requests_per_job", 2)
    user_id = consumer["user"]["id"]
    db = SessionLocal()
    try:
        for _ in range(2):
            outcome = zerion_service.run_request(
                db, capability="portfolio", payload={"wallet": VITALIK},
                user_id=user_id, job_id="job_quota_probe",
            )
            assert outcome.ok is True
        db.commit()

        blocked = zerion_service.run_request(
            db, capability="portfolio", payload={"wallet": VITALIK},
            user_id=user_id, job_id="job_quota_probe",
        )
        db.commit()
        assert blocked.ok is False
        assert blocked.error_code == "zerion_quota_exceeded"
        assert blocked.payment is None          # nothing was authorized, nothing was paid
    finally:
        db.close()


def test_session_quota_and_spend_cap_are_independent(client, consumer, monkeypatch):
    from app.db import SessionLocal

    monkeypatch.setattr(settings, "zerion_max_requests_per_job", 100)
    monkeypatch.setattr(settings, "zerion_max_requests_per_session", 100)
    monkeypatch.setattr(settings, "zerion_max_spend_micros", 0)

    db = SessionLocal()
    try:
        outcome = zerion_service.run_request(
            db, capability="portfolio", payload={"wallet": VITALIK},
            user_id=consumer["user"]["id"],
        )
        db.commit()
        assert outcome.ok is False
        assert outcome.error_code == "zerion_quota_exceeded"
    finally:
        db.close()


def test_budget_shortfall_is_refused_before_the_request(client, consumer):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        outcome = zerion_service.run_request(
            db, capability="wallet_analysis", payload={"wallet": VITALIK},
            user_id=consumer["user"]["id"], budget_micros=1, price_micros=50_000,
        )
        db.commit()
        assert outcome.ok is False
        assert outcome.error_code == "zerion_budget_exceeded"
    finally:
        db.close()


def test_quota_endpoint_reports_remaining_headroom(client, consumer):
    body = client.get("/v1/zerion/quota", headers=consumer["headers"]).json()
    assert body["max_requests_per_job"] >= 1
    assert body["requests_remaining_this_session"] >= 0
    assert body["window_seconds"] > 0


def test_a_failed_request_still_counts_against_quota(client, consumer):
    from app.db import SessionLocal
    from app.models import ZerionRequest

    db = SessionLocal()
    try:
        before = quota_service.usage(db, user_id=consumer["user"]["id"])
        outcome = zerion_service.run_request(
            db, capability="portfolio", payload={"wallet": "not-a-wallet"},
            user_id=consumer["user"]["id"],
        )
        db.commit()
        assert outcome.ok is False
        assert outcome.error_code == "zerion_invalid_request"

        after = quota_service.usage(db, user_id=consumer["user"]["id"])
        assert after["requests_this_session"] == before["requests_this_session"] + 1
        row = db.get(ZerionRequest, outcome.record_id)
        assert row is not None
        assert row.status.value == "invalid_request"
        assert row.provider_cost_micros == 0     # a rejected request costs nothing
    finally:
        db.close()


def test_quota_enforce_raises_rather_than_returning_a_flag(client, consumer, monkeypatch):
    from app.db import SessionLocal

    monkeypatch.setattr(settings, "zerion_max_requests_per_session", 0)
    db = SessionLocal()
    try:
        with pytest.raises(ZerionQuotaError):
            quota_service.enforce(db, user_id=consumer["user"]["id"], job_id=None,
                                  cost_micros=10_000)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Provider outage & payment failure through the job path
# --------------------------------------------------------------------------- #
def test_provider_outage_refunds_the_consumer_and_skips_retry(client, consumer, monkeypatch):
    """A Zerion outage must not leave the consumer out of pocket."""
    def boom(spec, transport):
        raise ZerionUnavailableError("Zerion is unreachable")

    monkeypatch.setattr(zerion_service, "_execute_transport", boom)

    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    resp = client.post("/v1/zerion/query",
                       json={"capability": "portfolio", "wallet": VITALIK},
                       headers=consumer["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "failed"
    assert "zerion_unavailable" in body["error"]

    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert after["available_micros"] == before["available_micros"]     # fully refunded
    assert after["escrow_micros"] == 0

    events = client.get(f"/v1/jobs/{body['job_id']}/events",
                        headers=consumer["headers"]).json()
    kinds = [e["kind"] for e in events]
    assert "external_call" in kinds
    assert "refunded" in kinds


def test_an_unretryable_failure_does_not_schedule_a_retry(client, consumer, monkeypatch):
    from app.integrations.zerion.errors import ZerionQuotaError as QuotaErr

    def blocked(spec, transport):
        raise QuotaErr("quota exhausted")

    monkeypatch.setattr(zerion_service, "_execute_transport", blocked)
    resp = client.post("/v1/zerion/query",
                       json={"capability": "pnl", "wallet": VITALIK},
                       headers=consumer["headers"]).json()

    events = client.get(f"/v1/jobs/{resp['job_id']}/events",
                        headers=consumer["headers"]).json()
    kinds = [e["kind"] for e in events]
    assert "refunded" in kinds
    assert "retry_scheduled" not in kinds       # paying again would not help


def test_invalid_wallet_is_rejected_before_a_payment_exists(client, consumer):
    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    resp = client.post("/v1/zerion/query",
                       json={"capability": "portfolio", "wallet": "0xnope"},
                       headers=consumer["headers"])
    assert resp.status_code == 400
    assert "valid EVM address" in resp.text

    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert after == before                       # no quote, no escrow, no charge


def test_payment_rail_unavailable_is_reported_not_faked(client, consumer, monkeypatch):
    from app.db import SessionLocal
    from app.integrations.zerion import payment as payment_module

    monkeypatch.setattr(payment_module, "transport_name", lambda: "unavailable")
    monkeypatch.setattr(zerion_service, "transport_name", lambda: "unavailable")

    db = SessionLocal()
    try:
        outcome = zerion_service.run_request(
            db, capability="portfolio", payload={"wallet": VITALIK},
            user_id=consumer["user"]["id"],
        )
        db.commit()
        assert outcome.ok is False
        assert outcome.error_code == "zerion_payment_failed"
        assert outcome.envelope is None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# End-to-end through the exchange
# --------------------------------------------------------------------------- #
def test_end_to_end_query_pays_calls_verifies_and_receipts(client, consumer):
    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()

    resp = client.post("/v1/zerion/query",
                       json={"capability": "wallet_analysis", "wallet": VITALIK},
                       headers=consumer["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["ok"] is True
    assert body["status"] == "succeeded"
    assert body["summary"]

    envelope = body["result"]
    assert envelope["provider"] == "zerion"
    assert envelope["wallet"] == VITALIK.lower()
    assert envelope["request_type"] == "wallet_analysis"
    assert envelope["source"] in ("zerion_api", "zerion_cli", "zerion_demo")
    assert {"portfolio", "positions", "transactions", "pnl"} <= set(envelope["data"])

    # Both legs are reported, and they are different rails.
    assert body["consumer_payment"]["rail"] == "m2x_algorand"
    assert body["consumer_payment"]["status"] in ("settled", "escrowed")
    assert body["consumer_payment"]["charged_micros"] > 0
    assert body["provider_payment"]["rail"] in ("zerion_x402", "api_key", "none")

    # Integrity: response hash, job manifest and a hash-chained receipt.
    assert len(body["integrity"]["response_hash"]) == 64
    assert body["integrity"]["verified"] is True
    assert body["receipt"] and body["receipt"]["chain_hash"]

    # Telemetry a judge can read.
    telemetry = body["telemetry"]
    assert telemetry["transport"] in ("api", "cli", "demo")
    assert telemetry["upstream_requests"] >= 1
    assert telemetry["latency_ms"] >= 0

    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    spent = before["available_micros"] - after["available_micros"]
    assert spent == body["consumer_payment"]["charged_micros"]
    assert after["escrow_micros"] == 0


def test_the_receipt_states_both_payment_rails(client, consumer):
    body = client.post("/v1/zerion/query",
                       json={"capability": "pnl", "wallet": VITALIK},
                       headers=consumer["headers"]).json()
    assert body["ok"] is True

    receipt = client.get(f"/v1/receipts/{body['receipt']['id']}",
                         headers=consumer["headers"]).json()
    external = receipt["body"]["external_provider"]
    assert external["provider"] == "zerion"
    assert external["capability"] == "pnl"
    assert external["payment"]["rail"] in ("zerion_x402", "api_key", "none")
    assert len(external["integrity"]["response_hash"]) == 64
    # The exchange's own leg is still stated in the mandated asset.
    assert receipt["body"]["payment"]["asset_id"] == 10458941

    verified = client.get(f"/v1/receipts/{body['receipt']['id']}/verify").json()
    assert verified["valid"] is True


def test_a_zerion_capability_is_buyable_through_the_plain_x402_endpoint(client, consumer, services):
    """No Zerion-specific client code needed: it is an ordinary catalog service."""
    from conftest import sign_payment

    service = services["zerion-portfolio"]
    url = f"/v1/services/{service['id']}/invoke"
    payload = {"wallet": VITALIK}

    challenge = client.post(url, json=payload, headers=consumer["headers"])
    assert challenge.status_code == 402
    requirements = challenge.json()["accepts"][0]
    assert requirements["scheme"] == "exact"
    assert requirements["extra"]["assetId"] == 10458941

    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])
    paid = client.post(url, json=payload, headers={**consumer["headers"], "X-PAYMENT": header})
    assert paid.status_code == 200, paid.text
    result = paid.json()["result"]
    assert result["provider"] == "zerion"
    assert result["request_type"] == "portfolio"
    assert paid.json()["integrity_verified"] is True


def test_result_artifacts_are_stored_and_hash_verified(client, consumer):
    body = client.post("/v1/zerion/query",
                       json={"capability": "positions", "wallet": VITALIK},
                       headers=consumer["headers"]).json()
    artifacts = client.get(f"/v1/jobs/{body['job_id']}/artifacts",
                           headers=consumer["headers"]).json()
    names = {a["name"] for a in artifacts}
    assert "zerion_response.json" in names

    stored = next(a for a in artifacts if a["name"] == "zerion_response.json")
    download = client.get(f"/v1/artifacts/{stored['id']}", headers=consumer["headers"])
    assert download.status_code == 200
    assert download.headers["X-Content-SHA256"] == stored["sha256"]

    import hashlib
    assert hashlib.sha256(download.content).hexdigest() == stored["sha256"]
    assert json.loads(download.content)["provider"] == "zerion"


def test_no_sandbox_worker_is_provisioned_for_an_external_service(client, consumer, admin_headers):
    stats_before = client.get("/v1/stats").json()["workers"]["total"]
    client.post("/v1/zerion/query", json={"capability": "chains"},
                headers=consumer["headers"])
    stats_after = client.get("/v1/stats").json()["workers"]["total"]
    assert stats_after == stats_before          # nothing was spawned to make an HTTP call


def test_verify_endpoint_recomputes_the_stored_hash(client, consumer):
    body = client.post("/v1/zerion/query",
                       json={"capability": "portfolio", "wallet": VITALIK},
                       headers=consumer["headers"]).json()
    verify = client.get(f"/v1/zerion/verify/{body['job_id']}",
                        headers=consumer["headers"]).json()
    assert verify["response_integrity"]["valid"] is True
    assert verify["manifest_verified"] is True
    assert verify["receipt"]["external_provider"]["provider"] == "zerion"


# --------------------------------------------------------------------------- #
# Dashboard API
# --------------------------------------------------------------------------- #
def test_dashboard_telemetry_endpoints(client, consumer):
    client.post("/v1/zerion/query", json={"capability": "portfolio", "wallet": VITALIK},
                headers=consumer["headers"])

    requests = client.get("/v1/zerion/requests", headers=consumer["headers"]).json()
    assert requests
    row = requests[0]
    assert row["capability"]
    assert row["transport"] in ("api", "cli", "demo")
    assert "private_key" not in json.dumps(row).lower()

    detail = client.get(f"/v1/zerion/requests/{row['id']}",
                        headers=consumer["headers"]).json()
    assert detail["id"] == row["id"]
    if detail["result"]:
        assert detail["integrity_check"]["valid"] is True

    stats = client.get("/v1/zerion/stats").json()
    assert stats["requests_total"] >= 1
    assert stats["by_capability"]

    payments = client.get("/v1/zerion/payments", headers=consumer["headers"]).json()
    assert payments["count"] >= 1
    leg = payments["items"][0]
    assert leg["consumer_leg"]["rail"] == "m2x_algorand"
    assert "provider_leg" in leg


def test_preview_prices_a_request_without_charging(client, consumer):
    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    body = client.post("/v1/zerion/preview",
                       json={"capability": "portfolio", "wallet": VITALIK},
                       headers=consumer["headers"]).json()
    assert body["quote"]["max_price_micros"] > 0
    assert body["request"]["wallet"] == VITALIK.lower()
    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert after == before


def test_zerion_endpoints_require_authentication(client):
    assert client.post("/v1/zerion/query", json={"capability": "chains"}).status_code == 401
    assert client.get("/v1/zerion/requests").status_code == 401
    assert client.get("/v1/zerion/quota").status_code == 401


def test_a_principal_cannot_read_another_principals_requests(client, consumer, admin_headers):
    body = client.post("/v1/zerion/query",
                       json={"capability": "chains"}, headers=consumer["headers"]).json()
    request_id = client.get("/v1/zerion/requests",
                            headers=consumer["headers"]).json()[0]["id"]
    assert body["ok"] is True

    import uuid
    other = client.post("/v1/auth/register",
                        json={"email": f"o-{uuid.uuid4().hex[:8]}@test.local",
                              "password": "password-12345"}).json()
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(f"/v1/zerion/requests/{request_id}", headers=headers).status_code == 403


# --------------------------------------------------------------------------- #
# Agent intent routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("goal,capability", [
    (f"Analyze wallet {VITALIK}", "wallet_analysis"),
    (f"Show portfolio of {VITALIK}", "portfolio"),
    (f"What tokens does {VITALIK} hold?", "positions"),
    (f"Show DeFi positions for {VITALIK}", "defi_positions"),
    (f"Show last 20 transactions of {VITALIK}", "transactions"),
    (f"How much profit or loss has {VITALIK} made?", "pnl"),
    ("Analyze wallet vitalik.eth", "wallet_analysis"),
])
def test_agent_routes_wallet_questions_to_zerion(goal, capability):
    steps = decompose(goal)
    step = steps[0]
    assert step.provider_hint == "zerion", f"{goal!r} did not route to Zerion"
    assert step.params["capability"] == capability
    assert step.params["wallet"]


@pytest.mark.parametrize("goal", [
    "Hash this text with sha256",
    "Compute the primes below 20000",
    "Analyze this text and summarize it",
    "Transform the JSON payload into a flat structure",
    "Validate the document against the schema",
    "Build a quarterly report",
    "Show me DeFi positions",                    # on-chain wording, but no wallet
    "How much profit did the strategy make?",    # no wallet to look up
])
def test_agent_does_not_call_zerion_for_unrelated_or_unresolvable_goals(goal):
    for step in decompose(goal):
        assert step.provider_hint != "zerion", f"{goal!r} should not reach a paid provider"


def test_a_wallet_named_once_carries_across_later_steps():
    steps = decompose(f"Analyze wallet {VITALIK}, then show its DeFi positions, "
                      f"then show its PnL")
    assert len(steps) == 3
    assert [s.params.get("capability") for s in steps] == [
        "wallet_analysis", "defi_positions", "pnl"
    ]
    assert {s.params["wallet"] for s in steps} == {VITALIK.lower()}


def test_wallet_extraction_ignores_ordinary_words():
    assert extract_wallet("please analyze this document carefully") == ""
    assert extract_wallet(f"check {VITALIK} now") == VITALIK.lower()
    assert extract_wallet("look at vitalik.eth") == "vitalik.eth"


def test_chain_and_limit_hints_are_extracted():
    intent = detect_onchain_intent(f"show the last 15 transactions of {VITALIK} on base")
    assert intent.matched is True
    assert intent.capability == "transactions"
    assert intent.chain == "base"
    assert intent.limit == 15


def test_agent_plan_preview_selects_the_zerion_service(client, consumer):
    preview = client.post("/v1/plans/preview",
                          json={"goal": f"Analyze wallet {VITALIK}"},
                          headers=consumer["headers"]).json()
    step = preview["steps"][0]
    assert step["provider_hint"] == "zerion"
    candidates = step["candidates"]
    assert candidates, "the planner found no Zerion candidate"
    assert candidates[0]["slug"] == "zerion-wallet-analysis"
    assert candidates[0]["provider_slug"] == "zerion"


def test_agent_plan_runs_the_full_zerion_path(client, consumer):
    plan = client.post("/v1/plans",
                       json={"goal": f"Analyze wallet {VITALIK}", "budget_micros": 2_000_000},
                       headers=consumer["headers"])
    assert plan.status_code == 201, plan.text
    body = plan.json()

    step = body["steps"][0]
    assert step["provider_hint"] == "zerion"
    assert step["status"] == "succeeded", step.get("error")
    assert step["service_slug"] == "zerion-wallet-analysis"
    assert step["output"]["provider"] == "zerion"
    assert len(step["output"]["integrity"]["hash"]) == 64

    assert body["result"]["summary"]
    assert "zerion" in body["result"]["providers_used"]
    assert body["spent_micros"] > 0

    # The trace shows the exchange leg being paid before Zerion was contacted.
    nodes = [t["node"] for t in body["trace"]]
    assert nodes.index("pay") < nodes.index("execute")
    assert "verify" in nodes and "settle" in nodes


def test_mcp_exposes_the_onchain_tool(client, consumer):
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers=consumer["headers"]).json()
    tools = {t["name"] for t in listed["result"]["tools"]}
    assert "onchain_intelligence" in tools

    called = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "onchain_intelligence",
                   "arguments": {"capability": "portfolio", "wallet": VITALIK}},
    }, headers=consumer["headers"]).json()
    result = called["result"]["structuredContent"]
    assert result["status"] == "succeeded"
    assert result["result"]["provider"] == "zerion"
    assert len(result["integrity_hash"]) == 64


# --------------------------------------------------------------------------- #
# Optional real-credential integration test
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("ZERION_LIVE_TEST"),
    reason=(
        "Live Zerion test. Enable deliberately with ZERION_LIVE_TEST=1 plus either "
        "ZERION_API_KEY=zk_... (free, no per-request charge) or ZERION_USE_X402=true with "
        "ZERION_EVM_PRIVATE_KEY / ZERION_SOLANA_PRIVATE_KEY and the Zerion CLI installed "
        "(this spends real USDC — roughly 0.01 per request)."
    ),
)
def test_live_zerion_portfolio_against_the_real_provider(client, consumer):
    """Optional: exercises the real provider with real credentials.

    Never runs in CI. With ZERION_API_KEY set it costs nothing; with x402 enabled
    it spends real USDC on Base or Solana, which is why it is opt-in.
    """
    wallet = os.getenv("ZERION_LIVE_WALLET", VITALIK)
    resp = client.post("/v1/zerion/query",
                       json={"capability": "portfolio", "wallet": wallet},
                       headers=consumer["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body.get("error")

    envelope = body["result"]
    assert envelope["source"] in ("zerion_api", "zerion_cli")   # not the demo fixture
    assert envelope["data"]["total_value"] >= 0
    assert verify_envelope(envelope)["valid"] is True
    assert body["telemetry"]["transport"] in ("api", "cli")
    print("\nlive Zerion result:", body["summary"])
    print("provider payment leg:", body["provider_payment"])
