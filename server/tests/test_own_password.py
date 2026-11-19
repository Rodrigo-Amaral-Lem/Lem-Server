# SPDX-License-Identifier: GPL-3.0-only
"""A change is authorized by the current session and password together."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from app.models import Audit
from test_postgres_branding import postgres_setup
from test_sessions import ADMIN, login
from test_workflow import setup


ENDPOINT = "/api/account/password"
NEW = "  Nova password 🔒  "


def change(current, new=NEW):
    return {"current_password": current, "new_password": new, "confirmation": new}


@pytest.mark.parametrize("role", ["admin", "tech"])
def test_only_calling_session_survives(setup, role):
    client, admin, contract, app = setup
    tech = {"email": "tech@example.test", "password": "TechPassword123!"}
    tech_id = client.post("/api/users", headers=admin, json={**tech, "name": "Tech", "role": "tech"}).json()["id"]
    client.put(f"/api/contracts/{contract['id']}/users/{tech_id}", headers=admin).raise_for_status()
    technician = login(client, tech)
    current, other, credentials = (admin, technician, ADMIN) if role == "admin" else (technician, admin, tech)
    revoked = login(client, credentials)
    before = client.get("/api/contracts", headers=current).json()
    result = client.put(ENDPOINT, headers=current, json=change(credentials["password"]))
    assert result.status_code == 200 and result.json()["ok"] is True
    assert client.get("/api/contracts", headers=current).json() == before
    assert client.get("/api/contracts", headers=revoked).status_code == 401
    assert client.get("/api/contracts", headers=other).status_code == 200
    assert client.post("/api/login", json=credentials).status_code == 401
    assert login(client, {**credentials, "password": NEW})
    with app.state.factory() as db:
        event = db.scalars(select(Audit).where(Audit.action == "user.password.change")).one()
        assert event.user_id == event.target == result.json()["user_id"]


def test_rejections_preserve_credentials_and_sessions(setup):
    client, admin, _, app = setup
    second = login(client)
    data = change(ADMIN["password"])
    assert client.put(ENDPOINT, json=data).status_code in (401, 403)
    assert client.put(ENDPOINT, headers={**admin, "X-Lem-Client-Version": "0.1.0"}, json=data).status_code == 426
    for updates in ({"current_password": "incorrect"}, {"confirmation": "mismatch"},
                    {"new_password": "a"*201, "confirmation": "a"*201},
                    {"new_password": "short", "confirmation": "short"}, {"new_password": None},
                    {"user_id": "another-account"}):
        response = client.put(ENDPOINT, headers=admin, json={**data, **updates})
        assert response.status_code == (403 if "current_password" in updates else 422)
        assert ADMIN["password"] not in response.text and NEW not in response.text
    del data["confirmation"]
    response = client.put(ENDPOINT, json=data, headers=admin)
    assert response.status_code == 422 and NEW not in response.text and ADMIN["password"] not in response.text
    assert client.get("/api/contracts", headers=second).status_code == 200
    assert login(client)
    assert client.post("/api/login", json={**ADMIN, "password": NEW}).status_code == 401
    with app.state.factory() as db:
        assert db.scalar(select(Audit).where(Audit.action == "user.password.change")) is None


def test_password_checks_are_rate_limited(setup):
    client, current, _, _ = setup
    for _ in range(5):
        assert client.put(ENDPOINT, headers=current, json=change("incorrect")).status_code == 403
    assert client.put(ENDPOINT, headers=current, json=change("incorrect")).status_code == 429
    assert client.get("/api/contracts", headers=current).status_code == 200


@pytest.mark.parametrize("database_fixture", ["setup", "postgres_setup"])
def test_concurrent_changes_accept_only_one_session(request, database_fixture):
    client, first, _, _ = request.getfixturevalue(database_fixture)
    second = login(client)
    ready = Barrier(2)

    def submit(session, new):
        ready.wait(5)
        result = client.put(ENDPOINT, headers=session, json=change(ADMIN["password"], new))
        return result.status_code, session, new

    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(submit, first, NEW)
        b = workers.submit(submit, second, "AnotherPassword123!")
        accepted, rejected = sorted([a.result(15), b.result(15)], key=lambda result: result[0])
    assert (accepted[0], rejected[0]) == (200, 401)
    assert client.get("/api/contracts", headers=accepted[1]).status_code == 200
    assert client.get("/api/contracts", headers=rejected[1]).status_code == 401
    assert login(client, {**ADMIN, "password": accepted[2]})
    assert client.post("/api/login", json={**ADMIN, "password": rejected[2]}).status_code == 401
