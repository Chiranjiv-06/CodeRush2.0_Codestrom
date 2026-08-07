"""Test fixtures: isolated database, storage and workspace per run."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

TMP_ROOT = Path(tempfile.mkdtemp(prefix="m2x-tests-"))
os.environ.update(
    {
        "M2X_VAR_DIR": str(TMP_ROOT),
        "M2X_DATABASE_URL": f"sqlite:///{(TMP_ROOT / 'test.db').as_posix()}",
        "M2X_SCHEDULER_ENABLED": "false",
        "M2X_REDIS_URL": "",
        "M2X_MINIO_ENDPOINT": "",
        "M2X_BAZAAR_ENABLED": "true",
        "M2X_BAZAAR_BASE_URL": "http://127.0.0.1:9",  # unreachable on purpose
        "M2X_BAZAAR_TIMEOUT_SECONDS": "0.3",
        "M2X_JWT_SECRET": "test-secret-that-is-long-enough-for-hs256-keys",
        "M2X_SANDBOX_BACKEND": "subprocess",
        "M2X_SANDBOX_TIMEOUT_SECONDS": "30",
        "M2X_LOG_LEVEL": "WARNING",
    }
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def admin(client):
    resp = client.post("/v1/auth/login",
                       json={"email": "admin@m2x.local", "password": "admin-password-123"})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(scope="session")
def admin_headers(admin):
    return {"Authorization": f"Bearer {admin['access_token']}"}


@pytest.fixture()
def consumer(client):
    """A fresh funded consumer for each test that needs one."""
    import uuid

    email = f"c-{uuid.uuid4().hex[:10]}@test.local"
    resp = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "password-12345", "display_name": "Test Consumer"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    data["headers"] = {"Authorization": f"Bearer {data['access_token']}"}
    return data


@pytest.fixture()
def services(client):
    resp = client.get("/v1/services", params={"limit": 100})
    assert resp.status_code == 200
    return {s["slug"]: s for s in resp.json()}


def sign_payment(client, headers: dict, payment_id: str) -> str:
    """Ask the API for the X-PAYMENT header of one of the caller's payments."""
    resp = client.post("/v1/payments/sign", json={"payment_id": payment_id}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["x_payment"]
