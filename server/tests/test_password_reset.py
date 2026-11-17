# SPDX-License-Identifier: GPL-3.0-only
"""Administrator resets only technicians; credentials never enter audit/output."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from app.models import User, SessionToken, Audit, Assignment
from app.security import verify
from test_workflow import setup

OLD = "OriginalPassword123!"
NEW = "ReplacementPassword456!"


def account(client, admin, name, role="tech"):
    email = name + "@example.test"
    result = client.post("/api/users", headers=admin,
                        json={"name": name, "email": email, "password": OLD, "role": role})
    assert result.status_code == 200, result.text
    token = client.post("/api/login", json={"email": email, "password": OLD}).json()["token"]
    return result.json()["id"], email, {"Authorization": "Bearer " + token}


def test_reset_revokes_all_sessions_preserves_account_and_contracts(setup):
    client, admin, contract, app = setup
    uid, email, session = account(client, admin, "technician")
    assert client.put(f"/api/contracts/{contract['id']}/users/{uid}", headers=admin).status_code == 200
    second = client.post("/api/login", json={"email": email, "password": OLD}).json()["token"]
    response = client.put(f"/api/users/{uid}/password", headers=admin, json={"password": NEW})
    assert response.status_code == 200 and response.json() == {"ok": True, "user_id": uid}
    assert NEW not in response.text and OLD not in response.text
    for headers in (session, {"Authorization": "Bearer " + second}):
        assert client.get("/api/contracts", headers=headers).status_code == 401
    assert client.post("/api/login", json={"email": email, "password": OLD}).status_code == 401
    assert client.post("/api/login", json={"email": email, "password": NEW}).status_code == 200
    assert client.get("/api/users", headers=admin).status_code == 200
    with app.state.factory() as db:
        user = db.get(User, uid)
        assert user.role == "tech" and user.email == email and verify(NEW, user.password)
        assert db.get(Assignment, (uid, contract["id"]))
        audits = list(db.scalars(select(Audit).where(Audit.action == "user.password.reset")))
        assert len(audits) == 1 and audits[0].target == uid
        assert NEW not in str(audits[0].__dict__) and OLD not in str(audits[0].__dict__)


def test_only_admin_can_reset_only_technicians(setup):
    client, admin, _, app = setup
    tech, _, session = account(client, admin, "target")
    another, _, _ = account(client, admin, "other-tech")
    protected, _, admin2 = account(client, admin, "another-admin", "admin")
    for target in (tech, another, protected):
        assert client.put(f"/api/users/{target}/password", headers=session, json={"password": NEW}).status_code == 403
    assert client.put(f"/api/users/{tech}/password", json={"password": NEW}).status_code in (401, 403)
    assert client.put(f"/api/users/{protected}/password", headers=admin, json={"password": NEW}).status_code == 403
    assert client.put(f"/api/users/{protected}/password", headers=admin2, json={"password": NEW}).status_code == 403
    assert client.put("/api/users/does-not-exist/password", headers=admin, json={"password": NEW}).status_code == 404
    # The server rechecks the current role; an old app listing is not authorization.
    with app.state.factory() as db:
        db.get(User, tech).role = "admin"
        db.commit()
    assert client.put(f"/api/users/{tech}/password", headers=admin, json={"password": NEW}).status_code == 403
    assert client.get("/api/users", headers=admin2).status_code == 200


@pytest.mark.parametrize("password", ["", "short-key", "x" * 201])
def test_invalid_password_keeps_old_credentials_and_sessions(setup, password):
    client, admin, _, app = setup
    uid, _, session = account(client, admin, "validation")
    response = client.put(f"/api/users/{uid}/password", headers=admin, json={"password": password})
    assert response.status_code == 422
    if password:
        assert password not in response.text
    assert client.get("/api/contracts", headers=session).status_code == 200
    with app.state.factory() as db:
        assert verify(OLD, db.get(User, uid).password)
        assert not list(db.scalars(select(Audit).where(Audit.action == "user.password.reset")))


def test_concurrent_resets_and_admin_targets(setup):
    client, admin, _, app = setup
    tech, _, session = account(client, admin, "concurrent-tech")
    aid, _, admin2 = account(client, admin, "concurrent-admin", "admin")
    # Different administrators can reset the same technician; one complete password
    # wins. Neither request may partly revoke/update or retain an old session.
    def reset(headers, password):
        return client.put(f"/api/users/{tech}/password", headers=headers, json={"password": password})
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(reset, admin, NEW), pool.submit(reset, admin2, NEW + "2")]
        assert [f.result(timeout=15).status_code for f in futures] == [200, 200]
    assert client.get("/api/contracts", headers=session).status_code == 401
    with app.state.factory() as db:
        encoded = db.get(User, tech).password
        assert verify(NEW, encoded) or verify(NEW + "2", encoded)
        assert not list(db.scalars(select(SessionToken).where(SessionToken.user_id == tech)))
        original_admin = db.scalar(select(User.id).where(User.email == "admin@lem.pt"))
    with ThreadPoolExecutor(2) as pool:
        def forbidden(headers, target):
            return client.put(f"/api/users/{target}/password", headers=headers, json={"password": NEW}).status_code
        futures = [pool.submit(forbidden, admin, aid), pool.submit(forbidden, admin2, original_admin)]
        assert [f.result(timeout=15) for f in futures] == [403, 403]
