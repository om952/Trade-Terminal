"""The local API answers only this machine: no DNS rebinding, no drive-by posts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.api.security import is_local_host, is_local_origin


@pytest.fixture(scope="module")
def local() -> TestClient:
    return TestClient(app, base_url="http://localhost:8000")


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000"])
def test_local_hosts_are_accepted(host):
    assert is_local_host(host)


@pytest.mark.parametrize(
    "host", ["evil.com", "localhost.evil.com", "127.0.0.1.nip.io", "", None, "[bad"]
)
def test_foreign_or_malformed_hosts_are_rejected(host):
    assert not is_local_host(host)


@pytest.mark.parametrize(
    "origin",
    [None, "http://localhost:5173", "http://127.0.0.1:5173", "https://127.0.0.1:8000"],
)
def test_local_or_absent_origins_are_accepted(origin):
    assert is_local_origin(origin)


@pytest.mark.parametrize(
    "origin", ["https://evil.com", "http://localhost.evil.com", "null", "file://", "ftp://localhost"]
)
def test_foreign_origins_are_rejected(origin):
    assert not is_local_origin(origin)


def test_dns_rebinding_request_is_refused():
    """A rebinding attack arrives with the attacker's hostname in Host."""
    attacker = TestClient(app, base_url="http://attacker.example")
    response = attacker.get("/api/health")
    assert response.status_code == 403
    assert "Host" in response.json()["detail"]


def test_cross_site_post_to_live_start_is_refused(local):
    response = local.post(
        "/api/live/start",
        json={"strategy": {}, "source": "replay"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "Origin" in response.json()["detail"]


def test_vite_dev_server_origin_is_allowed(local):
    response = local.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 200


def test_cli_style_request_without_origin_is_allowed(local):
    assert local.get("/api/health").status_code == 200
