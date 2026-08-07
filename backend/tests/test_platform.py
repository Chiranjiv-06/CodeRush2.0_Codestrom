"""End-to-end coverage of the exchange: marketplace, x402, execution, money, trust."""
from __future__ import annotations

import base64
import json

from conftest import sign_payment


# --------------------------------------------------------------------------- #
# Platform surface
# --------------------------------------------------------------------------- #
def test_health_reports_active_backends(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    components = body["components"]
    assert components["database"]["ok"] is True
    assert components["sandbox"]["backend"] in ("docker", "subprocess")
    assert components["storage"]["backend"] in ("local", "minio")


def test_public_config_and_metrics(client):
    config = client.get("/v1/config").json()
    assert config["x402"]["version"] == 1
    assert config["bazaar"]["extension"] == "@x402-avm/extensions"

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert b"m2x_http_requests_total" in metrics.content


def test_seed_created_marketplace(services):
    assert "sha256-notary" in services
    assert "prime-sieve" in services
    assert services["sha256-notary"]["source_hash"]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def test_registration_grants_testnet_balance(client, consumer):
    balance = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert balance["available_micros"] == 25_000_000
    assert balance["escrow_micros"] == 0


def test_api_key_authenticates_machine_clients(client, consumer):
    created = client.post("/v1/auth/api-keys", json={"name": "worker"},
                          headers=consumer["headers"])
    assert created.status_code == 201
    key = created.json()["key"]
    me = client.get("/v1/auth/me", headers={"X-API-Key": key})
    assert me.status_code == 200
    assert me.json()["id"] == consumer["user"]["id"]

    # the raw key is never stored, only its digest
    assert key not in json.dumps(client.get("/v1/auth/api-keys",
                                            headers=consumer["headers"]).json())


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/v1/auth/me").status_code == 401
    assert client.post("/v1/quotes", json={"service_id": "svc_x"}).status_code == 401


# --------------------------------------------------------------------------- #
# Marketplace
# --------------------------------------------------------------------------- #
def test_provider_can_publish_a_priced_service(client, consumer):
    provider = client.post(
        "/v1/providers",
        json={"slug": f"test-prov-{consumer['user']['id'][-8:]}", "name": "Test Provider",
              "description": "unit test provider"},
        headers=consumer["headers"],
    )
    assert provider.status_code == 201, provider.text
    provider_id = provider.json()["id"]

    service = client.post(
        f"/v1/providers/{provider_id}/services",
        json={
            "slug": "echo-service",
            "name": "Echo Service",
            "description": "echoes the payload back",
            "category": "transform",
            "runtime": "python",
            "entrypoint": "import json,sys\nprint(json.dumps({'echo': json.load(sys.stdin)}))",
            "base_price_micros": 500,
            "max_price_micros": 20_000,
            "max_runtime_seconds": 15,
        },
        headers=consumer["headers"],
    )
    assert service.status_code == 201, service.text
    body = service.json()
    assert body["source_hash"]

    source = client.get(f"/v1/services/{body['id']}/source").json()
    assert source["matches"] is True

    listings = client.get("/v1/bazaar/listings", params={"q": "echo"}).json()
    assert any(item["service_id"] == body["id"] for item in listings["items"])


def test_quote_is_priced_before_committing_funds(client, consumer, services):
    service = services["sha256-notary"]
    quote = client.post("/v1/quotes",
                        json={"service_id": service["id"], "payload": {"text": "hello"}},
                        headers=consumer["headers"])
    assert quote.status_code == 200
    body = quote.json()
    assert 0 < body["max_price_micros"] <= service["max_price_micros"]
    assert len(body["input_hash"]) == 64


# --------------------------------------------------------------------------- #
# x402 payment protocol
# --------------------------------------------------------------------------- #
def test_invoke_without_payment_returns_402_with_requirements(client, consumer, services):
    resp = client.post(f"/v1/services/{services['sha256-notary']['id']}/invoke",
                       json={"text": "pay me"}, headers=consumer["headers"])
    assert resp.status_code == 402
    body = resp.json()
    assert body["x402Version"] == 1
    requirements = body["accepts"][0]
    assert requirements["scheme"] == "exact"
    assert int(requirements["maxAmountRequired"]) > 0
    assert requirements["extra"]["nonce"]
    assert requirements["resource"].endswith("/invoke")
    assert resp.headers["X-Payment-Id"] == body["payment_id"]


def test_full_x402_cycle_executes_and_settles(client, consumer, services):
    service = services["sha256-notary"]
    url = f"/v1/services/{service['id']}/invoke"

    challenge = client.post(url, json={"text": "machine-to-machine"},
                            headers=consumer["headers"])
    assert challenge.status_code == 402
    payment_id = challenge.json()["payment_id"]
    quoted = int(challenge.json()["accepts"][0]["maxAmountRequired"])

    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    header = sign_payment(client, consumer["headers"], payment_id)

    paid = client.post(url, json={"text": "machine-to-machine"},
                       headers={**consumer["headers"], "X-PAYMENT": header})
    assert paid.status_code == 200, paid.text
    body = paid.json()

    assert body["status"] == "succeeded"
    assert body["integrity_verified"] is True
    assert len(body["result"]["digest"]) == 64
    assert body["charged_micros"] > 0
    assert body["charged_micros"] <= quoted

    settlement = json.loads(base64.b64decode(paid.headers["X-PAYMENT-RESPONSE"]))
    assert settlement["success"] is True
    assert settlement["transaction"].startswith("0x")

    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    spent = before["available_micros"] - after["available_micros"]
    assert spent == body["charged_micros"]
    assert after["escrow_micros"] == 0  # unused escrow returned


def test_tampered_authorization_is_rejected(client, consumer, services):
    url = f"/v1/services/{services['text-analyzer']['id']}/invoke"
    challenge = client.post(url, json={"text": "tamper"}, headers=consumer["headers"])
    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])

    forged = json.loads(base64.b64decode(header))
    forged["payload"]["authorization"]["value"] = "1"  # pay one micro instead
    tampered = base64.b64encode(json.dumps(forged).encode()).decode()

    resp = client.post(url, json={"text": "tamper"},
                       headers={**consumer["headers"], "X-PAYMENT": tampered})
    assert resp.status_code == 402
    assert "invalid_signature" in resp.text or "insufficient_authorization" in resp.text


def test_replaying_another_principals_payment_is_blocked(client, consumer, services, admin_headers):
    url = f"/v1/services/{services['sha256-notary']['id']}/invoke"
    challenge = client.post(url, json={"text": "mine"}, headers=consumer["headers"])
    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])

    stolen = client.post(url, json={"text": "mine"},
                         headers={**admin_headers, "X-PAYMENT": header})
    assert stolen.status_code == 403


def test_unknown_nonce_is_refused(client, consumer, services):
    fake = base64.b64encode(json.dumps({
        "x402Version": 1, "scheme": "exact", "network": "algorand-testnet",
        "payload": {"authorization": {"nonce": "deadbeef", "from": consumer["user"]["id"]},
                    "signature": "00"},
    }).encode()).decode()
    resp = client.post(f"/v1/services/{services['sha256-notary']['id']}/invoke",
                       json={"text": "x"}, headers={**consumer["headers"], "X-PAYMENT": fake})
    assert resp.status_code == 400
    assert "unknown payment nonce" in resp.text


def test_insufficient_funds_blocks_settlement(client, consumer):
    """A quote above the payer's balance is refused at verification, before execution."""
    provider = client.post("/v1/providers",
                           json={"slug": f"pricey-{consumer['user']['id'][-6:]}",
                                 "name": "Pricey Provider"},
                           headers=consumer["headers"]).json()
    service = client.post(
        f"/v1/providers/{provider['id']}/services",
        json={"slug": "gold-plated", "name": "Gold Plated Compute", "category": "compute",
              "runtime": "python", "entrypoint": "print('{}')",
              "base_price_micros": 10_000_000,
              "price_per_cpu_second_micros": 10_000_000,
              "max_price_micros": 100_000_000,
              "max_runtime_seconds": 10},
        headers=consumer["headers"],
    ).json()

    balance = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    url = f"/v1/services/{service['id']}/invoke"
    challenge = client.post(url, json={}, headers=consumer["headers"])
    assert challenge.status_code == 402
    quoted = int(challenge.json()["accepts"][0]["maxAmountRequired"])
    assert quoted > balance["available_micros"]

    header = sign_payment(client, consumer["headers"], challenge.json()["payment_id"])
    resp = client.post(url, json={}, headers={**consumer["headers"], "X-PAYMENT": header})
    assert resp.status_code == 402
    assert "insufficient_funds" in resp.text

    # nothing was escrowed, nothing was spent
    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert after == balance


# --------------------------------------------------------------------------- #
# Execution, metering, integrity, receipts
# --------------------------------------------------------------------------- #
def _run(client, headers, service, payload):
    url = f"/v1/services/{service['id']}/invoke"
    challenge = client.post(url, json=payload, headers=headers)
    assert challenge.status_code == 402, challenge.text
    header = sign_payment(client, headers, challenge.json()["payment_id"])
    return client.post(url, json=payload, headers={**headers, "X-PAYMENT": header})


def test_sandbox_runs_real_code_and_meters_usage(client, consumer, services):
    resp = _run(client, consumer["headers"], services["prime-sieve"], {"limit": 20000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["prime_count"] == 2262
    assert body["result"]["first_20"][0] == 2

    events = client.get(f"/v1/jobs/{body['job_id']}/events",
                        headers=consumer["headers"]).json()
    kinds = [e["kind"] for e in events]
    for expected in ("created", "payment_escrowed", "started", "metered",
                     "integrity_checked", "succeeded", "settled", "receipt_issued"):
        assert expected in kinds, f"missing lifecycle event {expected}: {kinds}"

    metered = next(e for e in events if e["kind"] == "metered")
    assert metered["data"]["wall_ms"] > 0
    assert metered["data"]["price_micros"] > 0


def test_artifacts_are_stored_and_hash_verified(client, consumer, services):
    resp = _run(client, consumer["headers"], services["report-builder"],
                {"goal": "quarterly report", "input": {"revenue": 42, "region": "emea"}})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    artifacts = client.get(f"/v1/jobs/{job_id}/artifacts", headers=consumer["headers"]).json()
    names = {a["name"] for a in artifacts}
    assert {"report.md", "data.csv"} <= names

    report = next(a for a in artifacts if a["name"] == "report.md")
    download = client.get(f"/v1/artifacts/{report['id']}", headers=consumer["headers"])
    assert download.status_code == 200
    assert download.headers["X-Content-SHA256"] == report["sha256"]
    assert b"# M2X Report" in download.content

    import hashlib

    assert hashlib.sha256(download.content).hexdigest() == report["sha256"]


def test_receipts_form_a_verifiable_hash_chain(client, consumer, services):
    _run(client, consumer["headers"], services["sha256-notary"], {"text": "receipt one"})
    _run(client, consumer["headers"], services["text-analyzer"], {"text": "receipt two please"})

    receipts = client.get("/v1/receipts", headers=consumer["headers"]).json()
    assert receipts
    latest = receipts[0]
    verify = client.get(f"/v1/receipts/{latest['id']}/verify").json()
    assert verify["valid"] is True
    assert all(verify["checks"].values())

    chain = client.get("/v1/receipts/chain").json()
    assert chain["chain_valid"] is True
    assert chain["receipts_checked"] >= 2
    assert chain["broken"] == []

    body = latest["body"]
    assert body["integrity"]["algorithm"] == "sha256"
    assert body["payment"]["charged_micros"] > 0


def test_failing_service_refunds_escrow_and_schedules_retry(client, consumer):
    provider = client.post("/v1/providers",
                           json={"slug": f"broken-{consumer['user']['id'][-6:]}",
                                 "name": "Broken Provider"},
                           headers=consumer["headers"]).json()
    service = client.post(
        f"/v1/providers/{provider['id']}/services",
        json={"slug": "always-fails", "name": "Always Fails", "category": "compute",
              "runtime": "python", "entrypoint": "raise SystemExit(3)",
              "base_price_micros": 1000, "max_price_micros": 20_000,
              "max_runtime_seconds": 10},
        headers=consumer["headers"],
    ).json()

    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    resp = _run(client, consumer["headers"], service, {"text": "boom"})
    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "failed"

    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert after["available_micros"] == before["available_micros"]  # fully refunded
    assert after["escrow_micros"] == 0

    events = client.get(f"/v1/jobs/{body['job_id']}/events",
                        headers=consumer["headers"]).json()
    kinds = [e["kind"] for e in events]
    assert "failed" in kinds and "refunded" in kinds and "retry_scheduled" in kinds

    refunds = client.get("/v1/refunds", headers=consumer["headers"]).json()
    assert any(r["job_id"] == body["job_id"] for r in refunds)


def test_timeout_is_enforced_by_the_sandbox(client, consumer):
    provider = client.post("/v1/providers",
                           json={"slug": f"slow-{consumer['user']['id'][-6:]}",
                                 "name": "Slow Provider"},
                           headers=consumer["headers"]).json()
    service = client.post(
        f"/v1/providers/{provider['id']}/services",
        json={"slug": "hangs", "name": "Hangs Forever", "category": "compute",
              "runtime": "python", "entrypoint": "import time\ntime.sleep(120)",
              "base_price_micros": 1000, "max_price_micros": 20_000,
              "max_runtime_seconds": 2},
        headers=consumer["headers"],
    ).json()

    resp = _run(client, consumer["headers"], service, {})
    assert resp.status_code == 502
    assert "timeout" in resp.json()["error"].lower()


# --------------------------------------------------------------------------- #
# Disputes & reputation
# --------------------------------------------------------------------------- #
def test_dispute_on_a_healthy_job_goes_to_review(client, consumer, services):
    resp = _run(client, consumer["headers"], services["sha256-notary"], {"text": "dispute me"})
    job_id = resp.json()["job_id"]

    dispute = client.post("/v1/disputes",
                          json={"job_id": job_id, "reason": "quality",
                                "detail": "not what I wanted"},
                          headers=consumer["headers"])
    assert dispute.status_code == 201
    dispute_id = dispute.json()["id"]

    triaged = client.post(f"/v1/disputes/{dispute_id}/triage",
                          headers=consumer["headers"]).json()
    assert triaged["status"] == "under_review"  # no evidence of provider fault


def test_dispute_on_a_failed_job_auto_refunds(client, consumer):
    provider = client.post("/v1/providers",
                           json={"slug": f"faulty-{consumer['user']['id'][-6:]}",
                                 "name": "Faulty Provider"},
                           headers=consumer["headers"]).json()
    service = client.post(
        f"/v1/providers/{provider['id']}/services",
        json={"slug": "bad-output", "name": "Bad Output", "category": "compute",
              "runtime": "python", "entrypoint": "import sys; sys.exit(9)",
              "base_price_micros": 1000, "max_price_micros": 20_000,
              "max_runtime_seconds": 10},
        headers=consumer["headers"],
    ).json()
    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    failed = _run(client, consumer["headers"], service, {}).json()

    dispute = client.post("/v1/disputes",
                          json={"job_id": failed["job_id"], "reason": "non_delivery"},
                          headers=consumer["headers"]).json()
    resolved = client.post(f"/v1/disputes/{dispute['id']}/triage",
                           headers=consumer["headers"]).json()
    assert resolved["status"] == "resolved_consumer"
    assert resolved["auto_resolved"] is True
    assert "did not complete successfully" in resolved["resolution"]

    # the escrow was already returned when the job failed — no double refund
    assert resolved["refund_micros"] == 0
    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert after["available_micros"] == before["available_micros"]


def test_reputation_tracks_outcomes(client, consumer, services):
    provider_id = services["sha256-notary"]["provider_id"]
    before = client.get(f"/v1/providers/{provider_id}/stats").json()
    _run(client, consumer["headers"], services["sha256-notary"], {"text": "reputation"})
    after = client.get(f"/v1/providers/{provider_id}/stats").json()

    assert after["total_jobs"] == before["total_jobs"] + 1
    assert after["successful_jobs"] == before["successful_jobs"] + 1
    assert after["tier"] in ("platinum", "gold", "silver", "bronze", "probation")
    assert after["recent_events"]
