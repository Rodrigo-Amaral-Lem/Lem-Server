"""Behavioral checks for bounded query work and keeping uploads off the event loop."""

import asyncio
import importlib
from sqlalchemy import event, inspect
from app.models import database
from test_workflow import setup, upload, xlsx


def test_import_uses_one_charger_lookup_for_many_rows(setup):
    client, headers, contract, app = setup
    engine = app.state.factory.kw["bind"]
    lookups = []

    def record(conn, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT") and "FROM chargers" in statement:
            lookups.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        data = xlsx(tuple(f"TEST-{i:04d}" for i in range(100)))
        for expected in ({"created": 100, "updated": 0}, {"created": 0, "updated": 100}):
            lookups.clear()
            response = upload(client, headers, contract["id"], True, data)
            assert response.status_code == 200, response.text
            assert response.json() == expected
            assert len(lookups) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_excel_processing_runs_outside_event_loop(setup, monkeypatch):
    client, headers, contract, app = setup
    main = importlib.import_module("app.main")
    executed = []

    def checked(function):
        def run(*args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                executed.append(function.__name__)
            else:
                raise AssertionError("Excel processing blocks the ASGI event loop")
            return function(*args, **kwargs)
        return run

    monkeypatch.setattr(main, "analyse", checked(main.analyse))
    monkeypatch.setattr(main, "parse", checked(main.parse))
    response = client.post("/api/imports/analyse", headers=headers,
                           files={"file": ("test.xlsx", xlsx())})
    assert response.status_code == 200, response.text
    assert upload(client, headers, contract["id"], True).status_code == 200
    assert executed == ["analyse", "parse"]


def test_indexes_upgrade_existing_tables_without_reset(tmp_path):
    url = "sqlite:///" + str(tmp_path / "existing.db")
    engine, _ = database(url)
    names = {"reports": "ix_reports_charger_updated",
             "photos": "ix_photos_report",
             "deleted_reports": "ix_deleted_reports_charger"}
    with engine.begin() as conn:
        for name in names.values():
            conn.exec_driver_sql(f'DROP INDEX "{name}"')
        conn.exec_driver_sql("INSERT INTO users VALUES ('kept', 'kept@example.org', 'Kept', 'hash', 'tech')")
    engine.dispose()
    for _ in range(2):
        engine, _ = database(url)
        for table, name in names.items():
            assert name in {i["name"] for i in inspect(engine).get_indexes(table)}
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT name FROM users WHERE id='kept'").scalar() == "Kept"
        engine.dispose()
