# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Independent station identity, contract membership, and verified field updates."""
from copy import deepcopy
from datetime import datetime
import re
from sqlalchemy import select, update
from .checklist import connector_names, connector_text
from .import_profiles import FIELDS
from .models import Station, StationCatalogueLock, Charger, now
from .measurements import not_applicable
import uuid

IDENTITY_FIELDS = {'EST.02': 'manufacturer', 'EST.03': 'model', 'EST.04': 'serial'}


class StationConflict(ValueError):
    pass


EDITABLE_FIELDS = (set(FIELDS) - {"external_id"}) | {"power_unit"}


def lock_catalogue(db):
    # PostgreSQL locks the singleton row. SQLite ignores FOR UPDATE, so acquire
    # its writer lock before reading the IDs/revisions that control the mutation.
    lock = StationCatalogueLock
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(lock).where(lock.id == 1).values(id=lock.id))
    else:
        db.scalar(select(lock).where(lock.id == 1).with_for_update())


def station_record(station):
    return {"id": station.id, "external_id": station.external_id,
            "details": station.details or {}, "updated": station.updated}


def stations_for_import(db, records):
    ids = [record["external_id"] for record in records]
    stations = {}
    for offset in range(0, len(ids), 500):
        rows = db.scalars(select(Station).where(Station.external_id.in_(ids[offset:offset + 500])))
        stations.update((row.external_id, row) for row in rows)
    return stations


def merge_import(station, incoming):
    # Once confirmed by a technician, a stale workbook must not undo the correction.
    verified = station.verified_fields or {}
    details = station.details or {}
    merged = {**details, **{key: value for key, value in incoming.items() if key not in verified}}
    if merged != details:
        station.details = merged
        station.updated = now()


def edit_station(station, base_updated, edits, actor_id):
    details = station.details or {}
    changes = {}
    for field, raw in edits.items():
        if field not in EDITABLE_FIELDS and not (field.startswith("extra.") and field in details):
            raise ValueError("Campo da estação inválido: " + field[:100])
        value = raw.strip()
        if len(value) > 32767:
            raise ValueError("O valor excede o limite de uma célula Excel: " + field)
        if field in ("latitude", "longitude") and value:
            number = value.replace(",", ".")
            limit = 90 if field == "latitude" else 180
            if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", number) or abs(float(number)) > limit:
                raise ValueError("Coordenada inválida: " + field)
        if field == "connectors":
            value = connector_text(value)
            connector_names(value)
        if details.get(field) != value:
            changes[field] = value
    if not changes:
        # A retry after a lost acknowledgement is harmless when all fields
        # already match the requested edit.
        return False
    if base_updated != station.updated:
        raise StationConflict("A ficha mudou no servidor. Atualize os IDs e volte a abrir a edição.")
    station.details = {**details, **changes}
    station.updated = now()
    # Keep the protection of previously verified identity fields. Record the
    # administrator as the source of any replacement for a verified value.
    verified = station.verified_fields or {}
    station.verified_fields = {**verified, **{
        field: {"user_id": actor_id, "updated": station.updated, "source": "admin"}
        for field in changes if field in verified
    }}
    return True


def get_or_create_station(db, external_id, details, existing=None):
    station = existing.get(external_id) if existing is not None else db.scalar(select(Station).where(Station.external_id == external_id))
    if station is None:
        station = Station(id=str(uuid.uuid4()), external_id=external_id, details=dict(details), verified_fields={})
        db.add(station)
        if existing is not None:
            existing[external_id] = station
    else:
        merge_import(station, details)
    return station


def identity_values(details):
    return {field: str(details.get(field) or "").strip() for field in IDENTITY_FIELDS.values()}


def correction_identity_reference(correction, station, historical, current):
    if correction.identity_baseline is not None:
        return correction.identity_baseline
    # Existing working copies have no opening baseline. The current identity is
    # safe to use only if the last catalogue write strictly predates their opening.
    # With missing or ambiguous dates, keep the historical conflict protection.
    try:
        updated = datetime.fromisoformat(station.updated)
        opened = datetime.fromisoformat(correction.created)
        if updated.utcoffset() is not None and opened.utcoffset() is not None and updated < opened:
            return current
    except (TypeError, ValueError):
        pass
    return historical


def close_identity(db, charger, report, payload, actor_id, *, correction=None):
    snapshot = deepcopy(report.snapshot)
    # Explicit administrator corrections also support historical templates whose
    # EST identity fields predate automatic station updates on ordinary closure.
    if report.template.get('version', 0) < 4 and correction is None:
        return snapshot
    changes = {}
    original = snapshot.get('charger', {})
    historical = identity_values(original)
    for checkpoint, field in IDENTITY_FIELDS.items():
        value = payload.get('answers', {}).get(checkpoint, {}).get('value', '').strip()
        if value and not not_applicable(value) and value != historical[field]:
            if len(value) > 500:
                raise ValueError(f'{checkpoint}: máximo 500 caracteres')
            changes[field] = value
    if not changes:
        return snapshot
    lock_catalogue(db)
    # Lock/read the physical station, not just this contract membership.
    station = db.scalar(select(Station).where(Station.id == charger.station_id).with_for_update()
                        .execution_options(populate_existing=True))
    if station is None:
        raise ValueError('Ficha do carregador indisponível')
    current = dict(station.details or {})
    current_identity = identity_values(current)
    reference = correction_identity_reference(correction, station, historical, current_identity) if correction else historical
    for field, value in changes.items():
        if current_identity[field] not in (reference[field], value):
            raise StationConflict('Os dados do carregador foram corrigidos noutro report; atualize antes de concluir. Campo: ' + field)
    station.details = {**current, **changes}
    station.updated = now()
    station.verified_fields = {**(station.verified_fields or {}), **{
        field: {'report_id': report.id, 'user_id': actor_id, 'updated': station.updated}
        for field in changes
    }}
    snapshot['charger'] = {**original, **changes}
    return snapshot


def rename_station(db, charger, external_id):
    lock_catalogue(db)
    duplicate = db.scalar(select(Station).where(Station.external_id == external_id))
    if duplicate is not None and duplicate.id != charger.station_id:
        raise ValueError('Já existe outro carregador com esse ID')
    charger.station.external_id = external_id
    charger.station.updated = now()
    db.execute(update(Charger).where(Charger.station_id == charger.station_id).values(external_id=external_id))
    charger.external_id = external_id
