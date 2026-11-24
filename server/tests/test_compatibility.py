# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Policy gates login and existing sessions, including clients predating the handshake."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.compatibility import ClientPolicy, version_tuple
from app.main import create_app
from app.models import SessionToken


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "CompatibilityPassword123!")
    monkeypatch.setenv("LEM_MIN_CLIENT_VERSION", "0.9.28")
    monkeypatch.setenv("LEM_LATEST_CLIENT_VERSION", "0.9.30")
    app = create_app("sqlite:///" + str(tmp_path / "test.db"))
    return app, TestClient(app)


@pytest.mark.parametrize(
    "version", [None, "", "0.9.27", "0.9.31", "v0.9.28", "0.1", "0.01.28", "x" * 500, "0.9.28-beta"]
)
def test_incompatible_or_unidentified_never_gets_session(api, version):
    app, client = api
    headers = {} if version is None else {"X-Lem-Client-Version": version}
    policy = client.get("/api/client-compatibility", headers=headers).json()
    assert not policy["compatible"] and not policy["update_available"]
    result = client.post(
        "/api/login",
        headers=headers,
        json={"email": "admin@lem.pt", "password": "CompatibilityPassword123!"},
    )
    assert result.status_code == 426
    assert result.json()["detail"] == policy
    with app.state.factory() as db:
        assert db.scalar(select(func.count()).select_from(SessionToken)) == 0
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("version,update", [("0.9.28", True), ("0.9.29", True), ("0.9.30", False)])
def test_compatible_login_and_existing_session_gate(api, version, update):
    app, client = api
    client.headers["X-Lem-Client-Version"] = version
    response = client.get("/api/client-compatibility")
    assert response.headers["cache-control"] == "no-store"
    policy = response.json()
    assert policy["compatible"] and policy["update_available"] == update
    response = client.post(
        "/api/login", json={"email": "admin@lem.pt", "password": "CompatibilityPassword123!"}
    )
    assert response.status_code == 200
    client.headers["Authorization"] = "Bearer " + response.json()["token"]
    assert client.get("/api/contracts").status_code == 200
    client.headers["X-Lem-Client-Version"] = "0.9.27"
    assert client.get("/api/contracts").status_code == 426


@pytest.mark.parametrize(
    "minimum,latest", [("0.6.31", "0.6.30"), ("bad", "0.6.30"), ("0.6.28", "0.6.30-beta")]
)
def test_invalid_policy_fails_startup(monkeypatch, minimum, latest):
    monkeypatch.setenv("LEM_MIN_CLIENT_VERSION", minimum)
    monkeypatch.setenv("LEM_LATEST_CLIENT_VERSION", latest)
    with pytest.raises(ValueError):
        ClientPolicy()


def test_numeric_version_order_and_download_url(monkeypatch):
    monkeypatch.setenv("LEM_MIN_CLIENT_VERSION", "0.9.9")
    monkeypatch.setenv("LEM_LATEST_CLIENT_VERSION", "0.9.30")
    monkeypatch.setenv("LEM_UPDATE_URL", "https://lem.example.org/download")
    p = ClientPolicy()
    assert p.evaluate("0.9.10")["compatible"]
    assert p.evaluate("0.9.10")["update_url"] == "https://lem.example.org/download"
    monkeypatch.setenv("LEM_UPDATE_URL", "http://example.org/download")
    with pytest.raises(ValueError):
        ClientPolicy()


def test_shipped_policy_accepts_current_app(monkeypatch):
    monkeypatch.delenv("LEM_MIN_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_LATEST_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_UPDATE_URL", raising=False)
    policy = ClientPolicy()
    assert (policy.minimum, policy.latest) == ("0.9.1", "0.11.0")
    assert policy.evaluate("0.9.1")["compatible"]
    assert policy.evaluate("0.9.1")["update_available"]
    assert policy.evaluate("0.9.2")["compatible"] and policy.evaluate("0.9.2")["update_available"]
    assert policy.evaluate("0.10.0")["compatible"] and policy.evaluate("0.10.0")["update_available"]
    assert policy.evaluate("0.11.0")["compatible"]
    assert not policy.evaluate("0.11.0")["update_available"]
    for version in ("0.2.5", "0.2.6", "0.4.0", "0.5.2", "0.5.3", "0.5.4", "0.5.5", "0.6.0"):
        assert not policy.evaluate(version)["compatible"]


def test_shipped_policy_allows_compatible_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "CompatibilityPassword123!")
    monkeypatch.delenv("LEM_MIN_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_LATEST_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_UPDATE_URL", raising=False)
    app = create_app("sqlite:///" + str(tmp_path / "release.db"))
    with TestClient(app, headers={"X-Lem-Client-Version": "0.9.1"}) as client:
        policy=client.get("/api/client-compatibility").json()
        assert policy["compatible"] and policy["update_available"]
        result = client.post("/api/login", json={
            "email": "admin@lem.pt", "password": "CompatibilityPassword123!"})
        assert result.status_code == 200, result.text
        session = {"Authorization": "Bearer " + result.json()["token"]}
        assert client.get("/api/work-orders", headers=session).status_code == 200
        rejected = client.get("/api/work-orders", headers={**session, "X-Lem-Client-Version": "0.9.0"})
        assert rejected.status_code == 426
        detail = rejected.json()["detail"]
        assert detail["latest_version"] == "0.11.0" and "0.11.0" in detail["message"]


@pytest.mark.parametrize(
    "version", ["0.2.2", "0.2.3", "0.2.4", "0.2.5", "0.2.6", "0.5.2", "0.5.3", "0.5.4", "0.5.5", "0.6.0", "0.9.0"]
)
def test_shipped_policy_blocks_older_apps_at_login(tmp_path, monkeypatch, version):
    monkeypatch.setenv('ADMIN_PASSWORD', 'CompatibilityPassword123!')
    monkeypatch.delenv('LEM_MIN_CLIENT_VERSION', raising=False)
    monkeypatch.delenv('LEM_LATEST_CLIENT_VERSION', raising=False)
    app = create_app('sqlite:///' + str(tmp_path / 'old-client.db'))
    with TestClient(app, headers={'X-Lem-Client-Version': version}) as client:
        result = client.post('/api/login', json={
            'email': 'admin@lem.pt', 'password': 'CompatibilityPassword123!'})
        assert result.status_code == 426, result.text
        assert not result.json()['detail']['compatible']


@pytest.mark.parametrize("minimum,latest", [("0.2.2", "0.2.5"), ("0.6.1", "0.9.0")])
def test_stale_environment_cannot_lower_the_mandatory_floor(monkeypatch, minimum, latest):
    monkeypatch.setenv("LEM_MIN_CLIENT_VERSION", minimum)
    monkeypatch.setenv("LEM_LATEST_CLIENT_VERSION", latest)
    policy = ClientPolicy()
    assert (policy.minimum, policy.latest) == ("0.9.1", "0.11.0")
    assert policy.evaluate("0.9.1")["compatible"]
    assert not policy.evaluate(latest)["compatible"]
