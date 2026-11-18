# SPDX-License-Identifier: GPL-3.0-only
"""Private administrator working copies; replacement is one database transaction.

No old report or evidence is removed until the complete corrected copy validates.
Report IDs are never reused; a committed correction also records its replacement link.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import uuid
from fastapi import HTTPException
from sqlalchemy import select, update, delete, literal
from .models import Report, ReportCorrection, DeletedReport, Photo, Charger, Station, User, now
from .stations import close_identity, identity_values, lock_catalogue, StationConflict, IDENTITY_FIELDS
from .maintenance_migration import persist_upgrade


def desktop_admin(db, user):
    if user.role != "admin" or db.info.get("client_platform") not in ("windows", "linux"):
        raise HTTPException(403, "A correção de reports fechados exige administrador na app desktop Windows ou Linux")


def lock_charger(db, charger_id):
    # SQLite ignores FOR UPDATE. Acquire its writer lock before re-reading the
    # source/correction state, so begin/cancel/delete cannot pass stale checks.
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(Charger).where(Charger.id == charger_id).values(id=Charger.id)
                   .execution_options(synchronize_session=False))
    return db.scalar(select(Charger).where(Charger.id == charger_id).with_for_update())


def lock_report_users(db, rid):
    # Account deletion locks users before their reports. FK inserts/restoring the
    # original owner must take the same order to avoid a PostgreSQL lock cycle.
    report = db.get(Report, rid)
    correction = db.get(ReportCorrection, rid)
    owners = {report.owner_id} if report else set()
    if correction:
        owners.add(correction.original_owner_id)
    if owners:
        list(db.scalars(select(User).where(User.id.in_(owners)).order_by(User.id).with_for_update(read=True)))


def gate(db, user, rid):
    correction = db.get(ReportCorrection, rid)
    if correction and correction.state == "editing":
        desktop_admin(db, user)
        if correction.editor_id != user.id:
            raise HTTPException(403, "Esta cópia de correção pertence a outro administrador")
    return correction


def metadata(correction):
    return {"source_id": correction.source_id, "source_revision": correction.source_revision,
            "editor_id": correction.editor_id, "state": correction.state,
            "photo_map": correction.photo_map if correction.state == "editing" else {}}


def replaced_detail(db, rid):
    replacement = db.scalar(select(ReportCorrection.id).where(
        ReportCorrection.source_id == rid, ReportCorrection.state == "committed"))
    return ({"message": "Report substituído por uma correção do administrador; conserve alterações locais pendentes",
             "reason": "report_replaced", "replacement_id": replacement} if replacement
            else "Report apagado definitivamente")


def checked_times(payload):
    parsed = []
    for key in ("started", "finished"):
        value = payload.get(key)
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise HTTPException(422, "Indique início e fim válidos, com fuso horário") from None
        parsed.append(instant.astimezone(timezone.utc))
    if parsed[1] < parsed[0]:
        raise HTTPException(422, "O fim não pode ser anterior ao início")
    return [value.isoformat(timespec="seconds") for value in parsed]


def begin(db, user, source, new_id, revision, current_template=None):
    desktop_admin(db, user)
    try:
        uuid.UUID(new_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "UUID inválido") from None
    charger = lock_charger(db, source.charger_id)
    existing = db.get(ReportCorrection, new_id)
    if existing:
        if (existing.source_id, existing.source_revision, existing.editor_id, existing.state) != (
                source.id, revision, user.id, "editing"):
            raise HTTPException(409, "Este identificador já pertence a outra operação")
        report = db.get(Report, new_id)
        persist_upgrade(db, report, current_template, correction=True)
        return report
    source = db.scalar(select(Report).where(Report.id == source.id).with_for_update()
                       .execution_options(populate_existing=True))
    if not source or source.status != "closed" or source.revision != revision:
        raise HTTPException(409, "O report original mudou; atualize antes de iniciar a correção")
    if db.get(Report, new_id) or db.get(DeletedReport, new_id):
        raise HTTPException(409, "Identificador já utilizado")
    # The report snapshot records an earlier intervention. Read the live station
    # afresh for conflict detection and keep that baseline across begin retries.
    station = db.get(Station, charger.station_id, populate_existing=True) if charger.station_id else None
    identity_baseline = identity_values(station.details or {}) if station is not None else None
    payload = deepcopy(source.payload)
    payload["started"] = payload.get("started") or source.started
    payload["finished"] = payload.get("finished") or source.finished
    report = Report(id=new_id, charger_id=source.charger_id, owner_id=user.id, revision=1,
                    status="draft", payload=deepcopy(payload), snapshot=deepcopy(source.snapshot),
                    template=deepcopy(source.template), started=source.started)
    db.add(report)
    db.flush()
    # Clone immutable bytes inside the database: no download/upload round trip,
    # and no report-sized Python buffer. Each copy owns independent photo IDs.
    photo_map = {pid: str(uuid.uuid4()) for pid in db.scalars(select(Photo.id).where(Photo.report_id == source.id))}
    for old_id, new_photo in photo_map.items():
        db.execute(Photo.__table__.insert().from_select(
            ["id", "report_id", "checkpoint", "filename", "mime", "digest", "data", "created"],
            select(literal(new_photo), literal(new_id), Photo.checkpoint, Photo.filename,
                   Photo.mime, Photo.digest, Photo.data, Photo.created).where(Photo.id == old_id)))
    # A second correction clones historical evidence too. Remap both reference
    # sets so archived signatures never point to a report removed on commit.
    for field in ("signatures", "legacy_signatures"):
        for signature in payload.get(field, {}).values():
            signature["photo_id"] = photo_map.get(signature["photo_id"], signature["photo_id"])
    report.payload = deepcopy(payload)
    persist_upgrade(db, report, current_template, correction=True)
    correction = ReportCorrection(id=new_id, contract_id=charger.contract_id,
        source_id=source.id, source_revision=revision, editor_id=user.id,
        original_owner_id=source.owner_id, photo_map=photo_map, identity_baseline=identity_baseline)
    db.add(correction)
    db.flush()
    return report


def commit(db, user, report, body, validate, current_template=None):
    desktop_admin(db, user)
    correction = db.get(ReportCorrection, report.id)
    if not correction or correction.editor_id != user.id:
        raise HTTPException(403, "Cópia de correção não pertence a este administrador")
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    if correction.state == "committed":
        if correction.commit_digest != fingerprint:
            raise HTTPException(409, "A correção já foi guardada com outros dados")
        return report
    # Ordinary closure/import lock the shared catalogue before charger rows.
    # Corrections can also change station identity: taking these in reverse order
    # lets a concurrent closure hold the catalogue while waiting for our charger.
    lock_catalogue(db)
    lock_charger(db, report.charger_id)
    # Fixed lock order for two administrators correcting the same original.
    locked = {r.id: r for r in db.scalars(select(Report).where(
        Report.id.in_((correction.source_id, report.id))).order_by(Report.id).with_for_update()
        .execution_options(populate_existing=True))}
    source, report = locked.get(correction.source_id), locked.get(report.id)
    if not source or source.status != "closed" or source.revision != correction.source_revision:
        raise HTTPException(409, "O original foi substituído ou alterado; esta cópia de correção foi conservada")
    if report and report.status == "draft":
        persist_upgrade(db, report, current_template)
    if (not report or report.status != "draft" or report.owner_id != user.id
            or report.revision != body.base_revision or body.charger_id != report.charger_id
            or body.checklist_version != report.template.get("version") or body.status != "closed"):
        raise HTTPException(409, "A cópia de correção mudou; atualize antes de guardar")
    started, finished = checked_times(body.payload)
    validate(db, report, body)
    # Station identity and report replacement share this transaction. Use the
    # historical answers to identify edits, and the opening catalogue values to
    # detect conflicting writes made while the copy was open.
    # Legacy reports can already have answers different from their snapshot.
    # Editing only notes must not publish those old, untouched identity answers.
    original_answers = source.payload.get("answers", {}) if source.template.get("version", 0) >= 6 else {}
    proposed_answers = body.payload.get("answers", {})
    identity_answers = {
        key: proposed_answers.get(key, {}) for key in IDENTITY_FIELDS
        if proposed_answers.get(key, {}).get("value", "").strip()
        != original_answers.get(key, {}).get("value", "").strip()
    }
    try:
        snapshot = close_identity(db, db.get(Charger, report.charger_id), report,
                                  {"answers": identity_answers}, user.id, correction=correction)
    except StationConflict as error:
        raise HTTPException(409, {"reason": "station_identity_conflict", "message": str(error)}) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    changed = db.execute(update(Report).where(Report.id == report.id, Report.status == "draft",
        Report.revision == body.base_revision, Report.owner_id == user.id).values(
        payload=body.payload, snapshot=snapshot, status="closed", owner_id=correction.original_owner_id,
        revision=body.base_revision + 1, started=started, finished=finished, updated=now())
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(409, "A correção foi alterada noutro dispositivo; cópia conservada")
    db.execute(delete(Photo).where(Photo.report_id == source.id))
    claimed = db.execute(delete(Report).where(Report.id == source.id, Report.status == "closed",
        Report.revision == correction.source_revision).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise HTTPException(409, "Outro administrador substituiu o original; cópia conservada")
    db.add(DeletedReport(id=source.id, charger_id=source.charger_id, owner_id=source.owner_id, status="closed"))
    correction.state = "committed"
    correction.commit_digest = fingerprint
    correction.committed = now()
    db.flush()
    db.expire_all()
    return db.get(Report, report.id)
