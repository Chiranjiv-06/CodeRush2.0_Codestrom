"""Agent planner, MCP, A2A, discovery, scheduler and integrity primitives."""
from __future__ import annotations

import base64
import json

import pytest

from app.integrity import build_manifest, chain_hash, hash_object, sign, verify_manifest, verify_signature
from app.metering import Usage, price_for_usage
from app.services.cron import CronError, matches, next_fire_time, parse
from app.x402.protocol import build_exact_payload, decode_payment_header, encode_payment_header


# --------------------------------------------------------------------------- #
# Integrity primitives
# --------------------------------------------------------------------------- #
def test_canonical_hashing_is_key_order_independent():
    assert hash_object({"a": 1, "b": [2, 3]}) == hash_object({"b": [2, 3], "a": 1})
    assert hash_object({"a": 1}) != hash_object({"a": 2})


def test_manifest_detects_tampering():
    files = {"out.txt": b"hello", "data.bin": b"\x00\x01"}
    manifest = build_manifest(files)
    ok, problems = verify_manifest(manifest, files)
    assert ok and not problems

    ok, problems = verify_manifest(manifest, {**files, "out.txt": b"hell0"})
    assert not ok
    assert "mismatch:out.txt" in problems

    ok, problems = verify_manifest(manifest, {"out.txt": b"hello"})
    assert not ok
    assert "missing:data.bin" in problems


def test_signatures_and_chain_links():
    payload = "abc"
    signature = sign(payload)
    assert verify_signature(payload, signature)
    assert not verify_signature("abd", signature)
    assert chain_hash("0" * 64, "a" * 64) != chain_hash("1" * 64, "a" * 64)


def test_price_scales_with_metered_usage():
    class FakeService:
        base_price_micros = 1000
        price_per_cpu_second_micros = 500
        price_per_mb_egress_micros = 10
        max_price_micros = 1_000_000

    cheap = price_for_usage(FakeService, Usage(cpu_ms=100, egress_bytes=1024))
    dear = price_for_usage(FakeService, Usage(cpu_ms=10_000, egress_bytes=10 * 1024 * 1024))
    assert dear.capped_micros > cheap.capped_micros
    assert cheap.platform_fee_micros < cheap.capped_micros
    assert cheap.provider_net_micros + cheap.platform_fee_micros == cheap.capped_micros

    capped = price_for_usage(FakeService, Usage(cpu_ms=10_000_000))
    assert capped.capped_micros == FakeService.max_price_micros


# --------------------------------------------------------------------------- #
# x402 header codec
# --------------------------------------------------------------------------- #
def test_payment_header_roundtrip():
    payload = build_exact_payload(
        payer="usr_1", pay_to="usr_2", value_micros=1234, nonce="ab12",
        resource="/v1/services/svc_1/invoke", payer_secret="s3cret",
    )
    header = encode_payment_header(payload)
    decoded = decode_payment_header(header)
    assert decoded.scheme == "exact"
    assert decoded.payload["authorization"]["value"] == "1234"
    assert decoded.payload["signature"]


def test_malformed_payment_headers_are_rejected():
    from app.x402.protocol import X402Error

    with pytest.raises(X402Error):
        decode_payment_header("")
    with pytest.raises(X402Error):
        decode_payment_header("not-base64!!")
    with pytest.raises(X402Error):
        decode_payment_header(base64.b64encode(b'{"scheme":"exact"}').decode())


# --------------------------------------------------------------------------- #
# Cron
# --------------------------------------------------------------------------- #
def test_cron_parsing_and_scheduling():
    minute, hour, *_ = parse("*/15 9-17 * * 1-5")
    assert minute == {0, 15, 30, 45}
    assert hour == set(range(9, 18))

    assert parse("@daily")[1] == {0}

    from datetime import datetime, timezone

    moment = datetime(2026, 8, 7, 9, 15, tzinfo=timezone.utc)  # a Friday
    assert matches("*/15 9-17 * * 1-5", moment)
    assert not matches("0 0 * * *", moment)

    nxt = next_fire_time("*/15 * * * *", moment)
    assert nxt.minute in (30,)

    with pytest.raises(CronError):
        parse("bogus expression here now")
    with pytest.raises(CronError):
        parse("99 * * * *")


# --------------------------------------------------------------------------- #
# Bazaar discovery
# --------------------------------------------------------------------------- #
def test_local_services_are_published_to_the_discovery_index(client):
    status = client.get("/v1/bazaar/status").json()
    assert status["enabled"] is True
    assert status["counts"]["local"] > 0
    assert status["extension"] == "@x402-avm/extensions"

    listings = client.get("/v1/bazaar/listings", params={"source": "local"}).json()
    assert listings["count"] > 0
    item = listings["items"][0]
    assert item["accepts"][0]["scheme"] == "exact"
    assert item["accepts"][0]["maxAmountRequired"]
    assert item["resource"].startswith("/v1/services/")


def test_remote_bazaar_failure_degrades_without_breaking(client, consumer):
    """The configured Bazaar endpoint is unreachable in tests — discovery must survive."""
    refreshed = client.post("/v1/bazaar/refresh", params={"force": True},
                            headers=consumer["headers"])
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["local_published"] > 0
    assert body["remote"]["degraded"] is True
    assert client.get("/v1/bazaar/listings").json()["count"] > 0


def test_discovery_picks_the_best_value_listing(client):
    best = client.get("/v1/bazaar/best",
                      params={"capability": "hash sha256 digest", "budget_micros": 1_000_000}).json()
    assert best["match"] is not None
    assert best["match"]["price_micros"] <= 1_000_000


def test_remote_listing_payload_normalization():
    from app.bazaar.discovery import normalize_payload

    records = normalize_payload({
        "items": [
            {"resource": "https://x.example/api", "accepts": [{"scheme": "exact",
                                                               "network": "algorand-testnet",
                                                               "maxAmountRequired": "5000"}]},
            {"no": "accepts"},
        ]
    })
    assert len(records) == 1
    assert records[0]["resource"] == "https://x.example/api"

    assert normalize_payload([]) == []
    assert normalize_payload({"unexpected": True}) == []


# --------------------------------------------------------------------------- #
# Agent planner
# --------------------------------------------------------------------------- #
def test_planner_decomposes_and_matches_capabilities(client, consumer):
    preview = client.post(
        "/v1/plans/preview",
        json={"goal": "hash this payload, then analyze the text statistics"},
        headers=consumer["headers"],
    )
    assert preview.status_code == 200
    body = preview.json()
    assert len(body["steps"]) == 2
    assert body["steps"][0]["capability"] == "hash"
    assert body["steps"][0]["candidates"]
    assert body["engine"] in ("builtin", "langgraph")


def test_agent_executes_a_multi_step_plan_and_pays_per_step(client, consumer):
    before = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    plan = client.post(
        "/v1/plans",
        json={"goal": "hash the payload, then analyze the resulting text",
              "budget_micros": 400_000},
        headers=consumer["headers"],
    )
    assert plan.status_code == 201, plan.text
    body = plan.json()

    assert body["status"] == "completed"
    assert len(body["steps"]) == 2
    assert all(step["status"] == "succeeded" for step in body["steps"])
    assert all(step["job_id"] for step in body["steps"])

    # every graph node is recorded in the trace
    nodes = {entry["node"] for entry in body["trace"]}
    assert {"plan", "discover", "quote", "pay", "execute", "verify", "settle", "report"} <= nodes

    # step 1's output was fed into step 2's job payload
    digest = body["steps"][0]["output"]["digest"]
    assert len(digest) == 64
    downstream = client.get(f"/v1/jobs/{body['steps'][1]['job_id']}",
                            headers=consumer["headers"]).json()
    assert downstream["payload"]["input"]["digest"] == digest
    assert body["steps"][1]["output"] is not None

    assert 0 < body["spent_micros"] <= body["budget_micros"]
    after = client.get("/v1/auth/balance", headers=consumer["headers"]).json()
    assert before["available_micros"] - after["available_micros"] == body["spent_micros"]


def test_agent_stops_when_the_budget_cannot_cover_a_step(client, consumer):
    plan = client.post(
        "/v1/plans",
        json={"goal": "hash this then analyze it then build a report", "budget_micros": 1},
        headers=consumer["headers"],
    ).json()
    assert plan["spent_micros"] == 0
    assert plan["status"] in ("failed", "partial")
    assert all(step["status"] in ("unmatched", "over_budget", "skipped")
               for step in plan["steps"])


def test_plans_are_private_to_their_owner(client, consumer):
    plan = client.post("/v1/plans", json={"goal": "hash this payload", "budget_micros": 100_000},
                       headers=consumer["headers"]).json()
    other = client.post("/v1/auth/register",
                        json={"email": "nosy-agent@test.local", "password": "password-12345"}).json()
    resp = client.get(f"/v1/plans/{plan['id']}",
                      headers={"Authorization": f"Bearer {other['access_token']}"})
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
def rpc(client, headers, method, params=None, rpc_id=1):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": rpc_id, "method": method,
                                     "params": params or {}}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_mcp_initialize_and_tool_listing(client, consumer):
    init = rpc(client, consumer["headers"], "initialize")["result"]
    assert init["protocolVersion"]
    assert init["serverInfo"]["name"] == "m2x-exchange"

    tools = {t["name"] for t in rpc(client, consumer["headers"], "tools/list")["result"]["tools"]}
    assert {"discover_services", "get_quote", "run_job", "verify_receipt",
            "plan_and_execute", "wallet_balance"} <= tools


def test_mcp_run_job_pays_and_returns_verified_output(client, consumer, services):
    result = rpc(client, consumer["headers"], "tools/call", {
        "name": "run_job",
        "arguments": {"service_id": services["sha256-notary"]["id"],
                      "payload": {"text": "mcp calling"}},
    })["result"]

    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["status"] == "succeeded"
    assert len(structured["result"]["digest"]) == 64
    assert structured["charged_micros"] > 0
    assert structured["integrity_verified"] is True
    assert json.loads(result["content"][0]["text"])["job_id"] == structured["job_id"]


def test_mcp_resources_and_errors(client, consumer):
    resources = rpc(client, consumer["headers"], "resources/list")["result"]["resources"]
    assert {r["uri"] for r in resources} >= {"m2x://services", "m2x://receipts/chain"}

    read = rpc(client, consumer["headers"], "resources/read",
               {"uri": "m2x://services"})["result"]
    assert json.loads(read["contents"][0]["text"])

    missing = rpc(client, consumer["headers"], "tools/call", {"name": "nope", "arguments": {}})
    assert missing["error"]["code"] == -32602

    bad_method = rpc(client, consumer["headers"], "does/not/exist")
    assert bad_method["error"]["code"] == -32601


def test_mcp_requires_authentication(client):
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"}).status_code == 401


# --------------------------------------------------------------------------- #
# A2A
# --------------------------------------------------------------------------- #
def test_a2a_agent_card_is_published(client):
    card = client.get("/.well-known/agent.json").json()
    assert card["protocolVersion"]
    assert card["preferredTransport"] == "JSONRPC"
    assert any(skill["id"] == "execute-goal" for skill in card["skills"])
    assert client.get("/.well-known/agent-card.json").status_code == 200


def test_a2a_message_send_runs_a_task(client, consumer):
    resp = client.post("/a2a", json={
        "jsonrpc": "2.0", "id": "task-1", "method": "message/send",
        "params": {
            "message": {"role": "user", "messageId": "m1",
                        "parts": [{"kind": "text", "text": "hash this payload"}]},
            "metadata": {"budgetMicros": 200_000},
        },
    }, headers=consumer["headers"])
    assert resp.status_code == 200, resp.text
    task = resp.json()["result"]

    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["metadata"]["spentMicros"] > 0
    assert task["metadata"]["jobs"]
    assert task["metadata"]["receipts"]
    assert task["artifacts"]

    fetched = client.post("/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                                        "params": {"id": task["id"]}},
                          headers=consumer["headers"]).json()["result"]
    assert fetched["id"] == task["id"]


def test_a2a_rejects_unknown_methods_and_missing_goals(client, consumer):
    unknown = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "bogus"},
                          headers=consumer["headers"]).json()
    assert unknown["error"]["code"] == -32601

    empty = client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
                                      "params": {"message": {"parts": []}}},
                        headers=consumer["headers"]).json()
    assert empty["error"]["code"] == -32602


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def test_scheduler_executes_queued_jobs(client, consumer, services):
    """A job paid via the async path is picked up and run by the scheduler."""
    created = client.post("/v1/jobs", json={
        "service_id": services["text-analyzer"]["id"],
        "payload": {"text": "scheduled work runs itself"},
        "auto_pay": True,
    }, headers=consumer["headers"])
    assert created.status_code == 201, created.text
    job_id = created.json()["job"]["id"]
    assert created.json()["job"]["status"] == "queued"

    from app.services.scheduler import scheduler

    result = scheduler.tick(slow_pass=True)
    assert result["jobs_run"] >= 1

    job = client.get(f"/v1/jobs/{job_id}", headers=consumer["headers"]).json()
    assert job["status"] == "succeeded"
    assert job["final_price_micros"] > 0
    assert job["integrity_verified"] is True


def test_recurring_schedule_fires_and_reschedules(client, consumer, services):
    schedule = client.post("/v1/schedules", json={
        "name": "heartbeat",
        "service_id": services["sha256-notary"]["id"],
        "payload": {"text": "heartbeat"},
        "interval_seconds": 5,
    }, headers=consumer["headers"])
    assert schedule.status_code == 201, schedule.text
    schedule_id = schedule.json()["id"]

    # make it due right now
    from datetime import datetime, timedelta, timezone

    from app.db import session_scope
    from app.models import Schedule

    with session_scope() as db:
        row = db.get(Schedule, schedule_id)
        row.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    from app.services.scheduler import scheduler

    result = scheduler.tick(slow_pass=False)
    assert result["schedules_fired"] >= 1

    after = client.get("/v1/schedules", headers=consumer["headers"]).json()
    fired = next(s for s in after if s["id"] == schedule_id)
    assert fired["run_count"] == 1
    assert fired["last_job_id"]
    assert fired["next_run_at"] is not None

    client.delete(f"/v1/schedules/{schedule_id}", headers=consumer["headers"])


def test_cron_schedule_validation(client, consumer, services):
    ok = client.get("/v1/schedules/cron/validate", params={"expression": "*/5 * * * *"}).json()
    assert ok["valid"] is True and ok["next_fire_at"]

    bad = client.get("/v1/schedules/cron/validate", params={"expression": "not a cron"}).json()
    assert bad["valid"] is False

    rejected = client.post("/v1/schedules", json={
        "service_id": services["sha256-notary"]["id"], "cron": "99 99 * * *",
    }, headers=consumer["headers"])
    assert rejected.status_code == 400


def test_expired_payment_windows_are_swept(client, consumer, services):
    challenge = client.post(f"/v1/services/{services['sha256-notary']['id']}/invoke",
                            json={"text": "will expire"}, headers=consumer["headers"]).json()

    from datetime import datetime, timedelta, timezone

    from app.db import session_scope
    from app.models import Payment

    with session_scope() as db:
        payment = db.get(Payment, challenge["payment_id"])
        payment.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    from app.services.scheduler import scheduler

    assert scheduler.tick(slow_pass=False)["payments_expired"] >= 1

    job = client.get(f"/v1/jobs/{challenge['job_id']}", headers=consumer["headers"]).json()
    assert job["status"] == "cancelled"

    payment = client.get(f"/v1/payments/{challenge['payment_id']}",
                         headers=consumer["headers"]).json()
    assert payment["status"] == "expired"


def test_cleanup_reaps_workspaces_and_expired_artifacts(client, admin_headers):
    result = client.post("/v1/admin/cleanup", headers=admin_headers)
    assert result.status_code == 200
    body = result.json()
    assert set(body) == {"artifacts_deleted", "workers_reaped", "workspaces_removed",
                         "containers_removed", "external_results_expired"}


def test_admin_only_endpoints_are_guarded(client, consumer):
    assert client.post("/v1/admin/cleanup", headers=consumer["headers"]).status_code == 403
    assert client.get("/v1/admin/audit", headers=consumer["headers"]).status_code == 403


def test_platform_stats_aggregate_the_exchange(client):
    stats = client.get("/v1/stats").json()
    assert stats["services"] > 0
    assert stats["jobs"]["total"] > 0
    assert stats["payments"]["settled_micros"] > 0
    assert stats["receipts"]["total"] > 0
    assert stats["leaderboard"]
    assert stats["bazaar"]["counts"]["local"] > 0
