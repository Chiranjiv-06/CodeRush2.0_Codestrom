"""Redis-backed cache / rate limiter / distributed lock with in-process fallback.

The public surface is identical either way, so business code never branches.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from .config import settings


class _MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[float | None, Any]] = {}
        self._lock = threading.RLock()

    def _expired(self, key: str) -> bool:
        item = self._data.get(key)
        if item is None:
            return True
        exp, _ = item
        if exp is not None and exp < time.time():
            self._data.pop(key, None)
            return True
        return False

    def get(self, key: str) -> Any:
        with self._lock:
            if self._expired(key):
                return None
            return self._data[key][1]

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        with self._lock:
            self._data[key] = (time.time() + ttl if ttl else None, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def incr(self, key: str, ttl: int | None = None) -> int:
        with self._lock:
            current = 0 if self._expired(key) else int(self._data[key][1])
            exp = self._data[key][0] if key in self._data and not self._expired(key) else None
            current += 1
            self._data[key] = (exp if exp else (time.time() + ttl if ttl else None), current)
            return current

    def setnx(self, key: str, value: Any, ttl: int | None = None) -> bool:
        with self._lock:
            if not self._expired(key):
                return False
            self._data[key] = (time.time() + ttl if ttl else None, value)
            return True

    def keys(self, prefix: str) -> list[str]:
        with self._lock:
            return [k for k in list(self._data) if k.startswith(prefix) and not self._expired(k)]

    def ping(self) -> bool:
        return True


class _RedisBackend:  # pragma: no cover - exercised only when Redis is running
    name = "redis"

    def __init__(self, client) -> None:
        self.client = client

    def get(self, key: str) -> Any:
        raw = self.client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        payload = json.dumps(value, default=str)
        self.client.set(key, payload, ex=ttl)

    def delete(self, key: str) -> None:
        self.client.delete(key)

    def incr(self, key: str, ttl: int | None = None) -> int:
        pipe = self.client.pipeline()
        pipe.incr(key)
        if ttl:
            pipe.expire(key, ttl, nx=True)
        return int(pipe.execute()[0])

    def setnx(self, key: str, value: Any, ttl: int | None = None) -> bool:
        return bool(self.client.set(key, json.dumps(value, default=str), nx=True, ex=ttl))

    def keys(self, prefix: str) -> list[str]:
        return [k.decode() if isinstance(k, bytes) else k for k in self.client.scan_iter(f"{prefix}*")]

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False


def _build_backend():
    if settings.redis_url:
        try:  # pragma: no cover
            import redis

            client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
            client.ping()
            return _RedisBackend(client)
        except Exception:
            pass
    return _MemoryBackend()


backend = _build_backend()


# --------------------------------------------------------------------------- #
# Helpers used across the app
# --------------------------------------------------------------------------- #
def cache_get(key: str) -> Any:
    return backend.get(key)


def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    backend.set(key, value, ttl)


def cache_delete(key: str) -> None:
    backend.delete(key)


def rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Fixed-window limiter. Returns (allowed, current_count)."""
    count = backend.incr(f"rl:{key}:{int(time.time() // window_seconds)}", ttl=window_seconds)
    return count <= limit, count


class Lock:
    """Best-effort distributed lock (`with Lock("job:x"): ...`)."""

    def __init__(self, name: str, ttl: int = 60) -> None:
        self.key = f"lock:{name}"
        self.ttl = ttl
        self.acquired = False

    def acquire(self, blocking: bool = False, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while True:
            if backend.setnx(self.key, str(time.time()), ttl=self.ttl):
                self.acquired = True
                return True
            if not blocking or time.time() > deadline:
                return False
            time.sleep(0.05)

    def release(self) -> None:
        if self.acquired:
            backend.delete(self.key)
            self.acquired = False

    def __enter__(self) -> "Lock":
        self.acquire(blocking=True)
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def idempotent(key: str, ttl: int = 3600) -> bool:
    """True the first time a key is seen inside the TTL window."""
    return backend.setnx(f"idem:{key}", "1", ttl=ttl)


def backend_name() -> str:
    return backend.name
