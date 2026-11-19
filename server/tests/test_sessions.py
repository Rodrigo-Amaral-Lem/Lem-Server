# SPDX-License-Identifier: GPL-3.0-only
"""Expiry, console revocation and login ordering with disposable databases."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from sqlalchemy import select

from app import admin_cli, main
from app.models import Audit, SessionToken, User
from app.security import digest
from test_postgres_branding import postgres_setup
from test_workflow import setup

ADMIN = {"email": "admin@lem.pt", "password": "StrongPassword123!"}
REVOKE = ("users", "revoke-sessions")


def login(client, credentials=ADMIN):
    response = client.post("/api/login", json=credentials)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    return {"Authorization": "Bearer " + response.json()["token"]}


def revoke(app, *scope):
    args = admin_cli.parser().parse_args([*REVOKE, *scope])
    return admin_cli.execute(app.state.factory, args)


def test_session_expires_at_server_deadline_and_does_not_slide(setup, monkeypatch):
    client, legacy, _, app = setup
    issued = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    instant = issued

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(main, "now", lambda: instant.isoformat())
    old_key = digest(legacy["Authorization"].split()[1])
    with app.state.factory() as db:
        db.get(SessionToken, old_key).expires = (issued + timedelta(days=30)).isoformat()
        db.commit()
    new = login(client)
    key = digest(new["Authorization"].split()[1])
    deadline = issued + timedelta(days=365)
    with app.state.factory() as db:
        assert db.get(SessionToken, key).expires == deadline.isoformat()

    instant = issued + timedelta(days=30)
    assert client.get("/api/contracts", headers=legacy).status_code == 401
    assert client.get("/api/contracts", headers=new).status_code == 200
    instant = deadline - timedelta(microseconds=1)
    assert client.get("/api/contracts", headers=new).status_code == 200
    with app.state.factory() as db:
        assert db.get(SessionToken, key).expires == deadline.isoformat()
    instant = deadline
    assert client.get("/api/contracts", headers=new).status_code == 401
    assert client.get("/api/contracts", headers=login(client)).status_code == 200


def test_console_revokes_one_account_or_every_account(setup):
    client, admin, _, app = setup
    credentials = {"email": "tech@example.test", "password": "SessionTest123!"}
    response = client.post("/api/users", headers=admin,
                           json={**credentials, "name": "Technician", "role": "tech"})
    assert response.status_code == 200
    user_id = response.json()["id"]
    first, second = login(client, credentials), login(client, credentials)

    with pytest.raises(ValueError, match="Conta ativa inexistente"):
        revoke(app, "missing@example.test")
    assert client.get("/api/contracts", headers=first).status_code == 200
    assert revoke(app, "TECH@example.test") == {"target": user_id, "sessions_revoked": 2}
    assert client.get("/api/contracts", headers=first).status_code == 401
    assert client.get("/api/contracts", headers=second).status_code == 401
    assert client.get("/api/contracts", headers=admin).status_code == 200
    assert revoke(app, user_id)["sessions_revoked"] == 0

    temporary = login(client, credentials)
    assert client.post("/api/logout", headers=temporary).status_code == 200
    assert client.get("/api/contracts", headers=temporary).status_code == 401
    renewed = login(client, credentials)
    assert revoke(app, "--all") == {"target": "all", "sessions_revoked": 2}
    assert client.get("/api/contracts", headers=renewed).status_code == 401
    assert client.get("/api/contracts", headers=admin).status_code == 401
    assert client.get("/api/contracts", headers=login(client, credentials)).status_code == 200
    with app.state.factory() as db:
        entries = list(db.scalars(select(Audit).where(Audit.action == "console.sessions.revoke")))
        assert sorted(entry.target for entry in entries) == sorted([user_id, user_id, "all"])
        assert all(db.get(User, entry.user_id).role == "system" for entry in entries)


@pytest.mark.parametrize("scope", [[], ["tech@example.test", "--all"]])
def test_revocation_requires_one_explicit_scope(scope):
    with pytest.raises(SystemExit) as error:
        admin_cli.parser().parse_args([*REVOKE, *scope])
    assert error.value.code == 2


def test_postgres_revoke_all_waits_for_an_inflight_login(monkeypatch, postgres_setup):
    # PostgreSQL deploys the actual shared/exclusive login guard.
    client, old, _, app = postgres_setup
    checking_password, finish_login, revocation_started = Event(), Event(), Event()
    verify_password = main.verify
    write_guard = admin_cli.account_write_guard

    def pause_login(*args):
        checking_password.set()
        assert finish_login.wait(10)
        return verify_password(*args)

    def observe_guard(db):
        revocation_started.set()
        return write_guard(db)

    monkeypatch.setattr(main, "verify", pause_login)
    monkeypatch.setattr(admin_cli, "account_write_guard", observe_guard)
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(login, client)
        try:
            assert checking_password.wait(5)
            revocation = workers.submit(revoke, app, "--all")
            assert revocation_started.wait(5)
            with pytest.raises(TimeoutError):
                revocation.result(0.25)
        finally:
            finish_login.set()
        issued = pending.result(10)
        assert revocation.result(10)["sessions_revoked"] == 2
    assert client.get("/api/contracts", headers=issued).status_code == 401
    assert client.get("/api/contracts", headers=old).status_code == 401
    assert client.get("/api/contracts", headers=login(client)).status_code == 200
