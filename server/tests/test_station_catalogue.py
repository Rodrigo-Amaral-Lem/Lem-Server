# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""The independent PCVE catalogue projects the same station into all contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
import json
from threading import Barrier

import openpyxl
from app.models import ContractTrash, Station
from test_stations_055 import draft
from test_workflow import MAPPING, setup, upload


def excel(*rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Parque"
    sheet.append(["Identificador", "Modelo", "Local", "Série", "CPE", "Custom"])
    for identifier, model in rows:
        sheet.append([identifier, model, "Aveiro", "000012", "PT0002", "Parque Norte"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def station_import(client, headers, data, *, commit=False, mapping=MAPPING):
    return client.post("/api/stations/import", headers=headers,
                       files={"file": ("stations.xlsx", data)},
                       data={"mapping": json.dumps(mapping), "commit": str(commit).lower()})


def read(client, headers):
    response = client.get("/api/stations", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def second_contract(client, headers):
    response = client.post("/api/contracts", headers=headers,
                           json={"company": "Galp", "year": 2026, "semester": 2, "kind": "TRE"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def technician(client, admin, *contracts):
    account = {"email": "catalogue@example.test", "name": "Técnico",
               "password": "PcveFixturePassword123!", "role": "tech"}
    uid = client.post("/api/users", headers=admin, json=account).json()["id"]
    for cid in contracts:
        assert client.put(f"/api/contracts/{cid}/users/{uid}", headers=admin).status_code == 200
    token = client.post("/api/login", json={"email": account["email"], "password": account["password"]}).json()["token"]
    return {"Authorization": f"Bearer {token}", "X-Lem-Client-Platform": "windows"}


def test_direct_import_then_contract_import_keeps_one_station(setup):
    client, admin, first, app = setup
    windows = {**admin, "X-Lem-Client-Platform": "windows"}
    cid = first["id"]
    book = excel(("PCVE-01", "Original"), ("PCVE-LIVRE", "Sem contrato"))
    preview = station_import(client, windows, book)
    assert preview.status_code == 200 and preview.json()["count"] == 2
    assert read(client, admin) == []
    assert station_import(client, windows, book, commit=True).json() == {"created": 2, "updated": 0}
    assert client.get(f"/api/contracts/{cid}/chargers", headers=admin).json() == []
    saved = read(client, admin)
    global_id = saved[0]["id"]
    assert station_import(client, windows, book, commit=True).json() == {"created": 0, "updated": 2}
    assert read(client, admin) == saved
    # A bad row rejects the whole batch even when the first row would update a station.
    bad = excel(("PCVE-01", "Não guardar"), ("REPETIDO", "A"), ("REPETIDO", "B"))
    assert station_import(client, windows, bad, commit=True).status_code == 422
    assert read(client, admin) == saved

    other = second_contract(client, admin)
    assert upload(client, admin, cid, True, excel(("PCVE-01", "Primeiro"))).json()["created"] == 1
    assert upload(client, admin, other, True, excel(("PCVE-01", "Segundo"))).json()["created"] == 1
    canonical, _ = read(client, admin)
    assert canonical["id"] == global_id and canonical["details"]["model"] == "Segundo"
    links = [client.get(f"/api/contracts/{key}/chargers", headers=admin).json()[0] for key in (cid, other)]
    assert links[0]["id"] != links[1]["id"]
    assert all(link["station_id"] == global_id and link["station_updated"] == canonical["updated"] and
               link["details"] == canonical["details"] for link in links)

    # Purging the last membership also leaves this independent record available.
    for key in (cid, other):
        assert client.post(f"/api/contracts/{key}/trash", headers=admin, json={"confirmation": "APAGAR"}).status_code == 200
    with app.state.factory() as db:
        expiry = max(datetime.fromisoformat(db.get(ContractTrash, key).purge_after) for key in (cid, other))
    assert app.state.purge_expired_contracts(expiry) == 2
    assert read(client, admin)[0] == canonical


def test_technician_reads_one_station_per_accessible_assignment(setup):
    client, admin, first, _ = setup
    cid = first["id"]
    other = second_contract(client, admin)
    windows = {**admin, "X-Lem-Client-Platform": "windows"}
    assert station_import(client, windows, excel(("PCVE-LIVRE", "Sem contrato")), commit=True).status_code == 200
    for key in (cid, other):
        assert upload(client, admin, key, True, excel(("PCVE-COMUM", "Partilhado"), (key, "Só neste contrato"))).status_code == 200
    tech = technician(client, admin, cid)
    assert {row["external_id"] for row in read(client, tech)} == {cid, "PCVE-COMUM"}
    uid = next(user["id"] for user in client.get("/api/users", headers=admin).json() if user["role"] == "tech")
    assert client.put(f"/api/contracts/{other}/users/{uid}", headers=admin).status_code == 200
    assert [row["external_id"] for row in read(client, tech)].count("PCVE-COMUM") == 1
    assert client.post(f"/api/contracts/{cid}/trash", headers=admin, json={"confirmation": "APAGAR"}).status_code == 200
    assert {row["external_id"] for row in read(client, tech)} == {other, "PCVE-COMUM"}


def test_edit_projects_into_all_contracts_and_rejects_stale_writes(setup):
    client, admin, first, app = setup
    cid = first["id"]
    other = second_contract(client, admin)
    linux = {**admin, "X-Lem-Client-Platform": "linux"}
    book = excel(("PCVE-01", "Original"))
    for key in (cid, other):
        assert upload(client, admin, key, True, book).status_code == 200
    old = read(client, admin)[0]
    path = f"/api/stations/{old['id']}"
    membership = client.get(f"/api/contracts/{cid}/chargers", headers=admin).json()[0]
    rid, _, report = draft(client, admin, membership)
    with app.state.factory() as db:
        station = db.get(Station, old["id"])
        station.verified_fields = {"model": {"report_id": rid}}
        db.commit()
    patch = {"base_updated": old["updated"], "fields": {"model": "Confirmado", "latitude": "0", "location": ""}}
    response = client.patch(path, headers=linux, json=patch)
    assert response.status_code == 200, response.text
    new = response.json()
    assert new["updated"] != old["updated"] and client.patch(path, headers=linux, json=patch).json() == new
    assert new["details"]["source"] == old["details"]["source"]
    assert new["details"]["serial"] == "000012"
    assert client.patch(path, headers=linux, json={**patch, "fields": {"model": "Desatualizado"}}).status_code == 409
    for invalid in ({"external_id": "DUPLICADO"}, {"latitude": "180"}, {"source": "Não"}):
        assert client.patch(path, headers=linux, json={"base_updated": new["updated"], "fields": invalid}).status_code == 422
    for key in (cid, other):
        link = client.get(f"/api/contracts/{key}/chargers", headers=admin).json()[0]
        assert link["details"] == new["details"] and link["station_updated"] == new["updated"]
    assert client.get(f"/api/reports/{rid}", headers=admin).json()["snapshot"] == report["snapshot"]
    assert upload(client, admin, other, True, excel(("PCVE-01", "Excel desatualizado"))).status_code == 200
    assert read(client, admin)[0]["details"]["model"] == "Confirmado"


def test_desktop_permissions_for_direct_import_and_edit(setup):
    client, admin, first, _ = setup
    tech = technician(client, admin, first["id"])
    data = excel(("PCVE-01", "Original"))
    mac = {**admin, "X-Lem-Client-Platform": "macos"}
    assert station_import(client, mac, data, commit=True).status_code == 200
    android = {**admin, "X-Lem-Client-Platform": "android"}
    current = read(client, android)[0]
    for denied in (tech, android, admin):
        for commit in (False, True):
            assert station_import(client, denied, data, commit=commit).status_code == 403
        assert client.patch(f"/api/stations/{current['id']}", headers=denied,
                            json={"base_updated": current["updated"], "fields": {"model": "Bloqueado"}}).status_code == 403
    assert read(client, admin) == [current]


def test_concurrent_contract_and_direct_import_share_identity(setup):
    client, admin, first, _ = setup
    desktop = {**admin, "X-Lem-Client-Platform": "windows"}
    start = Barrier(2)
    book = excel(("PCVE-01", "Partilhado"))

    def direct():
        start.wait(timeout=5)
        return station_import(client, desktop, book, commit=True)

    def contract():
        start.wait(timeout=5)
        return upload(client, admin, first["id"], True, book)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = [future.result(timeout=15) for future in (pool.submit(direct), pool.submit(contract))]
    assert all(reply.status_code == 200 for reply in replies), [reply.text for reply in replies]
    station, = read(client, admin)
    link, = client.get(f"/api/contracts/{first['id']}/chargers", headers=admin).json()
    assert station["id"] == link["station_id"]
