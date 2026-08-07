"""The payment asset is Algorand ASA #10458941, everywhere, or the money stops.

These cover the promise the exchange makes to both sides of a trade: the buyer
authorizes one specific asset, the seller is advertised as accepting it, and
nothing runs or settles in anything else.
"""
from __future__ import annotations

import base64
import json

from conftest import sign_payment

ASSET_ID = 10458941
ASSET = str(ASSET_ID)


def _decode(header: str) -> dict:
    return json.loads(base64.b64decode(header))


def _encode(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_config_advertises_the_mandated_asset(client):
    config = client.get("/v1/config").json()
    assert config["payment_asset"]["asset_id"] == ASSET_ID
    assert config["payment_asset"]["blockchain"] == "Algorand"
    assert config["payment_asset"]["network"] == "TestNet"
    assert config["payment_asset"]["display"] == f"Algorand ASA #{ASSET_ID}"
    assert config["algorand"]["asset_id"] == ASSET_ID
    assert config["x402"]["asset"] == ASSET
    assert config["x402"]["network"] == "algorand-testnet"


def test_asset_endpoint_reports_a_clean_configuration(client):
    report = client.get("/v1/x402/asset").json()
    assert report["ok"] is True
    assert report["mandated_asset_id"] == ASSET_ID
    assert report["overridden_by_administrator"] is False
    assert all(c["ok"] for c in report["checks"])


def test_facilitator_only_supports_the_mandated_asset(client):
    supported = client.get("/v1/x402/supported").json()
    assert supported["asset"]["asset_id"] == ASSET_ID
    assert [k["assetId"] for k in supported["kinds"]] == [ASSET_ID]


def test_health_flags_the_configured_asset(client):
    body = client.get("/health").json()
    assert body["components"]["x402"]["asset_id"] == ASSET_ID
    assert body["components"]["algorand"]["asset"]["asset_id"] == ASSET_ID


# --------------------------------------------------------------------------- #
# Payment requirements & authorization
# --------------------------------------------------------------------------- #
def test_402_challenge_quotes_the_asset(client, consumer, services):
    resp = client.post(f"/v1/services/{services['sha256-notary']['id']}/invoke",
                       json={"text": "quote me"}, headers=consumer["headers"])
    assert resp.status_code == 402
    requirements = resp.json()["accepts"][0]
    assert requirements["asset"] == ASSET
    assert requirements["network"] == "algorand-testnet"
    assert requirements["extra"]["assetId"] == ASSET_ID
    assert requirements["extra"]["blockchain"] == "Algorand"
    assert resp.json()["quote"]["asset_id"] == ASSET_ID


def test_signed_authorization_names_the_asset(client, consumer, services):
    challenge = client.post(f"/v1/services/{services['sha256-notary']['id']}/invoke",
                            json={"text": "sign me"}, headers=consumer["headers"])
    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])
    auth = _decode(header)["payload"]["authorization"]
    assert auth["asset"] == ASSET
    assert auth["network"] == "algorand-testnet"


def test_authorization_for_another_asset_is_rejected(client, consumer, services):
    """Re-pointing a payment at a different ASA must not buy compute."""
    url = f"/v1/services/{services['text-analyzer']['id']}/invoke"
    challenge = client.post(url, json={"text": "wrong asset"}, headers=consumer["headers"])
    payment_id = challenge.json()["payment_id"]
    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()

    forged = _decode(sign_payment(client, consumer["headers"], payment_id))
    forged["payload"]["authorization"]["asset"] = "31566704"  # a different ASA
    forged["payload"]["asset"] = "31566704"

    resp = client.post(url, json={"text": "wrong asset"},
                       headers={**consumer["headers"], "X-PAYMENT": _encode(forged)})
    assert resp.status_code == 402
    # Rejected for the tampered signature or the asset itself — either way the
    # authorization is refused before execution.
    assert "invalid_signature" in resp.text or "asset_mismatch" in resp.text

    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert after == before  # nothing escrowed, nothing spent


def test_authorization_without_an_asset_is_rejected(client, consumer, services):
    url = f"/v1/services/{services['sha256-notary']['id']}/invoke"
    challenge = client.post(url, json={"text": "no asset"}, headers=consumer["headers"])
    payload = _decode(sign_payment(client, consumer["headers"], challenge.json()["payment_id"]))
    payload["payload"]["authorization"].pop("asset")
    payload["payload"].pop("asset", None)

    resp = client.post(url, json={"text": "no asset"},
                       headers={**consumer["headers"], "X-PAYMENT": _encode(payload)})
    assert resp.status_code == 402
    assert "invalid_signature" in resp.text or "asset_missing" in resp.text


# --------------------------------------------------------------------------- #
# Validation gate, settlement, receipts
# --------------------------------------------------------------------------- #
def test_preflight_reports_every_mandated_check(client, consumer, services):
    url = f"/v1/services/{services['sha256-notary']['id']}/invoke"
    challenge = client.post(url, json={"text": "preflight"}, headers=consumer["headers"])
    job_id = challenge.headers["X-Job-Id"]

    report = client.get(f"/v1/jobs/{job_id}/preflight", headers=consumer["headers"]).json()
    names = {c["check"] for c in report["checks"]}
    assert names == {"algorand_network", "asset_id", "buyer_balance", "seller_address",
                     "payment_authorization", "settlement_success"}
    assert report["asset"]["asset_id"] == ASSET_ID
    # Not paid yet, so authorization has not happened and the job may not run.
    assert report["ok"] is False
    assert "payment_authorization" in report["failed"]


def test_paid_job_passes_validation_and_settles_in_the_asset(client, consumer, services):
    url = f"/v1/services/{services['sha256-notary']['id']}/invoke"
    challenge = client.post(url, json={"text": "settle me"}, headers=consumer["headers"])
    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])
    paid = client.post(url, json={"text": "settle me"},
                       headers={**consumer["headers"], "X-PAYMENT": header})
    assert paid.status_code == 200, paid.text

    settlement = _decode(paid.headers["X-PAYMENT-RESPONSE"])
    assert settlement["assetId"] == ASSET_ID
    assert settlement["blockchain"] == "Algorand"

    events = client.get(f"/v1/jobs/{paid.json()['job_id']}/events",
                        headers=consumer["headers"]).json()
    validated = next(e for e in events if e["kind"] == "validated")
    assert validated["data"]["asset_id"] == ASSET_ID
    assert all(c["ok"] for c in validated["data"]["checks"])

    settled = next(e for e in events if e["kind"] == "settled")
    assert settled["data"]["asset_id"] == ASSET_ID
    assert settled["data"]["settlement_verified"] is True


def test_receipt_carries_chain_network_asset_transaction_and_status(client, consumer, services):
    url = f"/v1/services/{services['text-analyzer']['id']}/invoke"
    challenge = client.post(url, json={"text": "receipt fields please"},
                            headers=consumer["headers"])
    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])
    paid = client.post(url, json={"text": "receipt fields please"},
                       headers={**consumer["headers"], "X-PAYMENT": header})
    assert paid.status_code == 200, paid.text

    receipts = client.get("/v1/receipts", headers=consumer["headers"]).json()
    receipt = next(r for r in receipts if r["job_id"] == paid.json()["job_id"])
    payment = receipt["body"]["payment"]

    assert payment["blockchain"] == "Algorand"
    assert payment["network"] == "TestNet"
    assert payment["asset_id"] == ASSET_ID
    assert payment["transaction_id"]
    assert payment["settlement_status"] == "settled"

    # the added fields are inside the signed body, so the chain still verifies
    assert client.get(f"/v1/receipts/{receipt['id']}/verify").json()["valid"] is True


def test_payment_history_reports_the_asset(client, consumer, services):
    url = f"/v1/services/{services['sha256-notary']['id']}/invoke"
    challenge = client.post(url, json={"text": "history"}, headers=consumer["headers"])
    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])
    client.post(url, json={"text": "history"},
                headers={**consumer["headers"], "X-PAYMENT": header})

    payments = client.get("/v1/payments", headers=consumer["headers"]).json()
    assert payments
    assert all(p["asset_id"] == ASSET_ID for p in payments)
    assert all(p["payment_asset"]["display"] == f"Algorand ASA #{ASSET_ID}" for p in payments)


# --------------------------------------------------------------------------- #
# Marketplace & discovery
# --------------------------------------------------------------------------- #
def test_provider_registration_defaults_to_the_asset(client, consumer):
    provider = client.post(
        "/v1/providers",
        json={"slug": f"asa-prov-{consumer['user']['id'][-8:]}", "name": "ASA Provider"},
        headers=consumer["headers"],
    )
    assert provider.status_code == 201, provider.text
    body = provider.json()
    assert body["payment_asset_id"] == ASSET_ID
    assert body["payment_asset"]["display"] == f"Algorand ASA #{ASSET_ID}"


def test_provider_registration_refuses_another_asset(client, consumer):
    resp = client.post(
        "/v1/providers",
        json={"slug": f"bad-asa-{consumer['user']['id'][-8:]}", "name": "Wrong Asset Provider",
              "payment_asset_id": 31566704},
        headers=consumer["headers"],
    )
    assert resp.status_code == 422
    assert "10458941" in resp.text


def test_services_are_priced_in_the_asset(client, services):
    service = services["prime-sieve"]
    assert service["payment_asset"]["asset_id"] == ASSET_ID
    assert service["payment_asset"]["decimals"] == 6


def test_bazaar_listings_advertise_the_asset(client):
    listings = client.get("/v1/bazaar/listings", params={"source": "local"}).json()
    assert listings["payment_asset"]["asset_id"] == ASSET_ID
    assert listings["items"]
    for item in listings["items"]:
        assert item["asset_id"] == ASSET_ID
        assert item["payable"] is True
        assert item["accepts"][0]["asset"] == ASSET
        assert item["accepts"][0]["extra"]["assetId"] == ASSET_ID

    status = client.get("/v1/bazaar/status").json()
    assert status["asset"]["asset_id"] == ASSET_ID
    assert status["payable_listings"] >= len(listings["items"])


def test_listings_can_be_filtered_to_payable_providers(client):
    payable = client.get("/v1/bazaar/listings", params={"payable_only": True}).json()
    assert all(item["asset_id"] == ASSET_ID for item in payable["items"])

    foreign = client.get("/v1/bazaar/listings", params={"asset_id": 31566704}).json()
    assert foreign["items"] == []


# --------------------------------------------------------------------------- #
# Agent planner
# --------------------------------------------------------------------------- #
def test_agent_costs_are_denominated_in_the_asset(client, consumer):
    plan = client.post("/v1/plans",
                       json={"goal": "hash this payload", "budget_micros": 200_000},
                       headers=consumer["headers"]).json()
    assert plan["result"]["payment_asset"]["asset_id"] == ASSET_ID
    assert all(step["asset_id"] == ASSET_ID for step in plan["steps"])
