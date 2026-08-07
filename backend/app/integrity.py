"""SHA-256 integrity primitives: canonical hashing, manifests, signed hash chains."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .config import settings

ZERO_HASH = "0" * 64


def canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding — the only thing we ever hash."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_object(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))


def sign(payload: str, key: str | None = None) -> str:
    secret = (key or settings.receipt_signing_key).encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(payload: str, signature: str, key: str | None = None) -> bool:
    return hmac.compare_digest(sign(payload, key), signature)


def chain_hash(prev_hash: str, body_hash: str) -> str:
    return sha256_hex(f"{prev_hash}:{body_hash}")


def build_manifest(files: dict[str, bytes]) -> dict[str, Any]:
    """SHA-256 manifest over a set of produced files + a merkle-ish root."""
    entries = [
        {"name": name, "size": len(blob), "sha256": sha256_hex(blob)}
        for name, blob in sorted(files.items())
    ]
    root = sha256_hex("".join(e["sha256"] for e in entries)) if entries else ZERO_HASH
    return {"algorithm": "sha256", "entries": entries, "root": root, "count": len(entries)}


def verify_manifest(manifest: dict[str, Any], files: dict[str, bytes]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    expected = {e["name"]: e["sha256"] for e in manifest.get("entries", [])}
    for name, digest in expected.items():
        if name not in files:
            problems.append(f"missing:{name}")
        elif sha256_hex(files[name]) != digest:
            problems.append(f"mismatch:{name}")
    for name in files:
        if name not in expected:
            problems.append(f"unexpected:{name}")
    recomputed = build_manifest(files)["root"]
    if recomputed != manifest.get("root"):
        problems.append("root-mismatch")
    return (not problems), problems
