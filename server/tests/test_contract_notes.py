# SPDX-License-Identifier: GPL-3.0-only
"""Contract instructions stay with a membership across import, editing and report access."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from threading import Barrier

import openpyxl
import pytest
from app.import_profiles import STATIONS_COLUMNS
from app.importer import analyse
from test_station_catalogue import second_contract, technician
from test_stations_055 import draft
from test_workflow import setup


ID = "PCVE-001"
ABSENT = object()


def book(notes=ABSENT, *, custom=False):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Stations"
    headers = ["Identificador", "Modelo"] if custom else [label for _, _, label, _ in STATIONS_COLUMNS]
    row = [ID, "Modelo"] if custom else [ID, "Rua", "", "", "", "Norte", "", "", "", "Marca", "", "", "", "CCS2"]
    if notes is not ABSENT:
        headers.append("Notas")
        row.append(notes)
    sheet.append(headers)
    sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def send_book(client, headers, path, data, *, commit=True, columns=None):
    response = client.post(path, headers=headers, files={"file": ("IDs.xlsx", data)},
                           data={"mapping": json.dumps({"sheet": "Stations", "columns": columns or {}}),
                                 "commit": str(commit).lower()})
    assert response.status_code == 200, response.text
    return response.json()


def test_contract_import_scope_and_legacy_reimport(setup):
    client, admin, contract, _ = setup
    cid, other = contract["id"], second_contract(client, admin)
    root = f"/api/contracts/{cid}"
    message = "  Chave no portão\nConfirmar com a receção.  "
    data = book(message)
    mapping = analyse(data)["sheets"][0]["recommended_mapping"]["columns"]
    assert mapping["contract_notes"] == "Notas"
    assert "contract_notes" not in analyse(book())["sheets"][0]["recommended_mapping"]["columns"]
    preview, = send_book(client, admin, root + "/import", data, commit=False)["records"]
    assert preview["contract_notes"] == message and message not in str(preview["details"])
    assert not client.get(root + "/chargers", headers=admin).json()
    send_book(client, admin, root + "/import", data)
    custom = {"external_id": "Identificador", "model": "Modelo", "extra.Notas": "Notas"}
    send_book(client, admin, f"/api/contracts/{other}/import", book("Segunda chave", custom=True), columns=custom)
    first, = client.get(root + "/chargers", headers=admin).json()
    second, = client.get(f"/api/contracts/{other}/chargers", headers=admin).json()
    assert first["station_id"] == second["station_id"] and first["id"] != second["id"]
    assert first["contract_notes"] == message and second["contract_notes"] == "Segunda chave"
    assert "Notas" not in first["details"]["source"]
    url = root + "/chargers/" + first["id"] + "/notes"
    rid, _, frozen = draft(client, admin, first)
    assert "Confirmar com a receção." not in str(frozen)
    windows = {**admin, "X-Lem-Client-Platform": "windows"}
    for commit in (False, True):
        result = send_book(client, windows, "/api/stations/import", book("Nunca na ficha"), commit=commit)
        assert "Nunca na ficha" not in str(result)
    assert "Notas" not in str(client.get("/api/stations", headers=admin).json())
    for workbook in (book(), data):
        send_book(client, admin, root + "/import", workbook)
        current = client.get(url, headers=admin).json()
        assert current["contract_notes"] == message and current["notes_revision"] == 1
    send_book(client, admin, root + "/import", book(" \t\n\u200b\u200d\ufeff\ufe0f\u2800"))
    current = client.get(url, headers=admin).json()
    assert current["contract_notes"] == "" and current["notes_revision"] == 2
    assert client.get(f"/api/reports/{rid}", headers=admin).json()["snapshot"] == frozen["snapshot"]
    assert client.get(f"/api/contracts/{other}/chargers", headers=admin).json()[0]["contract_notes"] == "Segunda chave"


def test_permissions_retries_concurrency_and_detached_history(setup):
    client, admin, contract, _ = setup
    cid, other = contract["id"], second_contract(client, admin)
    root = f"/api/contracts/{cid}"
    send_book(client, admin, root + "/import", book())
    charger, = client.get(root + "/chargers", headers=admin).json()
    url = root + "/chargers/" + charger["id"] + "/notes"
    tech = technician(client, admin, other)
    body = {"base_revision": 0, "notes": "Entrada A"}
    assert client.get(url, headers=tech).status_code == 403
    assert client.put(url, headers=tech, json=body).status_code == 403
    assert client.get(url.replace(cid, other), headers=admin).status_code == 404
    users = client.get("/api/users", headers=admin).json()
    uid = next(u["id"] for u in users if u["role"] == "tech")
    assert client.put(root + "/users/" + uid, headers=admin).status_code == 200
    assert client.get(url, headers=tech).json()["contract_notes"] == ""
    assert client.put(url, headers=tech, json=body).status_code == 403
    mobile = {**admin, "X-Lem-Client-Platform": "android"}
    barrier = Barrier(2)

    def edit(value):
        barrier.wait(timeout=8)
        return client.put(url, headers=mobile, json={**body, "notes": value})

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(edit, ["Entrada A", "Entrada B"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(r.json() for r in results if r.status_code == 200)
    assert winner["notes_revision"] == 1
    assert client.put(url, headers=mobile, json={**body, "notes": winner["contract_notes"]}).json() == winner
    assert client.get(url, headers=tech).json() == winner
    assert client.delete(url.removesuffix("/notes"), headers=admin).status_code == 200
    assert client.get(url, headers=tech).json() == {**winner, "attached": False}
    assert client.put(url, headers=admin, json=body).status_code == 409
    send_book(client, admin, root + "/import", book())
    assert client.get(url, headers=tech).json() == winner
    clear = client.put(url, headers=mobile, json={"base_revision": 1, "notes": " \u200b "})
    assert clear.json()["contract_notes"] == "" and clear.json()["notes_revision"] == 2


@pytest.mark.parametrize("text,status", [("x" * 32767, 200), ("x" * 32768, 422), ("x\0y", 422)])
def test_note_text_limits(setup, text, status):
    client, admin, contract, _ = setup
    root = f"/api/contracts/{contract['id']}"
    send_book(client, admin, root + "/import", book())
    charger, = client.get(root + "/chargers", headers=admin).json()
    url = root + "/chargers/" + charger["id"] + "/notes"
    assert client.put(url, headers=admin, json={"base_revision": 0, "notes": text}).status_code == status
    assert client.get(url, headers=admin).json()["contract_notes"] == (text if status == 200 else "")
