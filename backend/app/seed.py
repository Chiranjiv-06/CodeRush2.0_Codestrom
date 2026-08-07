"""Demo seed data: operators, providers, priced services, discovery index.

Idempotent — safe to run on every boot. Service entrypoints are real programs
executed inside the sandbox; they read the job payload from stdin/input.json and
print a JSON result.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .bazaar.discovery import publish_local_services
from .config import settings
from .integrity import sha256_hex
from .models import Provider, Role, Service, User
from .security import hash_password
from .services import ledger

log = logging.getLogger("m2x.seed")

PRELUDE = '''\
import json, sys

def load():
    raw = sys.stdin.read()
    if not raw.strip():
        with open("input.json", "r", encoding="utf-8") as fh:
            raw = fh.read()
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}

def emit(obj):
    print(json.dumps(obj, default=str))

def text_of(payload):
    for key in ("text", "content", "body", "data"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    upstream = payload.get("input")
    if isinstance(upstream, str):
        return upstream
    if isinstance(upstream, dict):
        for key in ("text", "digest", "summary", "result", "output"):
            if isinstance(upstream.get(key), str):
                return upstream[key]
        return json.dumps(upstream, sort_keys=True)
    return payload.get("goal", "")

def numbers_of(payload):
    for key in ("numbers", "values", "series"):
        value = payload.get(key)
        if isinstance(value, list):
            return [float(v) for v in value if isinstance(v, (int, float))]
    upstream = payload.get("input")
    if isinstance(upstream, dict):
        for key in ("numbers", "values", "series"):
            if isinstance(upstream.get(key), list):
                return [float(v) for v in upstream[key] if isinstance(v, (int, float))]
        derived = [v for v in upstream.values() if isinstance(v, (int, float))]
        if derived:
            return [float(v) for v in derived]
    if isinstance(upstream, list):
        return [float(v) for v in upstream if isinstance(v, (int, float))]
    import re
    found = re.findall(r"-?\\d+(?:\\.\\d+)?", str(payload.get("goal", "")))
    return [float(f) for f in found]
'''

SERVICES: list[dict] = [
    {
        "provider": "quantum-forge",
        "slug": "sha256-notary",
        "name": "SHA-256 Notary",
        "category": "hash",
        "description": "Hashes a payload with SHA-256 and returns a notarized digest with length metadata.",
        "tags": ["hash", "sha256", "integrity", "notary"],
        "base_price_micros": 800,
        "price_per_cpu_second_micros": 200,
        "max_price_micros": 40_000,
        "max_runtime_seconds": 20,
        "entrypoint": PRELUDE + '''
import hashlib

payload = load()
text = text_of(payload)
digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
emit({
    "digest": digest,
    "algorithm": "sha256",
    "input_bytes": len(text.encode("utf-8")),
    "short": digest[:16],
    "text": digest,
})
''',
    },
    {
        "provider": "quantum-forge",
        "slug": "text-analyzer",
        "name": "Text Analyzer",
        "category": "text",
        "description": "Word, sentence and vocabulary statistics with the most frequent terms.",
        "tags": ["text", "analyze", "statistics", "summary", "words"],
        "base_price_micros": 1500,
        "price_per_cpu_second_micros": 400,
        "max_price_micros": 60_000,
        "max_runtime_seconds": 25,
        "entrypoint": PRELUDE + '''
import re
from collections import Counter

payload = load()
text = text_of(payload)
words = re.findall(r"[A-Za-z0-9']+", text.lower())
sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
counter = Counter(words)
emit({
    "characters": len(text),
    "words": len(words),
    "unique_words": len(counter),
    "sentences": len(sentences),
    "avg_word_length": round(sum(len(w) for w in words) / len(words), 3) if words else 0,
    "top_words": [{"word": w, "count": c} for w, c in counter.most_common(10)],
    "summary": f"{len(words)} words, {len(counter)} unique, {len(sentences)} sentences",
    "text": text[:500],
})
''',
    },
    {
        "provider": "quantum-forge",
        "slug": "stats-engine",
        "name": "Statistics Engine",
        "category": "analyze",
        "description": "Descriptive statistics (mean, median, stdev, quartiles) over a numeric series.",
        "tags": ["analyze", "statistics", "math", "report", "aggregate"],
        "base_price_micros": 1200,
        "price_per_cpu_second_micros": 350,
        "max_price_micros": 50_000,
        "max_runtime_seconds": 25,
        "entrypoint": PRELUDE + '''
import statistics as st

payload = load()
values = numbers_of(payload)
if not values:
    values = [float(len(text_of(payload)))]
values_sorted = sorted(values)
n = len(values)
emit({
    "count": n,
    "sum": sum(values),
    "mean": round(st.mean(values), 6),
    "median": round(st.median(values), 6),
    "stdev": round(st.pstdev(values), 6) if n > 1 else 0.0,
    "min": values_sorted[0],
    "max": values_sorted[-1],
    "p25": values_sorted[max(int(n * 0.25) - 1, 0)],
    "p75": values_sorted[max(int(n * 0.75) - 1, 0)],
    "numbers": values,
    "summary": f"n={n} mean={round(st.mean(values), 4)}",
})
''',
    },
    {
        "provider": "mesh-labs",
        "slug": "prime-sieve",
        "name": "Prime Sieve",
        "category": "compute",
        "description": "CPU-bound sieve of Eratosthenes; returns primes below a bound plus timing.",
        "tags": ["compute", "math", "primes", "cpu"],
        "base_price_micros": 2000,
        "price_per_cpu_second_micros": 1200,
        "max_price_micros": 120_000,
        "max_runtime_seconds": 40,
        "entrypoint": PRELUDE + '''
import time

payload = load()
limit = payload.get("limit")
if not isinstance(limit, int):
    nums = numbers_of(payload)
    limit = int(nums[0]) if nums else 50_000
limit = max(10, min(limit, 2_000_000))

started = time.perf_counter()
sieve = bytearray([1]) * (limit + 1)
sieve[0:2] = b"\\x00\\x00"
for i in range(2, int(limit ** 0.5) + 1):
    if sieve[i]:
        sieve[i * i:: i] = bytearray(len(sieve[i * i:: i]))
primes = [i for i, flag in enumerate(sieve) if flag]
elapsed = time.perf_counter() - started

emit({
    "limit": limit,
    "prime_count": len(primes),
    "largest_prime": primes[-1] if primes else None,
    "first_20": primes[:20],
    "numbers": primes[-10:],
    "elapsed_ms": round(elapsed * 1000, 3),
    "summary": f"{len(primes)} primes below {limit}",
})
''',
    },
    {
        "provider": "mesh-labs",
        "slug": "json-transformer",
        "name": "JSON Transformer",
        "category": "transform",
        "description": "Flattens, normalizes and type-profiles arbitrary JSON structures.",
        "tags": ["transform", "json", "convert", "normalize", "clean"],
        "base_price_micros": 1000,
        "price_per_cpu_second_micros": 300,
        "max_price_micros": 45_000,
        "max_runtime_seconds": 20,
        "entrypoint": PRELUDE + '''
payload = load()
source = payload.get("data")
if source is None:
    source = payload.get("input")
if source is None:
    source = {"goal": payload.get("goal", "")}

flat = {}

def walk(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            walk(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk(value, f"{prefix}[{index}]")
    else:
        flat[prefix or "value"] = node

walk(source)
types = {}
for key, value in flat.items():
    types[key] = type(value).__name__

emit({
    "flattened": flat,
    "types": types,
    "leaf_count": len(flat),
    "keys": sorted(flat)[:50],
    "summary": f"flattened into {len(flat)} leaves",
})
''',
    },
    {
        "provider": "mesh-labs",
        "slug": "report-builder",
        "name": "Report Builder",
        "category": "analyze",
        "description": "Renders a markdown + CSV report artifact from upstream step output.",
        "tags": ["report", "analyze", "summary", "artifact", "csv"],
        "base_price_micros": 2500,
        "price_per_cpu_second_micros": 500,
        "price_per_mb_egress_micros": 40,
        "max_price_micros": 90_000,
        "max_runtime_seconds": 30,
        "entrypoint": PRELUDE + '''
import datetime, json, os

payload = load()
upstream = payload.get("input")
rows = []
if isinstance(upstream, dict):
    rows = [(k, v) for k, v in upstream.items() if not isinstance(v, (dict, list))]
elif isinstance(upstream, list):
    rows = [(str(i), v) for i, v in enumerate(upstream[:100])]
if not rows:
    rows = [("goal", payload.get("goal", "")), ("capability", payload.get("capability", ""))]

os.makedirs("artifacts", exist_ok=True)
stamp = datetime.datetime.utcnow().isoformat()

with open("artifacts/report.md", "w", encoding="utf-8") as fh:
    fh.write(f"# M2X Report\\n\\nGenerated {stamp}\\n\\n")
    fh.write(f"Goal: {payload.get('goal', 'n/a')}\\n\\n| field | value |\\n| --- | --- |\\n")
    for key, value in rows:
        fh.write(f"| {key} | {str(value)[:120]} |\\n")

with open("artifacts/data.csv", "w", encoding="utf-8") as fh:
    fh.write("field,value\\n")
    for key, value in rows:
        fh.write(f'"{key}","{str(value)[:200].replace(chr(34), chr(39))}"\\n')

emit({
    "artifacts": ["report.md", "data.csv"],
    "rows": len(rows),
    "generated_at": stamp,
    "summary": f"report with {len(rows)} rows",
})
''',
    },
    {
        "provider": "mesh-labs",
        "slug": "schema-validator",
        "name": "Schema Validator",
        "category": "validate",
        "description": "Validates a document against a lightweight JSON schema (types, required, ranges).",
        "tags": ["validate", "verify", "schema", "check", "lint"],
        "base_price_micros": 900,
        "price_per_cpu_second_micros": 250,
        "max_price_micros": 35_000,
        "max_runtime_seconds": 20,
        "entrypoint": PRELUDE + '''
payload = load()
document = payload.get("document")
if document is None:
    document = payload.get("input") if isinstance(payload.get("input"), dict) else {}
schema = payload.get("schema") or {}
required = schema.get("required", [])
properties = schema.get("properties", {})

TYPES = {"string": str, "number": (int, float), "integer": int,
         "boolean": bool, "object": dict, "array": list}
errors = []
for field in required:
    if field not in document:
        errors.append({"field": field, "error": "required field missing"})
for field, rule in properties.items():
    if field not in document:
        continue
    expected = TYPES.get(rule.get("type", ""))
    if expected and not isinstance(document[field], expected):
        errors.append({"field": field,
                       "error": f"expected {rule.get('type')}, got {type(document[field]).__name__}"})

emit({
    "valid": not errors,
    "error_count": len(errors),
    "errors": errors,
    "checked_fields": list(properties) or list(document),
    "summary": "valid" if not errors else f"{len(errors)} schema violation(s)",
})
''',
    },
]

PROVIDERS = {
    "quantum-forge": {
        "name": "Quantum Forge Compute",
        "description": "Deterministic hashing, text and statistical primitives with strict SLAs.",
        "email": "ops@quantumforge.demo",
        "regions": ["us-east", "eu-west"],
        "capabilities": ["hash", "text", "analyze"],
        "reputation": 78.0,
    },
    "mesh-labs": {
        "name": "Mesh Labs",
        "description": "CPU-heavy math, JSON transformation, validation and reporting workers.",
        "email": "ops@meshlabs.demo",
        "regions": ["us-west", "ap-south"],
        "capabilities": ["compute", "transform", "validate", "report"],
        "reputation": 71.5,
    },
}

DEMO_PASSWORD = "demo-password-123"


def _ensure_user(db: Session, email: str, name: str, role: Role, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user:
        return user
    user = User(
        email=email,
        display_name=name,
        password_hash=hash_password(password),
        role=role,
        wallet_address=f"M2X{secrets.token_hex(20).upper()}",
        payment_secret=secrets.token_urlsafe(32),
    )
    db.add(user)
    db.flush()
    ledger.credit(db, user.id, settings.signup_grant_micros, memo="seed grant")
    return user


def _seed_integrations(db: Session) -> dict:
    """Register external providers. Idempotent, and never fatal to boot."""
    from .integrations.zerion.registration import ensure_registered as ensure_zerion

    report: dict = {}
    try:
        report["zerion"] = ensure_zerion(db)
    except Exception as exc:  # pragma: no cover - an integration must not block startup
        log.warning("zerion registration skipped: %s", exc)
        report["zerion"] = {"provider": "zerion", "enabled": False, "error": str(exc)[:200]}
    return report


def seed(db: Session, force: bool = False) -> dict:
    existing = db.scalar(select(func.count(Service.id))) or 0
    if existing and not force:
        integrations = _seed_integrations(db)
        publish_local_services(db)
        return {"seeded": False, "services": existing, "integrations": integrations}

    admin = _ensure_user(db, "admin@m2x.local", "Platform Admin", Role.admin,
                         "admin-password-123")
    consumer = _ensure_user(db, "agent@m2x.local", "Demo Agent", Role.agent, DEMO_PASSWORD)
    _ensure_user(db, "consumer@m2x.local", "Demo Consumer", Role.consumer, DEMO_PASSWORD)

    created = 0
    for slug, meta in PROVIDERS.items():
        owner = _ensure_user(db, meta["email"], meta["name"], Role.provider, DEMO_PASSWORD)
        provider = db.scalar(select(Provider).where(Provider.slug == slug))
        if provider is None:
            provider = Provider(
                owner_id=owner.id,
                slug=slug,
                name=meta["name"],
                description=meta["description"],
                payout_address=owner.wallet_address,
                regions=meta["regions"],
                capabilities=meta["capabilities"],
                is_verified=True,
                reputation_score=meta["reputation"],
            )
            db.add(provider)
            db.flush()

        for spec in [s for s in SERVICES if s["provider"] == slug]:
            if db.scalar(select(Service).where(Service.provider_id == provider.id,
                                               Service.slug == spec["slug"])):
                continue
            service = Service(
                provider_id=provider.id,
                slug=spec["slug"],
                name=spec["name"],
                description=spec["description"],
                category=spec["category"],
                runtime="python",
                entrypoint=spec["entrypoint"],
                tags=spec["tags"],
                base_price_micros=spec["base_price_micros"],
                price_per_cpu_second_micros=spec["price_per_cpu_second_micros"],
                price_per_mb_egress_micros=spec.get("price_per_mb_egress_micros", 10),
                max_price_micros=spec["max_price_micros"],
                max_runtime_seconds=spec["max_runtime_seconds"],
                memory_mb=512,
                source_hash=sha256_hex(spec["entrypoint"]),
                input_schema={"type": "object",
                              "properties": {"text": {"type": "string"},
                                             "numbers": {"type": "array"},
                                             "input": {}}},
                output_schema={"type": "object"},
            )
            db.add(service)
            created += 1

    db.flush()
    integrations = _seed_integrations(db)
    published = publish_local_services(db)
    log.info("seeded %s services (%s listings) admin=%s", created, published, admin.email)
    return {
        "seeded": True,
        "services_created": created,
        "listings": published,
        "integrations": integrations,
        "accounts": {
            "admin": {"email": "admin@m2x.local", "password": "admin-password-123"},
            "agent": {"email": "agent@m2x.local", "password": DEMO_PASSWORD},
            "consumer": {"email": "consumer@m2x.local", "password": DEMO_PASSWORD},
            "providers": [m["email"] for m in PROVIDERS.values()],
        },
    }
