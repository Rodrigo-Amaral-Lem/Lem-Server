# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Independent station identity, contract membership, and verified field updates."""
from copy import deepcopy
from sqlalchemy import select, update
from .models import Station, StationCatalogueLock, Charger, now
from .measurements import not_applicable
import uuid

IDENTITY_FIELDS = {'EST.02': 'manufacturer', 'EST.03': 'model', 'EST.04': 'serial'}


class StationConflict(ValueError):
    pass


def lock_catalogue(db):
    db.scalar(select(StationCatalogueLock).where(StationCatalogueLock.id == 1).with_for_update())


def merge_import(station, incoming):
    # Once confirmed by a technician, a stale workbook must not undo the correction.
    verified = station.verified_fields or {}
    station.details = {**(station.details or {}), **{k:v for k,v in incoming.items() if k not in verified}}
    station.updated = now()


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


def close_identity(db, charger, report, payload, actor_id, *, correcting_closed=False):
    snapshot = deepcopy(report.snapshot)
    # Explicit administrator corrections also support historical templates whose
    # EST identity fields predate automatic station updates on ordinary closure.
    if report.template.get('version', 0) < 4 and not correcting_closed:
        return snapshot
    changes = {}
    original = snapshot.get('charger', {})
    for checkpoint, field in IDENTITY_FIELDS.items():
        value = payload.get('answers', {}).get(checkpoint, {}).get('value', '').strip()
        if value and not not_applicable(value) and value != str(original.get(field, '') or '').strip():
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
    for field, value in changes.items():
        previous = str(original.get(field, '') or '').strip()
        if str(current.get(field, '') or '').strip() not in (previous, value):
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
    db.execute(update(Charger).where(Charger.station_id == charger.station_id).values(external_id=external_id))
    charger.external_id = external_id
