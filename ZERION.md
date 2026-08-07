# Zerion Onchain Intelligence on M2X

Zerion is registered on the M2X Compute & Tool Exchange as a **first-class
service provider**. An agent discovers it the same way it discovers any other
provider, pays for it the same way, and gets a result that is metered, hashed,
receipted and cleaned up the same way.

The whole point is that nothing about Zerion is special-cased. It is eight
priced services in the catalog with an executor that happens to run at Zerion
instead of in a sandbox container.

---

## 1. What the integration does

An agent can ask:

> "Analyze wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

and the exchange will:

| # | Step | Where |
|---|------|-------|
| 1 | Recognise the request as an on-chain lookup and extract the wallet | `app/agent/onchain.py` |
| 2 | Discover the Zerion capability in the Bazaar index | `app/bazaar/discovery.py` |
| 3 | Select the right capability (`wallet_analysis`, `pnl`, …) | `app/agent/planner.py` |
| 4 | Validate the address and check price, budget and quota | `integrations/zerion/{models,quota}.py` |
| 5 | Create a job and an x402 payment record | `app/services/jobs.py` |
| 6 | Escrow the consumer's payment in the mandated Algorand ASA | `app/x402/facilitator.py` |
| 7 | Authorize and settle the Zerion-side payment | `integrations/zerion/payment.py` |
| 8 | Call Zerion (HTTP API or CLI) | `integrations/zerion/{client,cli}.py` |
| 9 | Normalize the response into one stable M2X envelope | `integrations/zerion/normalizer.py` |
| 10 | SHA-256 the canonical JSON, verify the artifact manifest | `app/integrity.py` |
| 11 | Issue a hash-chained receipt naming **both** payment rails | `app/services/receipts.py` |
| 12 | Record usage, cost, latency, provider and payment metadata | `ZerionRequest` table |
| 13 | Return structured data plus a natural-language summary | `app/agent/graph.py` |
| 14 | Expire the cached result on its TTL | `app/services/scheduler.py` |

If Zerion fails at any point, the consumer's escrow is refunded automatically by
the existing job failure path — the same path a failed sandbox job takes.

### Capabilities

| Capability | Service slug | Zerion API | Zerion CLI |
|---|---|---|---|
| `wallet_analysis` | `zerion-wallet-analysis` | portfolio + positions + transactions + pnl | `zerion analyze` |
| `portfolio` | `zerion-portfolio` | `GET /v1/wallets/{address}/portfolio` | `zerion portfolio` |
| `positions` | `zerion-positions` | `GET /v1/wallets/{address}/positions/` | `zerion positions` |
| `defi_positions` | `zerion-defi-positions` | `…/positions/?filter[positions]=only_complex` | `zerion positions --defi` |
| `pnl` | `zerion-pnl` | `GET /v1/wallets/{address}/pnl` | `zerion pnl` |
| `transactions` | `zerion-transactions` | `GET /v1/wallets/{address}/transactions/` | `zerion history` |
| `token_search` | `zerion-token-search` | `GET /v1/fungibles/?filter[search_query]=` | `zerion search` |
| `chains` | `zerion-chains` | `GET /v1/chains/` | `zerion chains` |

---

## 2. Architecture

### Two payment rails, never mixed

This is the central design decision. The exchange's own payment network is
unchanged: **every consumer still pays M2X in Algorand ASA #10458941 over x402**,
escrowed and settled by the existing facilitator. Zerion charges on a different
network. So the two legs are modelled as two rails:

```
                   leg 1: PaymentRail.M2X_ALGORAND        leg 2: PaymentRail.ZERION_X402
                   x402 `exact`, ASA #10458941            x402, USDC on Base or Solana
   ┌─────────┐  ────────────────────────────────►  ┌─────┐  ──────────────────────────►  ┌────────┐
   │  Agent  │        escrow → settle → refund      │ M2X │      pay-per-request          │ Zerion │
   └─────────┘  ◄────────────────────────────────  └─────┘  ◄──────────────────────────  └────────┘
                        result + receipt                          normalized JSON
```

```
app/payments/rails.py
    PaymentRail                       M2X_ALGORAND | ZERION_X402 | API_KEY | NONE
    PaymentOutcome                    normalized, credential-free settlement metadata
    ExternalProviderPaymentAdapter    ABC: rail / available / quote_micros / pay
        └── ZerionX402PaymentAdapter  integrations/zerion/payment.py
```

The Algorand path was not modified. `app/x402/`, `app/algorand.py` and the
facilitator are untouched by this integration.

### External services

```
app/integrations/registry.py    provider-agnostic: 2 hooks into the job lifecycle
app/integrations/zerion/
    __init__.py       package exports
    models.py         capability catalog, address/chain validation, request spec
    client.py         HTTP transport (Basic auth, retries, rate limits)
    cli.py            subprocess transport (allowlisted commands, no shell)
    payment.py        rail/transport resolution + the x402 adapter
    normalizer.py     provider output → one M2X envelope + SHA-256 integrity
    quota.py          per-job / per-session / per-spend limits and budget checks
    demo.py           deterministic fixtures for credential-free environments
    service.py        the orchestrated request and the job executor
    registration.py   marketplace provider + service rows, executor wiring
    errors.py         structured, credential-safe error types
```

`app/services/jobs.py` gained exactly two provider-agnostic hooks:

1. **Payload validation before pricing** — an external service may reject a
   payload before a quote is committed, so a malformed wallet costs nothing.
2. **Execution dispatch** — if a service is external, its executor runs instead
   of `runner.run(...)`. Both return the same `ExecutionResult`, so metering,
   integrity, artifacts, settlement, receipts, reputation and cleanup are
   completely unchanged downstream.

### Normalized response

Every capability, on every transport, produces the same envelope:

```json
{
  "provider": "zerion",
  "wallet": "0xd8da6bf26964af9d7eed9e03e53415d37aa96045",
  "request_type": "wallet_analysis",
  "chain": "all",
  "currency": "usd",
  "timestamp": "2026-08-08T03:40:50.898Z",
  "source": "zerion_api | zerion_cli | zerion_demo",
  "data": { "portfolio": {...}, "positions": {...}, "pnl": {...},
            "transactions": {...}, "summary": "…" },
  "payment": {
    "rail": "zerion_x402",
    "status": "settled",
    "amount": "0.010000",
    "currency": "USDC",
    "network": "base",
    "transaction": "0x…",
    "settled": true
  },
  "integrity": {
    "algorithm": "sha256",
    "hash": "4b83f18167180de5451170fee51f204f…",
    "verified": true,
    "scope": "normalized_response",
    "note": "SHA-256 over the canonical JSON of this normalized response. It proves the integrity of what M2X received and stored; it is not a proof of on-chain truth."
  }
}
```

The hash covers the whole envelope except the `integrity` block itself. **It
proves the response was not altered after M2X received it. It does not prove
anything about the blockchain** — that distinction is stated in the envelope,
the receipt and the dashboard.

The untouched provider document is stored separately as the `zerion_raw.json`
artifact, so an audit can compare normalized against raw.

---

## 3. Mode A — Zerion API key

Free key from [dashboard.zerion.io](https://dashboard.zerion.io); keys start
with `zk_`. Authentication is HTTP Basic with the key as the username and an
empty password, exactly as Zerion documents.

```bash
ZERION_ENABLED=true
ZERION_API_KEY=zk_your_key_here
ZERION_USE_X402=false
```

- Rail reported: `api_key`; the payment block says `not_required`, because
  billing is by subscription rather than per request.
- Retries are enabled (429 / 202 / 5xx / timeouts) with exponential backoff,
  because a retry on this rail costs nothing.
- Rate-limit headers (`RateLimit-Org-Second-*`, `-Day-*`, `-Month-*`) are read;
  a persistent 429 surfaces as a structured `zerion_rate_limited` error.

## 4. Mode B — Zerion CLI

```bash
npm install -g zerion-cli     # requires Node.js 20+
zerion --version
```

```bash
ZERION_TRANSPORT=cli
ZERION_CLI_COMMAND=zerion
```

The CLI is executed as an argument array with `shell=False`, from a
`shutil.which`-resolved path, with a wall-clock timeout, `stdin` set to
`DEVNULL`, a scrubbed environment, and only the subcommands in the capability
allowlist. Output is JSON on stdout; structured errors on stderr are mapped onto
typed exceptions by their `code` field.

## 5. Mode C — Zerion x402 (pay-per-request)

Zerion's x402 rail settles in **USDC on Base or Solana** and needs **no Zerion
API key**. Payment is performed by the Zerion CLI's `--x402` flag, which is the
integration path Zerion documents as handling the signing and the 402 retry for
you — so signing keys stay inside a tool built to hold them and never enter this
codebase's request path.

```bash
npm install -g zerion-cli
```

```bash
ZERION_ENABLED=true
ZERION_USE_X402=true
ZERION_EVM_PRIVATE_KEY=0x…          # Base
# or
ZERION_SOLANA_PRIVATE_KEY=…         # base58, Solana
ZERION_X402_PREFER_SOLANA=false
```

In x402 mode the API key is deliberately **withheld** from the CLI subprocess,
so it cannot silently fall back to subscription billing when you asked to pay
per request.

A clean CLI exit with `--x402` is the settlement evidence, and that is what the
receipt records (`"evidence": "cli_exit_ok"`). If the CLI surfaces a transaction
id, it is carried through verbatim (`"evidence": "cli_reported_tx"`). **A
transaction id is never fabricated.**

> **If `ZERION_USE_X402=true` but the CLI is not installed**, the exchange does
> not pretend: it reports `zerion_payment_failed` with an actionable message, or
> falls back to demo mode when `ZERION_DEMO_MODE=true`. There is no code path
> that claims a settled payment that did not happen.

## 6. Mode D — Demo (default with no credentials)

With no credential configured, the exchange serves deterministic fixtures so the
whole path still runs on a bare machine. Demo mode is honest about itself:

- results are stamped `"source": "zerion_demo"`;
- the payment block says `"status": "simulated"`, `"settled": false`,
  `"amount": "0"`;
- the dashboard and the API both say so in words;
- the figures tie out (positions and PnL are derived from the portfolio total),
  but they are generated locally and are **not real on-chain data**.

Set `ZERION_DEMO_MODE=false` to make a credential-less deployment fail loudly
instead.

---

## 7. Environment variables

All are optional; every one has a working default. Both the unprefixed
`ZERION_*` names (matching Zerion's own tooling) and the `M2X_ZERION_*` forms
are accepted.

| Variable | Default | Purpose |
|---|---|---|
| `ZERION_ENABLED` | `true` | Master switch |
| `ZERION_API_BASE_URL` | `https://api.zerion.io` | API host |
| `ZERION_API_KEY` | *(empty)* | **Secret.** `zk_…` key for API-key mode |
| `ZERION_USE_X402` / `ZERION_X402` | `false` | Enable pay-per-request |
| `ZERION_EVM_PRIVATE_KEY` | *(empty)* | **Secret.** x402 signing key (Base) |
| `ZERION_SOLANA_PRIVATE_KEY` | *(empty)* | **Secret.** x402 signing key (Solana) |
| `ZERION_X402_PREFER_SOLANA` | `false` | Prefer Solana when both keys exist |
| `ZERION_TRANSPORT` | `auto` | `auto` \| `api` \| `cli` |
| `ZERION_CLI_COMMAND` | `zerion` | CLI binary name |
| `ZERION_TIMEOUT_SECONDS` | `15` | Per-request timeout |
| `ZERION_MAX_RETRIES` | `2` | API-key mode only; never on a paid rail |
| `ZERION_DEFAULT_CHAIN` | `ethereum` | Default chain hint |
| `ZERION_ALLOWED_CHAINS` | *(empty = all)* | Comma-separated chain allowlist |
| `ZERION_CURRENCY` | `usd` | Quote currency |
| `ZERION_HISTORY_LIMIT` | `20` | Default transaction page size |
| `ZERION_DEMO_MODE` | `true` | Serve fixtures when no credential exists |
| `ZERION_MAX_REQUESTS_PER_JOB` | `5` | Per-job quota |
| `ZERION_MAX_REQUESTS_PER_SESSION` | `25` | Per-principal quota, per window |
| `ZERION_MAX_SPEND_MICROS` | `2000000` | Provider spend cap, per window |
| `ZERION_QUOTA_WINDOW_SECONDS` | `3600` | Rolling quota window |
| `ZERION_COST_MICROS` | `10000` | Cost of one Zerion call (0.01 USDC) |
| `ZERION_PRICE_MICROS` | `15000` | What the exchange charges the consumer |
| `ZERION_RESULT_TTL_SECONDS` | `86400` | Cached-result TTL |

Never commit real values. `.env.example` ships with every secret blank.

---

## 8. Running

```bash
pip install -r backend/requirements.txt
```

```bash
cp .env.example .env
```

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
```

Then open <http://localhost:8000/dashboard#zerion>. Sign in with the seeded
agent account (`agent@m2x.local` / `demo-password-123`).

No new Python dependencies were added. The integration uses `httpx` (already
required) and the standard library's `subprocess`. The Zerion CLI is an optional
Node.js tool, needed only for the x402 rail.

---

## 9. Example agent prompts

These route to Zerion:

```
Analyze wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
Show portfolio of vitalik.eth
What tokens does 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 hold?
Show DeFi positions for 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
Show last 20 transactions of 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 on base
How much profit or loss has 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 made?
Analyze wallet 0xd8dA…045, then show its DeFi positions, then show its PnL
```

The last one demonstrates **wallet carry-over**: the address named once applies
to every later step of the plan.

These do **not** route to Zerion, by design:

```
Hash this text with sha256              → local compute
Compute the primes below 20000          → local compute
Analyze this text and summarize it      → text service, not a wallet lookup
Show me DeFi positions                  → on-chain wording, but no wallet to look up
```

The gate is deliberate: **a wallet identifier must be resolvable** before a step
can reach a paid data provider. Keyword enthusiasm alone is not enough.

---

## 10. Example API calls

```bash
curl -s localhost:8000/v1/zerion/status | jq
```

```bash
curl -s localhost:8000/v1/zerion/capabilities | jq '.items[] | {capability, price_micros, provider_payment_rail}'
```

Log in and keep the token:

```bash
TOKEN=$(curl -s localhost:8000/v1/auth/login -H 'content-type: application/json' -d '{"email":"agent@m2x.local","password":"demo-password-123"}' | jq -r .access_token)
```

Quote without paying:

```bash
curl -s localhost:8000/v1/zerion/preview -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"capability":"portfolio","wallet":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}' | jq
```

Run the full paid path:

```bash
curl -s localhost:8000/v1/zerion/query -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"capability":"wallet_analysis","wallet":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}' | jq '{summary, consumer_payment, provider_payment, integrity, receipt, telemetry}'
```

Buy the same capability through the canonical x402 endpoint (no Zerion-specific
client code — it is an ordinary catalog service, so the first call returns 402
with payment requirements and the retry carries `X-PAYMENT`):

```bash
curl -si localhost:8000/v1/services/$SERVICE_ID/invoke -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"wallet":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

Run it through the agent:

```bash
curl -s localhost:8000/v1/plans -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"goal":"Analyze wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045, then show its DeFi positions","budget_micros":2000000}' | jq '.result.summary'
```

Telemetry and audit:

```bash
curl -s localhost:8000/v1/zerion/requests -H "authorization: Bearer $TOKEN" | jq
```

```bash
curl -s localhost:8000/v1/zerion/verify/$JOB_ID -H "authorization: Bearer $TOKEN" | jq
```

Full endpoint list: `/v1/zerion/{status,capabilities,register,quota,query,preview,requests,requests/{id},stats,verify/{job_id},payments}`.

MCP tool (for any MCP-capable agent):

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"onchain_intelligence",
           "arguments":{"capability":"pnl","wallet":"vitalik.eth"}}}
```

---

## 11. Example CLI commands

What the integration runs on your behalf in CLI mode:

```bash
zerion analyze 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 --json --x402
```

```bash
zerion portfolio vitalik.eth --json
```

```bash
zerion positions 0xd8dA…045 --positions defi --defi --json --x402
```

```bash
zerion history 0xd8dA…045 --limit 20 --chain base --json --x402
```

```bash
zerion pnl 0xd8dA…045 --json --x402
```

```bash
zerion search USDC --json
```

```bash
zerion chains --json
```

---

## 12. Security notes

**Credentials**

- The Zerion API key and both wallet private keys are read from settings only,
  are never written to a database row, never returned by any endpoint, never
  serialized into a job result, receipt or audit entry, and never logged.
- `/v1/config`, `/health` and `/v1/zerion/status` report **presence booleans
  only** (`api_key_configured`, `x402_keys_configured`) — there is no code path
  that can serve a value. This is asserted by a test that configures real-looking
  secrets and then greps the whole config response for them.
- Provider error text passes through `sanitize()`, which redacts `zk_…` keys,
  32-byte hex private keys, `Authorization:` headers and `key=`/`secret=` pairs
  before anything is logged or returned.
- Credentials are forwarded to the CLI subprocess (it cannot work without them)
  through an explicit, minimal environment — the child never inherits the parent
  environment wholesale.

**Command execution**

- Arguments are passed as a list with `shell=False`. There is no string command
  anywhere in the integration.
- The binary is resolved with `shutil.which`; users cannot choose it.
- Only the fixed subcommands in `ALLOWED_CLI_COMMANDS` may run, selected from the
  capability table by key — never assembled from user input.
- Every argument value passed a strict regex (`^0x[0-9a-fA-F]{40}$`, base58,
  `.eth`, `^[a-z0-9-]{1,32}$` for chains) before construction, so injection
  attempts are rejected at validation and no process starts at all.
- Wall-clock timeouts always apply; `stdin` is `DEVNULL`, so the CLI can never
  block on an interactive prompt.

**Spending**

- Quotas are enforced *before* authorization, and each one raises rather than
  returning a flag, so there is no path where a check is evaluated and ignored.
- Failed and rejected attempts are recorded and **count against quota**, so a
  failing loop still runs into the limit.
- A paid rail is never auto-retried; and a non-retryable failure (bad input,
  exhausted quota, unconfigured provider) does not schedule a job retry, because
  re-running it would spend the consumer's money again for the same answer.
- An invalid wallet is rejected before a payment record exists.

---

## 13. Demo instructions

1. Start the server and open <http://localhost:8000/dashboard>. The header shows
   a **Zerion** status pill with the active mode.
2. **Discovery** tab → search `wallet onchain zerion`. Eight Zerion listings
   appear in the Bazaar index, each marked payable in the mandated ASA, each
   carrying its provider rail, chains, quota and schemas.
3. **Onchain Intelligence** tab → shows provider status, connected mode, the
   eight capabilities with prices and rails, and live telemetry.
4. Enter a wallet (`0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045` is prefilled),
   pick **Wallet Analysis**, click **Quote (free)** — the address is validated
   and priced with nothing charged.
5. Click **Analyze — pay & run**. The result panel shows:
   - a natural-language summary;
   - **Payment — two rails**: consumer→M2X settled in ASA #10458941 with its tx
     hash, and M2X→Zerion with its rail, amount, network and transaction;
   - the response SHA-256, the job output hash, manifest verification and the
     hash-chained receipt number;
   - latency, upstream Zerion call count and provider cost;
   - portfolio value, token positions, DeFi positions, PnL and transactions;
   - the full normalized envelope and the post-request quota.
6. **Agent Planner** tab → run
   `Analyze wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045, then show its DeFi positions`.
   The trace shows discover → quote → pay → execute → verify → settle for each
   step, with Zerion selected by the planner.
7. **Receipts** tab → open the newest receipt. Its body contains an
   `external_provider` block naming Zerion, the provider rail, whether that leg
   settled, and the response hash. Click verify: the hash chain still validates.

---

## 14. Tests

```bash
python -m pytest backend/tests -q
```

```bash
python -m pytest backend/tests/test_zerion.py -q
```

95 Zerion tests cover configuration and secret non-disclosure, wallet/chain/
capability validation including injection attempts, discovery and catalog
registration, capability routing, the API client (Basic auth, documented paths,
retries, rate limits, auth failure, timeouts, outages, fan-out with a failing
leg), the CLI client (argument vector, no shell, x402 env handling, JSON
parsing, structured errors, timeouts, absence), normalization of both shapes,
integrity hashing and tamper detection, the payment adapter on all four modes,
quotas and budgets, provider outage and refund, retry suppression, the full
mocked end-to-end flow, receipts, artifacts, the dashboard API, authorization,
and agent intent routing (positive and negative).

**No test spends money or touches the network.** `httpx` and `subprocess.run`
are stubbed; the x402 rail is driven through the adapter's evidence contract.

### Optional live test

```bash
ZERION_LIVE_TEST=1 ZERION_API_KEY=zk_… python -m pytest backend/tests/test_zerion.py -q -k live -s
```

Skipped unless `ZERION_LIVE_TEST` is set. With an API key it costs nothing. With
`ZERION_USE_X402=true` plus a signing key and the CLI installed it spends **real
USDC** (~0.01 per request) — which is why it is opt-in and never runs in CI.
Override the wallet with `ZERION_LIVE_WALLET`.

---

## 15. Limitations

- **x402 requires the Zerion CLI.** Zerion's own docs name the CLI as the
  integration that handles x402 signing and retries; the exchange uses it rather
  than reimplementing EIP-3009 signing against an endpoint it cannot test. With
  `ZERION_TRANSPORT=api` and x402 enabled, the exchange reports the rail as
  unavailable rather than attempting an unsigned request.
- **x402 settlement evidence is the CLI's exit status** unless the CLI surfaces a
  transaction id, in which case that id is carried through. The receipt records
  which of the two it was.
- **The integrity hash is not a blockchain proof.** It proves the response was
  not altered after M2X received it. Stated everywhere it appears.
- **The MPP rail on Tempo is not implemented** — the CLI supports it, but the
  exchange exposes only the API-key and x402 rails.
- **Demo mode data is synthetic.** Internally consistent and clearly labelled,
  but not real on-chain data.
- Zerion notes that Solana addresses do not currently support protocol
  positions, and that PnL is unavailable for wallets above ~1M transactions.
  Both surface as ordinary structured provider errors.
