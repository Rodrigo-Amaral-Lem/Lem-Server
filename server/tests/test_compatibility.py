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
    monkeypatch.setenv("LEM_MIN_CLIENT_VERSION", "0.1.28")
    monkeypatch.setenv("LEM_LATEST_CLIENT_VERSION", "0.1.30")
    app = create_app("sqlite:///" + str(tmp_path / "test.db"))
    return app, TestClient(app)


@pytest.mark.parametrize(
    "version", [None, "", "0.1.27", "0.1.31", "v0.1.28", "0.1", "0.01.28", "x" * 500, "0.1.28-beta"]
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


@pytest.mark.parametrize("version,update", [("0.1.28", True), ("0.1.29", True), ("0.1.30", False)])
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
    client.headers["X-Lem-Client-Version"] = "0.1.27"
    assert client.get("/api/contracts").status_code == 426


@pytest.mark.parametrize(
    "minimum,latest", [("0.1.31", "0.1.30"), ("bad", "0.1.30"), ("0.1.28", "0.1.30-beta")]
)
def test_invalid_policy_fails_startup(monkeypatch, minimum, latest):
    monkeypatch.setenv("LEM_MIN_CLIENT_VERSION", minimum)
    monkeypatch.setenv("LEM_LATEST_CLIENT_VERSION", latest)
    with pytest.raises(ValueError):
        ClientPolicy()


def test_numeric_version_order_and_download_url(monkeypatch):
    monkeypatch.setenv("LEM_MIN_CLIENT_VERSION", "0.1.9")
    monkeypatch.setenv("LEM_LATEST_CLIENT_VERSION", "0.1.30")
    monkeypatch.setenv("LEM_UPDATE_URL", "https://lem.example.org/download")
    p = ClientPolicy()
    assert p.evaluate("0.1.10")["compatible"]
    assert p.evaluate("0.1.10")["update_url"] == "https://lem.example.org/download"
    monkeypatch.setenv("LEM_UPDATE_URL", "http://example.org/download")
    with pytest.raises(ValueError):
        ClientPolicy()


def test_shipped_policy_accepts_current_app(monkeypatch):
    monkeypatch.delenv("LEM_MIN_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_LATEST_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_UPDATE_URL", raising=False)
    policy = ClientPolicy()
    assert policy.minimum == "0.1.57"
    assert policy.latest == "0.1.57"
    assert policy.evaluate("0.1.57")["compatible"]
    assert not policy.evaluate("0.1.57")["update_available"]
    for version in ["0.1.38", "0.1.48", "0.1.51", "0.1.55", "0.1.58"]:
        assert not policy.evaluate(version)["compatible"]


def test_shipped_policy_allows_current_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "CompatibilityPassword123!")
    monkeypatch.delenv("LEM_MIN_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_LATEST_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("LEM_UPDATE_URL", raising=False)
    app = create_app("sqlite:///" + str(tmp_path / "release.db"))
    with TestClient(app, headers={"X-Lem-Client-Version": "0.1.57"}) as client:
        assert client.get("/api/client-compatibility").json()["compatible"]
        result = client.post("/api/login", json={
            "email": "admin@lem.pt", "password": "CompatibilityPassword123!"})
        assert result.status_code == 200, result.text
