# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Local console uses real transactions against a disposable SQLite database."""

import json
import uuid
from datetime import timedelta
from pathlib import Path
import pytest
from sqlalchemy import select
from app import admin_cli as cli
from app.models import User, Report, Contract, ContractTrash, Charger, Photo, DeletedReport
from app.security import verify
from app.contract_trash import utc_now
from test_workflow import setup
from test_contract_trash import populated, trash


def command(app, words):
    return cli.execute(app.state.factory, cli.parser().parse_args(words))


def test_accounts_bootstrap_roles_password_privacy_edits_and_last_admin(tmp_path, monkeypatch):
    from app.models import database

    _, factory = database("sqlite:///" + str(tmp_path / "empty.db"))
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace(factory=factory))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "SecretPassword123!")
    args = [
        "users",
        "create",
        "--email",
        "ADMIN@LEM.PT",
        "--name",
        "Admin",
        "--role",
        "admin",
        "--if-missing",
    ]
    user = command(app, args)
    assert command(app, args)["id"] == user["id"]
    assert user["email"] == "admin@lem.pt"
    assert "password" not in json.dumps(command(app, ["users", "list"]))
    assert "SecretPassword123!" not in json.dumps(user)
    stored = command(app, ["users", "password-hash", user["id"]])
    assert verify("SecretPassword123!", stored["password_hash"])
    monkeypatch.setattr("builtins.input", lambda _: "APAGAR")
    with pytest.raises(ValueError, match="último administrador"):
        command(app, ["users", "delete", user["id"]])
    with pytest.raises(ValueError, match="último administrador"):
        command(app, ["users", "update", user["id"], "--role", "tech"])
    second = command(
        app,
        [
            "users",
            "create",
            "--email",
            "second@lem.pt",
            "--name",
            "Second",
            "--role",
            "admin",
            "--generate",
        ],
    )
    assert "new_password" not in second
    changed = command(
        app,
        [
            "users",
            "update",
            user["id"],
            "--name",
            "New Name",
            "--email",
            "new@lem.pt",
            "--role",
            "tech",
        ],
    )
    assert (changed["name"], changed["email"], changed["role"]) == (
        "New Name",
        "new@lem.pt",
        "tech",
    )
    generated = command(app, ["users", "password", user["id"], "--generate", "--show-password"])
    assert "new_password" in generated
    assert "password" not in json.dumps(command(app, ["users", "list"]))
    monkeypatch.setattr("builtins.input", lambda _: "apagar")
    with pytest.raises(ValueError, match="cancelada"):
        command(app, ["users", "delete", user["id"]])
    assert command(app, ["users", "show", user["id"]])["name"] == "New Name"


def test_account_deletion_orphan_repair_closed_owner_and_report_deletion(setup, monkeypatch):
    c, h, _, app = setup
    cid, charger, rid, pid, _ = populated(setup)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "SecondPassword123!")
    replacement = command(
        app,
        [
            "users",
            "create",
            "--email",
            "replacement@lem.pt",
            "--name",
            "Replacement",
            "--role",
            "admin",
        ],
    )
    # Old admin remains the report's historical UUID, but loses all credentials.
    with app.state.factory() as db:
        old = db.get(Report, rid).owner_id
        frozen = db.get(Report, rid).snapshot
        db.get(Report, rid).status = "closed"
        db.commit()
    monkeypatch.setattr("builtins.input", lambda _: "APAGAR")
    command(app, ["users", "delete", old])
    assert c.get("/api/users", headers=h).status_code == 401
    token = c.post(
        "/api/login", json=dict(email="replacement@lem.pt", password="SecondPassword123!")
    ).json()["token"]
    nh = {"Authorization": "Bearer " + token}
    doc = c.get(f"/api/reports/{rid}", headers=nh).json()
    assert doc["owner_id"] == "" and doc["technician"] == "Sem proprietário"
    assert command(app, ["reports", "list", "--unowned"])[0]["id"] == rid
    updated = command(app, ["reports", "owner", rid, replacement["id"]])
    assert updated["status"] == "closed"
    assert command(app, ["reports", "list", "--unowned"]) == []
    with app.state.factory() as db:
        assert db.get(Report, rid).snapshot == frozen
        assert db.get(Photo, pid) is not None
    monkeypatch.setattr("builtins.input", lambda _: "NO")
    with pytest.raises(ValueError):
        command(app, ["reports", "delete", rid])
    monkeypatch.setattr("builtins.input", lambda _: "APAGAR")
    command(app, ["reports", "delete", rid])
    with app.state.factory() as db:
        assert db.get(Report, rid) is None and db.get(Photo, pid) is None
        assert db.get(DeletedReport, rid) is not None
    # Existing HTTP endpoint also permits an administrator to create another admin.
    result = c.post(
        "/api/users",
        headers=nh,
        json=dict(email="third@lem.pt", name="Third", password="ThirdPassword123!", role="admin"),
    )
    assert result.status_code == 200
    assert all("password" not in row for row in c.get("/api/users", headers=nh).json())


def test_contract_force_purge_only_target_and_all_confirmation_paths(setup, monkeypatch):
    c, h, contract, app = setup
    cid = contract["id"]
    other = c.post(
        "/api/contracts", headers=h, json=dict(company="Other", year=2026, semester=1, kind="TRE")
    ).json()["id"]
    for action in ["trash", "purge"]:
        monkeypatch.setattr("builtins.input", lambda _: "no")
        with pytest.raises(ValueError):
            command(app, ["contracts", action, cid])
    monkeypatch.setattr("builtins.input", lambda _: "APAGAR")
    command(app, ["contracts", "trash", cid])
    command(app, ["contracts", "restore", cid])
    assert command(app, ["contracts", "list", "--deleted"]) == []
    command(app, ["contracts", "trash", cid])
    trash(c, h, other)
    with app.state.factory() as db:
        db.get(ContractTrash, other).purge_after = (utc_now() - timedelta(seconds=1)).isoformat()
        db.commit()
    command(app, ["contracts", "purge", cid])
    with app.state.factory() as db:
        assert db.get(Contract, cid) is None
        assert (
            db.get(Contract, other) is not None
        ), "A forced single-contract purge must not purge other expired entries"


def test_charger_add_patch_and_technician_report_assignment(setup, monkeypatch):
    # Exercise the Windows console default even when pytest itself uses UTF-8.
    read_text = Path.read_text

    def read_in_console_locale(path, encoding=None, errors=None, **options):
        if path.name == "checklist-v1.json" and encoding is None:
            encoding = "cp1252"
        return read_text(path, encoding=encoding, errors=errors, **options)

    monkeypatch.setattr(Path, "read_text", read_in_console_locale)
    c, h, contract, app = setup
    cid = contract["id"]
    added = command(
        app,
        [
            "chargers",
            "add",
            cid,
            "--external-id",
            "NEW-001",
            "--set",
            "model=Estação Âncora / North",
            "--set",
            "serial=001",
            "--set",
            "connectors=CCS2, CCS2",
        ],
    )
    rid = str(uuid.uuid4())
    assert (
        c.put(
            f"/api/reports/{rid}",
            headers=h,
            json=dict(checklist_version=TEMPLATE["version"],charger_id=added["id"], base_revision=0, payload={}),
        ).status_code
        == 200
    )
    before = c.get(f"/api/reports/{rid}", headers=h).json()
    assert before["snapshot"]["charger"]["model"] == "Estação Âncora / North"
    changed = command(
        app,
        [
            "chargers",
            "update",
            cid,
            added["id"],
            "--set",
            "address=Praça São João / South",
            "--set",
            "connectors=Mennekes Type 2",
        ],
    )
    assert changed["details"]["serial"] == "001"
    assert changed["details"]["address"] == "Praça São João / South"
    assert c.get(f"/api/reports/{rid}", headers=h).json() == before
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "TechPassword123!")
    tech = command(app, ["users", "create", "--email", "tech@lem.pt", "--name", "Tech"])
    command(app, ["users", "assign", tech["id"], cid])
    assert cid in command(app, ["users", "show", tech["id"]])["contracts"]
    command(app, ["users", "unassign", tech["id"], cid])
    assert cid not in command(app, ["users", "show", tech["id"]])["contracts"]
    with pytest.raises(ValueError, match="sem acesso"):
        command(app, ["reports", "owner", rid, tech["id"]])
    assert (
        command(app, ["reports", "owner", rid, tech["id"], "--assign-access"])["owner_id"]
        == tech["id"]
    )
    original = changed["details"]
    with pytest.raises(ValueError):
        command(app, ["chargers", "update", cid, added["id"], "--set", "connectors=100 x CCS2"])
    assert command(app, ["chargers", "list", cid])[0]["details"] == original
