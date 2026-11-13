# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from app.main import TEMPLATE
"""Second review: destructive prompts and charger/native presentation boundaries."""

import uuid
import pytest
from app.models import Charger, Report, Photo
from app import admin_cli as cli
from test_workflow import setup
from test_admin_cli import command
from test_contract_trash import populated


def test_report_changed_during_delete_confirmation_is_preserved(setup, monkeypatch):
    _, _, _, app = setup
    _, _, rid, pid, _ = populated(setup)

    def edit_while_operator_decides(_):
        with app.state.factory() as db:
            r = db.get(Report, rid)
            r.revision += 1
            r.payload = {"answers": {}, "field_work": "new work after deletion preview"}
            r.status = "closed"
            db.commit()
        return "APAGAR"

    monkeypatch.setattr("builtins.input", edit_while_operator_decides)
    with pytest.raises(ValueError, match="alterado"):
        command(app, ["reports", "delete", rid])
    with app.state.factory() as db:
        assert db.get(Report, rid).payload["field_work"] == "new work after deletion preview"
        assert db.get(Photo, pid) is not None


def test_authoritative_station_id_cannot_be_overridden_in_snapshot(setup):
    c, h, contract, app = setup
    charger = command(
        app, ["chargers", "add", contract["id"], "--external-id", "REAL-ID", "--set", "model=Test"]
    )
    # Also protect older/imported database metadata, not just new console input.
    with app.state.factory() as db:
        db.get(Charger, charger["id"]).details = {"model": "Test", "external_id": "WRONG-ID"}
        db.commit()
    result = c.put(
        f"/api/reports/{uuid.uuid4()}",
        headers=h,
        json=dict(checklist_version=TEMPLATE["version"],charger_id=charger["id"], base_revision=0, payload={"answers": {}}),
    )
    assert result.status_code == 200, result.text
    assert result.json()["snapshot"]["charger"]["external_id"] == "REAL-ID"


def test_console_rejects_reserved_station_id_in_details(setup):
    _, _, contract, app = setup
    with pytest.raises(ValueError, match="external_id"):
        command(
            app,
            [
                "chargers",
                "add",
                contract["id"],
                "--external-id",
                "REAL-ID",
                "--set",
                "model=Test",
                "--set",
                "external_id=WRONG-ID",
            ],
        )


@pytest.mark.parametrize("value", ["1e1", "1_0", "+1e1", "40,123"])
def test_coordinates_accepted_by_console_are_readable_by_qml(setup, value):
    _, _, contract, app = setup
    row = command(
        app,
        [
            "chargers",
            "add",
            contract["id"],
            "--external-id",
            "COORD",
            "--set",
            "model=Test",
            "--set",
            "latitude=" + value,
            "--set",
            "longitude=-8.5",
        ],
    )
    # QML's strict parser accepts ordinary decimal syntax, not Python float extensions.
    import re

    assert re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", str(row["details"]["latitude"]).replace(",", ".")
    ), row
