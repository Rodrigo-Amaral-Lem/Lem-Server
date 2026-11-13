# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Reproductions of interactions introduced by account/retention administration."""

import uuid
import pytest
from sqlalchemy import select
from app import admin_cli as cli
from app.models import User, Contract, ContractTrash, Report, Photo, SessionToken
from app.security import hash_password
from test_workflow import setup
from test_admin_cli import command
from test_contract_trash import populated


def test_delete_confirmation_is_bound_to_uuid_not_reused_email(setup, monkeypatch):
    _, _, _, app = setup
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "StrongPassword123!")
    original = command(app, ["users", "create", "--email", "original@lem.pt", "--name", "Original"])
    replacement_id = str(uuid.uuid4())

    def concurrent_email_change(_):
        with app.state.factory() as db:
            user = db.get(User, original["id"])
            user.email = "renamed@lem.pt"
            db.flush()
            db.add(
                User(
                    id=replacement_id,
                    email="original@lem.pt",
                    name="Replacement",
                    role="tech",
                    password=hash_password("StrongPassword123!"),
                )
            )
            db.commit()
        return "APAGAR"

    monkeypatch.setattr("builtins.input", concurrent_email_change)
    command(app, ["users", "delete", "original@lem.pt"])
    with app.state.factory() as db:
        assert db.get(User, original["id"]).role == "deleted"
        assert (
            db.get(User, replacement_id).role == "tech"
        ), "Unconfirmed replacement account must survive"


def test_force_purge_never_commits_deadline_without_actual_deletion(setup, monkeypatch):
    c, h, contract, app = setup
    monkeypatch.setattr("builtins.input", lambda _: "APAGAR")
    cid, _, rid, pid, _ = populated(setup)
    command(app, ["contracts", "trash", cid])
    from sqlalchemy.orm import Session

    original_commit = Session.commit
    snapshots = []

    def inspect_commits(db):
        original_commit(db)
        with app.state.factory() as inspect:
            snapshots.append(
                (
                    inspect.get(Contract, cid) is not None,
                    inspect.get(ContractTrash, cid) is not None,
                    inspect.get(Report, rid) is not None,
                )
            )

    monkeypatch.setattr(Session, "commit", inspect_commits)
    command(app, ["contracts", "purge", cid])
    assert snapshots
    assert all(not exists for exists, _, _ in snapshots), "No partially committed forced deletion"
    with app.state.factory() as db:
        assert db.get(Photo, pid) is None


def test_console_text_metadata_remains_usable_by_native_client(setup, tmp_path):
    _, _, contract, app = setup
    payload = tmp_path / "charger.json"
    payload.write_text('{"model":"Test", "serial":12345, "postal_code":1234, "connectors":"CCS2"}')
    result = command(
        app,
        [
            "chargers",
            "add",
            contract["id"],
            "--external-id",
            "TEST",
            "--details-json",
            str(payload),
        ],
    )
    assert isinstance(result["details"]["serial"], str), "Native QString extraction requires text"
    assert isinstance(result["details"]["postal_code"], str)


def test_password_prompt_does_not_modify_an_account_deleted_while_waiting(setup, monkeypatch):
    _, _, _, app = setup
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "StrongPassword123!")
    user = command(app, ["users", "create", "--email", "waiting@lem.pt", "--name", "Waiting"])
    original_hash = None

    def delete_during_prompt(_):
        nonlocal original_hash
        with app.state.factory() as db:
            target = db.get(User, user["id"])
            target.role = "deleted"
            target.password = hash_password("ErasedCredential123!")
            original_hash = target.password
            db.commit()
        return "NewPassword123!"

    monkeypatch.setattr(cli.getpass, "getpass", delete_during_prompt)
    with pytest.raises(ValueError, match="ativa inexistente"):
        command(app, ["users", "password", user["id"]])
    with app.state.factory() as db:
        assert db.get(User, user["id"]).password == original_hash


@pytest.mark.parametrize(
    "field,value",
    [
        ("latitude", "NaN"),
        ("longitude", "181"),
        ("latitude", "true"),
        ("latitude", "not a coordinate"),
    ],
)
def test_invalid_coordinates_are_rejected_before_database_or_maps_use(setup, field, value):
    _, _, contract, app = setup
    with pytest.raises(ValueError):
        command(
            app,
            [
                "chargers",
                "add",
                contract["id"],
                "--external-id",
                "INVALID",
                "--set",
                "model=Test",
                "--set",
                field + "=" + value,
            ],
        )
    assert command(app, ["chargers", "list", contract["id"]]) == []


def test_revocation_between_token_read_and_user_lookup_is_rechecked(setup, monkeypatch):
    c, h, _, app = setup
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    original_get, original_scalar = Session.get, Session.scalar
    interleaved = []

    def revoke():
        if not interleaved:
            interleaved.append(True)
            with app.state.factory() as db:
                db.execute(delete(SessionToken))
                db.commit()

    def get(db, entity, *args, **kwargs):
        if entity is User:
            revoke()
        return original_get(db, entity, *args, **kwargs)

    def scalar(db, statement, *args, **kwargs):
        columns = getattr(statement, "column_descriptions", [])
        if columns and columns[0].get("entity") is User:
            revoke()
        return original_scalar(db, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "get", get)
    monkeypatch.setattr(Session, "scalar", scalar)
    result = c.get("/api/users", headers=h)
    assert interleaved
    assert (
        result.status_code == 401
    ), "A token revoked during auth lookup cannot authorize the operation"


def test_racing_account_creation_returns_conflict_not_server_error(setup, monkeypatch):
    c, h, _, app = setup
    from sqlalchemy.orm import Session

    original_commit = Session.commit
    raced = []

    def concurrent_insert(db):
        if not raced and any(isinstance(x, User) and x.email == "racing@lem.pt" for x in db.new):
            raced.append(True)
            with app.state.factory() as other:
                other.add(
                    User(
                        id=str(uuid.uuid4()),
                        email="racing@lem.pt",
                        name="Winner",
                        role="tech",
                        password=hash_password("StrongPassword123!"),
                    )
                )
                original_commit(other)
        return original_commit(db)

    monkeypatch.setattr(Session, "commit", concurrent_insert)
    result = c.post(
        "/api/users",
        headers=h,
        json=dict(email="racing@lem.pt", name="Other", password="StrongPassword123!", role="tech"),
    )
    assert raced
    assert result.status_code == 409
    assert "password" not in result.text.lower()


def test_upgrade_initializes_account_guard_without_losing_existing_data(setup):
    _, _, _, app = setup
    cid, _, rid, pid, data = populated(setup)
    from app.models import AccountAdminLock, database

    engine = app.state.factory.kw["bind"]
    AccountAdminLock.__table__.drop(engine)
    _, upgraded = database(str(engine.url))
    with upgraded() as db:
        assert db.get(AccountAdminLock, 1) is not None
        assert db.get(Contract, cid) is not None
        assert db.get(Report, rid) is not None
        assert db.get(Photo, pid).data == data
