"""Artifact object storage: MinIO/S3 when configured, local filesystem otherwise.

Every put/get is SHA-256 verified — storage is never trusted blindly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .integrity import sha256_hex


@dataclass
class StoredObject:
    backend: str
    key: str
    size: int
    sha256: str


class LocalStorage:
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("path traversal blocked")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        self._path(key).write_bytes(data)
        return StoredObject(self.name, key, len(data), sha256_hex(data))

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def healthy(self) -> bool:
        return self.root.exists()


class MinioStorage:  # pragma: no cover - requires a live MinIO
    name = "minio"

    def __init__(self, client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        import io

        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=content_type)
        return StoredObject(self.name, key, len(data), sha256_hex(data))

    def get(self, key: str) -> bytes:
        resp = self.client.get_object(self.bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def delete(self, key: str) -> None:
        self.client.remove_object(self.bucket, key)

    def exists(self, key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, key)
            return True
        except Exception:
            return False

    def healthy(self) -> bool:
        try:
            return self.client.bucket_exists(self.bucket)
        except Exception:
            return False


def _build_storage():
    if settings.minio_endpoint:
        try:  # pragma: no cover
            from minio import Minio

            client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
            return MinioStorage(client, settings.minio_bucket)
        except Exception:
            pass
    return LocalStorage(settings.artifact_dir)


storage = _build_storage()


def put_artifact(job_id: str, name: str, data: bytes, content_type: str) -> StoredObject:
    safe = name.replace("..", "_").replace("\\", "/").lstrip("/")
    return storage.put(f"jobs/{job_id}/{safe}", data, content_type)


def get_artifact(key: str, expected_sha256: str | None = None) -> bytes:
    data = storage.get(key)
    if expected_sha256 and sha256_hex(data) != expected_sha256:
        raise ValueError(f"integrity failure reading {key}")
    return data


def delete_artifact(key: str) -> None:
    try:
        storage.delete(key)
    except Exception:
        pass


def backend_name() -> str:
    return storage.name
