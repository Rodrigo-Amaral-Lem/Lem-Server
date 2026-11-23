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

    # One contract workbook can both add an ID and update another across the catalogue.
    batch = excel(("PCVE-01", "Novo modelo"), ("PCVE-NOVO", "Novo"))
    assert upload(client, admin, cid, True, batch).json() == {"created": 1, "updated": 1}
    central = {row["external_id"]: row for row in read(client, admin)}
    canonical = central["PCVE-01"]
    assert canonical["id"] == global_id and canonical["details"]["model"] == "Novo modelo"
    for contract_id in (cid, other):
        rows = client.get(f"/api/contracts/{contract_id}/chargers", headers=admin).json()
        assert rows[0]["details"] == canonical["details"]
        assert [row["station_id"] for row in rows[1:]] == ([central["PCVE-NOVO"]["id"]] if contract_id == cid else [])

    # Purging the last membership also leaves this independent record available.
    for key in (cid, other):
        assert client.post(f"/api/contracts/{key}/trash", headers=admin, json={"confirmation": "APAGAR"}).status_code == 200
    with app.state.factory() as db:
        expiry = max(datetime.fromisoformat(db.get(ContractTrash, key).purge_after) for key in (cid, other))
    assert app.state.purge_expired_contracts(expiry) == 2
    assert read(client, admin)[0] == canonical


def test_live_station_edits_preserve_unicode_and_acknowledge_exact_retries(setup):
    client, admin, _, _ = setup
    windows = {**admin, "X-Lem-Client-Platform": "windows"}
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Parque"
    sheet.append(["Identificador", "Fabricante"])
    imported_manufacturer = "Ýa Energy / Ligação Âncora"
    sheet.append(["PCVE-01", imported_manufacturer])
    data = BytesIO()
    workbook.save(data)
    mapping = {"sheet": "Parque", "columns": {"external_id": "Identificador", "manufacturer": "Fabricante"}}
    station_import(client, windows, data.getvalue(), commit=True, mapping=mapping).raise_for_status()
    station, = read(client, windows)
    assert station["details"]["manufacturer"] == imported_manufacturer
    assert station["details"]["source"]["Fabricante"] == imported_manufacturer
    manufacturer = "MÝA Charging / Ação na Âncora e ýgua"
    body = {"base_updated": station["updated"], "fields": {"manufacturer": manufacturer}}
    endpoint = f"/api/stations/{station['id']}"
    first = client.patch(endpoint, headers=windows, json=body)
    first.raise_for_status()
    retry = client.patch(endpoint, headers=windows, json=body)
    assert retry.status_code == 200 and retry.json() == first.json()
    current, = read(client, windows)
    assert current["details"]["manufacturer"] == manufacturer


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


def test_detach_preserves_reports_and_reimport_reuses_membership(setup):
    from uuid import uuid4
    from app.main import TEMPLATE

    client, admin, first, _ = setup
    cid, other = first["id"], second_contract(client, admin)
    for contract in (cid, other):
        assert upload(client, admin, contract, True, excel(("PCVE-FIXO", "Original"))).status_code == 200
    tech = technician(client, admin, other)
    links = [client.get(f"/api/contracts/{key}/chargers", headers=admin).json()[0] for key in (cid, other)]
    rid, body, original = draft(client, admin, links[0])
    other_report, _, _ = draft(client, admin, links[1])
    sid = links[0]["station_id"]
    history = f"/api/stations/{sid}/reports"
    detach = f"/api/contracts/{cid}/chargers/{links[0]['id']}"
    assert client.delete(detach, headers=tech).status_code == 403
    assert client.delete(detach.replace(cid, other), headers=admin).status_code == 404
    for attempt in range(2):
        assert client.delete(detach, headers=admin).status_code == 200
    assert client.get(f"/api/contracts/{cid}/chargers", headers=admin).json() == []
    assert len(client.get(f"/api/contracts/{other}/chargers", headers=admin).json()) == 1
    assert {r["id"] for r in client.get(history, headers=admin).json()} == {rid, other_report}
    assert {r["id"] for r in client.get(history, headers=tech).json()} == {other_report}
    assert client.get(f"/api/reports/{rid}", headers=admin).json()["snapshot"] == original["snapshot"]
    assert client.put(f"/api/reports/{rid}", headers=admin, json=body).status_code == 200
    new_body = {"checklist_version": TEMPLATE["version"], "charger_id": links[0]["id"],
                "base_revision": 0, "payload": {}}
    assert client.put(f"/api/reports/{uuid4()}", headers=admin, json=new_body).status_code == 409
    assert upload(client, admin, cid, True, excel(("PCVE-FIXO", "Atualizado"))).status_code == 200
    restored, = client.get(f"/api/contracts/{cid}/chargers", headers=admin).json()
    assert restored["id"] == links[0]["id"] and restored["station_id"] == sid
    assert restored["reports"][0]["id"] == rid


def test_import_and_upgrade_clean_live_fields_and_preserve_snapshots(setup):
    from app.models import Charger, DataMigration, Report, clean_existing_station_data

    client, admin, contract, app = setup
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Parque"
    sheet.append(["ID", "Morada", "Localização / nome do local", "ID do site", "Latitude"])
    sheet.append(["PCVE-FIXO", "ESTRADA DOS CALIÝOS, EstaÝão", "Local excluído", "OUTRO", "37.0"])
    output = BytesIO()
    book.save(output)
    mapping = {"sheet": "Parque", "repair_legacy_cedillas": True,
               "columns": {"external_id": "ID", "address": "Morada",
               "location": "Localização / nome do local", "extra.ID do site": "ID do site", "latitude": "Latitude"}}
    response = client.post(f"/api/contracts/{contract['id']}/import", headers=admin,
                           files={"file": ("dados.xlsx", output.getvalue())},
                           data={"mapping": json.dumps(mapping), "commit": "true"})
    assert response.status_code == 200, response.text
    link, = client.get(f"/api/contracts/{contract['id']}/chargers", headers=admin).json()
    assert link["details"]["address"] == "ESTRADA DOS CALIÇOS, Estação"
    assert link["details"]["latitude"] == "37.0"
    assert link["details"]["source"]["Morada"] == link["details"]["address"]
    assert not {"Localização / nome do local", "ID do site"} & link["details"]["source"].keys()
    assert not {"location", "site_id", "extra.ID do site"} & link["details"].keys()
    rid, _, before = draft(client, admin, link)
    from test_review_050 import command
    import pytest
    with pytest.raises(ValueError, match="permanente"):
        command(app, ["chargers", "update", contract["id"], link["id"], "--external-id", "OUTRO"])
    windows = {**admin, "X-Lem-Client-Platform": "windows"}
    for key in ("external_id", "site_id", "extra.ID do PCVE"):
        result = client.patch(f"/api/stations/{link['station_id']}", headers=windows,
                              json={"base_updated": link["station_updated"], "fields": {key: "OUTRO"}})
        assert result.status_code == 422
    with app.state.factory() as db:
        station = db.get(Station, link["station_id"])
        bad = {"location": "Local excluído", "site_id": "OUTRO", "address": "CALIÝOS", "latitude": "37.0",
               "source": {"Localização / nome do local": "Local excluído", "Modelo": "Leia"}}
        station.details = {**bad, "source": {**bad["source"], "Notas": "Origem desconhecida"}}
        db.get(Charger, link["id"]).legacy_details = {**bad, "extra.Notas": "Apenas deste contrato"}
        db.delete(db.get(DataMigration, "pcve-contract-notes-v1"))
        engine = db.get_bind()
        db.commit()
    for attempt in range(2):
        clean_existing_station_data(engine)
    with app.state.factory() as db:
        clean = db.get(Station, link["station_id"]).details
        assert clean == {"address": "CALIÇOS", "latitude": "37.0", "source": {"Modelo": "Leia"}}
        assert db.get(Charger, link["id"]).legacy_details == clean
        assert db.get(Charger, link["id"]).contract_note.text == "Apenas deste contrato"
        assert db.get(Charger, link["id"]).contract_note.revision == 1
        assert db.get(Report, rid).snapshot == before["snapshot"]
