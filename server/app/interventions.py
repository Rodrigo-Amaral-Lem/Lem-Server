# SPDX-License-Identifier: GPL-3.0-only
"""Station interventions and dated commercial coverage, independent of preventive visits."""
import calendar
import hashlib
import json
from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from .import_profiles import normalize_contract_notes
from .importer import cell_text, workbook
from .models import (Assignment, Charger, Contract, ContractChargerNote, CorrectiveContract, CorrectiveCoverage,
                     Intervention, Report, Station, User, set_contract_notes, RemovedCoverage,
                     ContractTrash, CoverageTrashLink, DeletedIntervention, MembershipRevision)
from .contract_trash import MEMBERSHIP_RETENTION, advance_membership_revision, check_membership_revision, utc_now
from .stations import lock_catalogue, stations_for_import
from .work_orders import assigned_items, current_ordinary_drafts, lisbon_now


class NewIntervention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["corrective", "commissioning"]
    station_id: UUID
    contract_id: UUID | None = None
    technician: UUID | None = None
    notes: str = Field(default="", max_length=32767)


class DraftHandoff(BaseModel):
    report_id: UUID
    base_revision: int = Field(ge=1)


class Share(BaseModel):
    technician: UUID | None
    previous: UUID | None
    handoff: DraftHandoff | None = None


def coverage_for(station_id, day):
    return (select(CorrectiveContract).join(CorrectiveCoverage)
            .where(CorrectiveCoverage.station_id == station_id,
                   CorrectiveContract.start <= day, CorrectiveContract.end >= day)
            .order_by(CorrectiveContract.start.desc(), CorrectiveContract.name, CorrectiveContract.id))


def spreadsheet_ids(data):
    book = workbook(data, data_only=False)
    try:
        sheet = book.worksheets[0]
        # Producers may omit or understate worksheet dimensions.
        sheet.reset_dimensions()
        rows = {}
        for line, (cell,) in enumerate(sheet.iter_rows(max_col=1), 1):
            if line > 100000:
                raise HTTPException(422, "Máximo de 100000 linhas.")
            if cell.value is None:
                continue
            value = cell_text(cell, identifier=True)
            if not value:
                continue
            if cell.data_type in ("f", "e", "b", "d") or len(value) > 200:
                raise HTTPException(422, f"Linha {line}: indique um ID de PCVE válido na coluna A.")
            if value in rows:
                raise HTTPException(422, f"Linha {line}: ID repetido (linha {rows[value]}).")
            rows[value] = line
        if not rows:
            raise HTTPException(422, "A primeira folha precisa de pelo menos um ID na coluna A.")
        return rows
    finally:
        book.close()


def check_dates(start, end, today):
    if start.year < today.year or end < start:
        raise HTTPException(422, "O início deve pertencer ao ano atual ou posterior e o fim deve ser igual ou posterior ao início.")
    anniversary = date.max
    if start.year <= date.max.year - 2:
        last_day = min(start.day, calendar.monthrange(start.year + 2, start.month)[1])
        anniversary = start.replace(year=start.year + 2, day=last_day)
    if end > anniversary:
        raise HTTPException(422, "A duração máxima do contrato é de dois anos.")


def contract_data(contract):
    return {"id": contract.id, "name": contract.name, "company": contract.company,
            "start": contract.start, "end": contract.end}


def report_context(db, job, code):
    coverage = db.get(CorrectiveContract, job.corrective_contract_id) if job.corrective_contract_id else None
    return {"intervention": {"id": job.id, "kind": job.kind, "reference": code},
            "contract": contract_data(coverage) if coverage else None}


def require_preventive_scope(db, scope):
    if db.get(Intervention, scope):
        raise HTTPException(409, "Utilize o menu da intervenção para gerir esta tarefa.")


def active_report():
    # A resolved visit's recovery draft remains attached to its own work item.
    return (Report.status == "closed") | Report.id.in_(current_ordinary_drafts())


def catalogue_jobs(db, scope_ids, user):
    """Enrich only the authorized scopes, without loading answers or evidence."""
    if not scope_ids:
        return {}
    query = (select(Intervention, Contract.name, Charger, CorrectiveContract, Assignment.user_id, ContractChargerNote)
             .join(Contract, Contract.id == Intervention.id).join(Charger, Charger.id == Intervention.id)
             .outerjoin(CorrectiveContract, CorrectiveContract.id == Intervention.corrective_contract_id)
             .outerjoin(Assignment, Assignment.contract_id == Intervention.id)
             .outerjoin(ContractChargerNote, ContractChargerNote.charger_id == Intervention.id)
             .where(Intervention.id.in_(scope_ids)))
    jobs = {}
    for job, code, charger, coverage, technician, note in db.execute(query):
        jobs[job.id] = {"id": job.id, "kind": job.kind, "reference": code, "created": job.created,
                        "station_id": charger.station_id, "external_id": charger.external_id, "details": charger.details,
                        "station_updated": charger.station.updated if charger.station is not None else None,
                        "contract": contract_data(coverage) if coverage else None, "technician": technician,
                        "notes": note.text if note else "", "notes_revision": note.revision if note else 0}
    reports = (select(Report.charger_id, Report.id, Report.status, Report.owner_id, Report.revision)
               .where(Report.charger_id.in_(jobs), active_report()))
    if user.role != "admin":
        direct = select(Assignment.contract_id).where(Assignment.user_id == user.id)
        reports = reports.where(Report.charger_id.in_(direct) |
                                Report.charger_id.in_(assigned_items(user.id, report_id=Report.id)))
    for target, rid, status, owner, revision in db.execute(reports):
        job = jobs[target]
        job.update(report_id=rid, status=status)
        if user.role == "admin" and status == "draft" and owner == job["technician"]:
            job["handoff"] = {"report_id": rid, "base_revision": revision}
    return jobs


def technician_id(db, value):
    if value is None:
        return None
    uid = str(value)
    if not db.scalar(select(User.id).where(User.id == uid, User.role == "tech").with_for_update(read=True)):
        raise HTTPException(422, "Selecione um técnico ativo.")
    return uid


def register(app, db_dep, user_dep, admin, audit, visible_stations, file_bytes, change_draft_owner):
    @app.get("/api/corrective-contracts")
    def contracts(station_id: UUID | None = None, u=Depends(admin), db=Depends(db_dep)):
        if station_id:
            query = coverage_for(str(station_id), lisbon_now().date().isoformat())
        else:
            query = select(CorrectiveContract).order_by(CorrectiveContract.start.desc(), CorrectiveContract.name)
        return [contract_data(c) for c in db.scalars(query)]

    @app.put("/api/corrective-contracts/{cid}")
    def import_contract(cid: UUID, company: str = Form(max_length=120), name: str = Form(max_length=115),
                        start: date = Form(), end: date = Form(), file: UploadFile = File(),
                        u=Depends(admin), db=Depends(db_dep)):
        company, name = company.strip(), name.strip()
        if not name or not company:
            raise HTTPException(422, "Indique o cliente e o nome do contrato.")
        title = f"{start.year}_{company}_{name}"
        if len(title) > 240:
            raise HTTPException(422, "O nome completo do contrato excede 240 caracteres.")
        rows = spreadsheet_ids(file_bytes(file))
        lock_catalogue(db)
        stations = stations_for_import(db, [{"external_id": value} for value in rows])
        missing = next((value for value in rows if value not in stations), None)
        if missing is not None:
            raise HTTPException(422, f"Linha {rows[missing]}: o ID {missing} não existe na base de PCVEs.")
        key = str(cid)
        ids = {station.id for station in stations.values()}
        previous = db.get(CorrectiveContract, key)
        if previous:
            members = set(db.scalars(select(CorrectiveCoverage.station_id).where(CorrectiveCoverage.contract_id == key)))
            if (previous.name, previous.company, previous.start, previous.end, members) == (
                    title, company, start.isoformat(), end.isoformat(), ids):
                return {**contract_data(previous), "count": len(ids)}
            raise HTTPException(409, "Este pedido já criou outro contrato. Atualize a lista.")
        check_dates(start, end, lisbon_now().date())
        if db.scalar(select(CorrectiveContract.id).where(CorrectiveContract.name == title)):
            raise HTTPException(409, "Já existe um contrato de corretivas com este nome.")
        contract = CorrectiveContract(id=key, company=company, name=title, start=start.isoformat(), end=end.isoformat())
        db.add(contract)
        db.flush()
        db.add_all(CorrectiveCoverage(contract_id=key, station_id=sid) for sid in ids)
        audit(db, u, "corrective-contract.create", key)
        db.commit()
        return {**contract_data(contract), "count": len(ids)}


    @app.get("/api/corrective-contracts/{cid}/members")
    def members(cid: UUID, u=Depends(admin), db=Depends(db_dep)):
        key = str(cid)
        active = (select(Station.id, Station.external_id, MembershipRevision.revision).join(CorrectiveCoverage)
                  .outerjoin(MembershipRevision, (MembershipRevision.kind == "corrective")
                             & (MembershipRevision.contract_id == key)
                             & (MembershipRevision.member_id == Station.id))
                  .where(CorrectiveCoverage.contract_id == key))
        removed = (select(Station.id, Station.external_id, RemovedCoverage.purge_after).join(RemovedCoverage)
                   .where(RemovedCoverage.contract_id == key, RemovedCoverage.purge_after > utc_now().isoformat()))
        rows = [dict(id=sid, external_id=name, membership_revision=revision or 0)
                for sid, name, revision in db.execute(active)]
        rows.extend(dict(id=sid, external_id=name, purge_after=end) for sid, name, end in db.execute(removed))
        return sorted(rows, key=lambda row: row["external_id"])

    @app.delete("/api/corrective-contracts/{cid}/members/{sid}")
    def remove_member(cid: UUID, sid: UUID, membership_revision: int = Query(0, ge=0),
                      u=Depends(admin), db=Depends(db_dep)):
        key, station = str(cid), str(sid)
        lock_catalogue(db)
        check_membership_revision(db, "corrective", key, station, membership_revision)
        existing = db.get(RemovedCoverage, (key, station))
        if existing:
            return {"purge_after": existing.purge_after}
        coverage = db.get(CorrectiveCoverage, (key, station))
        if not coverage:
            raise HTTPException(404, "ID ausente do contrato.")
        instant = utc_now()
        deadline = (instant + MEMBERSHIP_RETENTION).isoformat()
        # A job already deleted independently keeps its own 48-hour deadline.
        jobs = (select(Contract).join(Intervention, Intervention.id == Contract.id)
                .join(Charger, Charger.id == Contract.id)
                .where(Intervention.corrective_contract_id == key, Charger.station_id == station)
                .order_by(Contract.id).with_for_update(of=Contract))
        for scope in db.scalars(jobs):
            if db.get(ContractTrash, scope.id):
                continue
            db.add(ContractTrash(contract_id=scope.id, deleted_by=u.id, deleted_at=instant.isoformat(), purge_after=deadline))
            db.flush()
            db.add(CoverageTrashLink(job_id=scope.id, contract_id=key, station_id=station))
        db.delete(coverage)
        db.add(RemovedCoverage(contract_id=key, station_id=station, purge_after=deadline))
        audit(db, u, "corrective-contract.remove-pcve", key + ":" + station)
        db.commit()
        return {"purge_after": deadline}

    @app.post("/api/corrective-contracts/{cid}/members/{sid}/restore")
    def restore_member(cid: UUID, sid: UUID, u=Depends(admin), db=Depends(db_dep)):
        key, station = str(cid), str(sid)
        lock_catalogue(db)
        if db.get(CorrectiveCoverage, (key, station)):
            return {"restored": True}
        entry = db.get(RemovedCoverage, (key, station))
        expired = HTTPException(410, "O prazo de recuperação deste ID terminou.")
        if entry is None:
            raise expired
        links = select(CoverageTrashLink.job_id).where(
            CoverageTrashLink.contract_id == key, CoverageTrashLink.station_id == station)
        scopes = list(db.scalars(links.order_by(CoverageTrashLink.job_id)))
        locked = list(db.scalars(select(Contract.id).where(Contract.id.in_(scopes))
                                 .order_by(Contract.id).with_for_update()))
        # A wait for a report write or a sweep can take us past the deadline.
        if locked != scopes or entry.purge_after <= utc_now().isoformat():
            raise expired
        if set(db.scalars(links)) != set(scopes):
            raise expired
        # Remove only the holds created by this membership operation.
        db.execute(delete(ContractTrash).where(ContractTrash.contract_id.in_(scopes)))
        db.add(CorrectiveCoverage(contract_id=key, station_id=station))
        db.delete(entry)
        advance_membership_revision(db, "corrective", key, station)
        audit(db, u, "corrective-contract.restore-pcve", key + ":" + station)
        db.commit()
        return {"restored": True}

    @app.put("/api/interventions/{iid}")
    def create(iid: UUID, body: NewIntervention, u=Depends(user_dep), db=Depends(db_dep)):
        if u.role != "admin" and (body.kind != "commissioning" or body.technician or body.notes):
            raise HTTPException(403, "A criação de corretivas e a atribuição a técnicos exigem um administrador.")
        key = str(iid)
        digest = hashlib.sha256(json.dumps(body.model_dump(mode="json", exclude_unset=True),
                                          sort_keys=True).encode("utf-8")).hexdigest()
        lock_catalogue(db)
        if db.get(DeletedIntervention, key) or db.get(ContractTrash, key):
            raise HTTPException(409, "Este pedido foi apagado.")
        previous = db.get(Intervention, key)
        if previous:
            if previous.created_by == u.id and previous.request_digest == digest:
                return {"id": key, "reference": db.get(Contract, key).name}
            raise HTTPException(409, "Identificador de intervenção já utilizado.")
        station = db.scalar(visible_stations(u).where(Station.id == str(body.station_id)))
        if station is None:
            raise HTTPException(404, "PCVE indisponível.")
        if db.get(Contract, key) or db.get(Charger, key):
            raise HTTPException(409, "Identificador já utilizado.")
        today = lisbon_now().date()
        coverage = None
        if body.kind == "commissioning":
            if body.contract_id is not None:
                raise HTTPException(422, "Um comissionamento não admite contrato.")
        else:
            eligible = coverage_for(station.id, today.isoformat())
            if "contract_id" not in body.model_fields_set:
                coverage = db.scalar(eligible.limit(1))
            elif body.contract_id is not None:
                coverage = db.scalar(eligible.where(CorrectiveContract.id == str(body.contract_id)))
                if coverage is None:
                    raise HTTPException(422, "O contrato não é válido hoje para este PCVE.")
        try:
            notes = normalize_contract_notes(body.notes)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        tech = technician_id(db, body.technician) if u.role == "admin" else u.id
        # A purge transfers a reference to the tombstone table atomically. Read
        # both counts in the same database snapshot while holding the creation lock.
        sequence = db.scalar(select(select(func.count()).select_from(Intervention).scalar_subquery()
                                    + select(func.count()).select_from(DeletedIntervention).scalar_subquery() + 1))
        code = f"{'COR' if body.kind == 'corrective' else 'COM'}-{sequence:06d}"
        scope = Contract(id=key, company=coverage.company if coverage else "", year=today.year,
                         semester=1 if today.month <= 6 else 2, kind=body.kind, name=code)
        db.add(scope)
        db.flush()
        charger = Charger(id=key, contract_id=key, station=station, external_id=station.external_id)
        set_contract_notes(charger, notes)
        db.add(charger)
        db.add(Intervention(id=key, kind=body.kind, corrective_contract_id=coverage.id if coverage else None,
                            created_by=u.id, request_digest=digest))
        if tech:
            db.add(Assignment(contract_id=key, user_id=tech))
        audit(db, u, "intervention.create", key)
        db.commit()
        return {"id": key, "reference": code}

    @app.put("/api/interventions/{iid}/share")
    def share(iid: UUID, body: Share, u=Depends(admin), db=Depends(db_dep)):
        key = str(iid)
        tech = technician_id(db, body.technician)
        lock_catalogue(db)
        if not db.get(Intervention, key):
            raise HTTPException(404, "Intervenção inexistente.")
        # Report writes also take the scope lock before touching the draft.
        db.scalar(select(Contract.id).where(Contract.id == key).with_for_update())
        if db.get(ContractTrash, key):
            raise HTTPException(423, "Pedido em recuperação.")
        current = db.scalar(select(Assignment.user_id).where(Assignment.contract_id == key))
        handoff = body.handoff
        stale_draft = "O rascunho mudou. Atualize a intervenção antes de o transferir."
        if current == tech:
            if handoff:
                report = db.get(Report, str(handoff.report_id))
                if (not tech or not report or report.charger_id != key or report.owner_id != tech
                        or report.revision <= handoff.base_revision):
                    raise HTTPException(409, stale_draft)
            return {"id": key, "technician": tech}
        if current != (str(body.previous) if body.previous else None):
            raise HTTPException(409, "A atribuição mudou. Atualize a intervenção.")
        draft = db.scalar(select(Report).where(Report.charger_id == key, Report.owner_id == current,
                          Report.id.in_(current_ordinary_drafts())).with_for_update()) if current else None
        if handoff:
            if not tech or not draft or draft.id != str(handoff.report_id) or draft.revision != handoff.base_revision:
                raise HTTPException(409, stale_draft)
            change_draft_owner(db, draft, tech, u)
        elif draft:
            raise HTTPException(409, "O técnico tem um rascunho. Transfira primeiro a propriedade do relatório.")
        db.execute(delete(Assignment).where(Assignment.contract_id == key))
        if tech:
            db.add(Assignment(contract_id=key, user_id=tech))
        audit(db, u, "intervention.share", key)
        db.commit()
        return {"id": key, "technician": tech}
