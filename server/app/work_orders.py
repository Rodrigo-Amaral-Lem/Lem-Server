# SPDX-License-Identifier: GPL-3.0-only
"""Planning, membership-scoped grants and durable mobile events for field work."""
from datetime import date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, literal, select
from sqlalchemy.orm import aliased, selectinload

from .import_profiles import normalize_contract_notes
from .models import (Charger, Contract, ContractTrash, CorrectiveContract, DeletedWorkOrder, ExpiredVisitDraft, Intervention, NoticeReceipt, Photo, Report, ReportCorrection, User, WorkAssignee,
                     WorkItem, WorkItemDraft, WorkNotice, WorkOrder, WorkOrderCancellation, RemovedCharger, attached_charger, now)
from .stations import lock_catalogue
from .work_order_retention import cancellation_cutoff


INTERVENTION_KINDS = ("corrective", "commissioning")
WORK_ITEM_KINDS = ("preventive", *INTERVENTION_KINDS)
RESULT_STATES = ("closed", "invalid")


def lisbon_now():
    return datetime.now(ZoneInfo("Europe/Lisbon"))


def has_started(order, today):
    # A preventive report may independently satisfy an item before the crew starts.
    return order.start <= today or any(
        item.state in RESULT_STATES and not item.resolved_before_start for item in order.items
    )


def assigned_items(user_id, *, writable=False, report_id=None, prospective_owner=False):
    query = (select(WorkItem.target).join(WorkAssignee, WorkAssignee.order_id == WorkItem.order_id)
             .join(WorkOrder, WorkOrder.id == WorkItem.order_id)
             .where(WorkAssignee.user_id == user_id, WorkItem.kind.in_(WORK_ITEM_KINDS), WorkItem.state != "cancelled"))
    if report_id is not None:
        bindings = (select(WorkItemDraft.item_id).where(WorkItemDraft.report_id == report_id)
                    .correlate_except(WorkItemDraft))
        current = (WorkItem.state == "pending") & (~bindings.exists() | WorkItem.id.in_(bindings))
        expired = select(ExpiredVisitDraft.report_id).where(ExpiredVisitDraft.report_id == report_id).correlate_except(ExpiredVisitDraft)
        current = current & ~expired.exists()
    else:
        current = WorkItem.state == "pending"
    if writable:
        completed_item = aliased(WorkItem)
        has_result = select(completed_item.id).where(completed_item.order_id == WorkOrder.id,
                     completed_item.state.in_(RESULT_STATES), completed_item.resolved_before_start.is_(False)).exists()
        return query.where(current, (WorkOrder.start <= lisbon_now().date().isoformat()) | has_result)
    if report_id is not None:
        owned = aliased(Report)
        recovery = (select(WorkItemDraft.item_id).join(owned, owned.id == WorkItemDraft.report_id)
                    .where(WorkItemDraft.report_id == report_id, owned.status == "draft")
                    .correlate_except(WorkItemDraft, owned))
        if not prospective_owner:
            recovery = recovery.where(owned.owner_id == user_id)
        return query.where(current | (WorkItem.report_id == report_id)
                           | WorkItem.id.in_(recovery))
    return query


def eligible_owner(user_id, target, report_id, *, recovery):
    return literal(target).in_(assigned_items(user_id, writable=not recovery, report_id=report_id,
                                               prospective_owner=recovery).correlate(User))


def notice(db, order, recipients, kind, message, day=None):
    for user_id in recipients:
        db.add(WorkNotice(id=str(uuid4()), order_id=order.id, user_id=user_id, kind=kind,
                          message=f"{order.code} · {message}", day=day))


def finish_if_ready(db, order, timestamp):
    if order.items and all(item.state != "pending" for item in order.items) and not order.completed:
        order.completed = timestamp
        local = datetime.fromisoformat(timestamp).astimezone(ZoneInfo("Europe/Lisbon"))
        notice(db, order, order.followers, "completed", f"Concluída em {local:%d/%m/%Y às %H:%M}.")


def ordinary_drafts():
    return select(Report.id).where(Report.status == "draft", ~Report.id.in_(select(ReportCorrection.id)),
                                   ~Report.id.in_(select(ExpiredVisitDraft.report_id)))


def resolved_visit_reports():
    return (select(WorkItemDraft.report_id).join(WorkItem, WorkItem.id == WorkItemDraft.item_id)
            .where(WorkItem.state != "pending"))


def current_ordinary_drafts():
    return ordinary_drafts().where(~Report.id.in_(resolved_visit_reports()))


def recovery_draft_ids():
    return resolved_visit_reports().union_all(select(ExpiredVisitDraft.report_id))


def current_drafts(item):
    # Unbound drafts predate this visit or the association protocol. A recovery
    # binding must never claim the next visit's draft slot.
    return current_ordinary_drafts().where(Report.charger_id == item.target,
        (~Report.id.in_(select(WorkItemDraft.report_id))) |
        Report.id.in_(select(WorkItemDraft.report_id).where(WorkItemDraft.item_id == item.id)))


def pending_item(db, target):
    return db.scalar(select(WorkItem).where(WorkItem.kind.in_(WORK_ITEM_KINDS),
                     WorkItem.target == target, WorkItem.state == "pending"))


def bound_item(db, rid):
    # Older releases could associate a draft with several resolved visits.
    return db.scalar(select(WorkItem).join(WorkItemDraft, WorkItemDraft.item_id == WorkItem.id)
                     .where(WorkItemDraft.report_id == rid)
                     .order_by(WorkItem.resolved_at.asc().nulls_last(), WorkItem.id).limit(1))


def require_editable_visit(db, report_id):
    item = bound_item(db, report_id)
    if (item and item.state != "pending") or db.get(ExpiredVisitDraft, report_id):
        raise HTTPException(403, "A visita de origem terminou. O rascunho está disponível para consulta.")


def preserve_drafts(db, item):
    for rid in db.scalars(ordinary_drafts().where(Report.charger_id == item.target,
                           ~Report.id.in_(select(WorkItemDraft.report_id)))):
        db.add(WorkItemDraft(item_id=item.id, report_id=rid))


def accept_closure(db, charger, report_id, actor, timestamp):
    # Called inside the validated report transaction under the catalogue lock.
    # A separately authorised full-contract closure can also satisfy planned work.
    item = bound_item(db, report_id) or pending_item(db, charger.id)
    if item and item.state != "pending":
        raise HTTPException(409, "A visita de origem já terminou. O report foi conservado.")
    if item:
        order = db.get(WorkOrder, item.order_id)
        item.resolved_before_start = not has_started(order, lisbon_now().date().isoformat())
        preserve_drafts(db, item)
        item.state, item.report_id = "closed", report_id
        item.resolved_by, item.resolved_at = actor.id, timestamp
        order.revision += 1
        finish_if_ready(db, order, timestamp)


class Plan(BaseModel):
    id: UUID
    revision: int = Field(ge=0)
    start: date
    due: date
    technicians: list[UUID] = Field(min_length=1, max_length=200)
    targets: list[UUID] = Field(min_length=1, max_length=1000)


class OrderRevision(BaseModel):
    revision: int = Field(ge=1)


class InvalidItem(OrderRevision):
    note: str = Field(max_length=4000)


class Follow(BaseModel):
    following: bool


def available(db):
    # A recovery draft belongs to a resolved visit. The same intervention
    # can be planned again and claim its own draft under the current visit.
    closed = select(Report.id).where(Report.charger_id == Charger.id, Report.status == "closed").exists()
    planned = select(WorkItem.id).where(WorkItem.target == Charger.id,
                                        WorkItem.state == "pending").exists()
    return (select(Charger.id, Charger.contract_id, Charger.station_id, Charger.external_id,
                   case((Intervention.id.is_(None), Contract.name),
                        (Intervention.kind == "corrective",
                         func.coalesce(CorrectiveContract.name, "Sem contrato"))).label("name"),
                   func.coalesce(Intervention.kind, "preventive").label("kind"), Contract.name.label("reference"),
                   Contract.company).join(Contract, Contract.id == Charger.contract_id)
            .outerjoin(Intervention, Intervention.id == Charger.id)
            .outerjoin(CorrectiveContract, CorrectiveContract.id == Intervention.corrective_contract_id)
            .where(Intervention.id.is_(None) | Intervention.kind.in_(INTERVENTION_KINDS))
            .where(attached_charger(), ~Contract.id.in_(select(ContractTrash.contract_id)), ~closed, ~planned)
            .order_by(Contract.name, Charger.external_id))


def register(app, db_dep, user_dep, admin, audit):
    def order_for(db, oid, user):
        order = db.get(WorkOrder, str(oid))
        cancelled = db.get(WorkOrderCancellation, str(oid)) if order else None
        if (not order or (cancelled and cancelled.cancelled_at <= cancellation_cutoff())
                or (user.role != "admin" and not db.get(WorkAssignee, (order.id, user.id)))):
            raise HTTPException(404, "Ordem de trabalho indisponível.")
        return order

    @app.get("/api/work-orders")
    def listing(include_cancelled: bool = False, u=Depends(user_dep), db=Depends(db_dep)):
        query = (select(WorkOrder, WorkOrderCancellation)
                 .outerjoin(WorkOrderCancellation, WorkOrderCancellation.order_id == WorkOrder.id)
                 .options(selectinload(WorkOrder.items)).order_by(WorkOrder.start.desc(), WorkOrder.code))
        active = WorkOrderCancellation.order_id.is_(None)
        query = query.where(active | (WorkOrderCancellation.cancelled_at > cancellation_cutoff()) if include_cancelled else active)
        if u.role != "admin":
            query = query.join(WorkAssignee).where(WorkAssignee.user_id == u.id)
        rows = db.execute(query).all()
        orders = [order for order, _ in rows]
        ids = [o.id for o in orders]
        cancellations = {c.order_id: c for _, c in rows if c is not None}
        assignees = {}
        for oid, uid, name, role in db.execute(select(WorkAssignee.order_id, User.id, User.name, User.role)
                .join(User, User.id == WorkAssignee.user_id).where(WorkAssignee.order_id.in_(ids))):
            assignees.setdefault(oid, []).append({"id": uid, "name": name, "role": role})
        targets = {i.target for o in orders for i in o.items}
        references = dict(db.execute(select(Contract.id, Contract.name).where(Contract.id.in_(
            {i.contract_id for o in orders for i in o.items if i.kind in INTERVENTION_KINDS}))).all())
        chargers = {c.id: c for c in db.scalars(select(Charger).options(
                    selectinload(Charger.station), selectinload(Charger.contract_note)).where(Charger.id.in_(targets),
                    attached_charger(), ~Charger.contract_id.in_(select(ContractTrash.contract_id)))).unique()}
        trashed = set(db.scalars(select(ContractTrash.contract_id).where(ContractTrash.contract_id.in_(
            {i.contract_id for o in orders for i in o.items}))))
        held = set(db.scalars(select(RemovedCharger.charger_id).where(RemovedCharger.charger_id.in_(targets))))
        # A detached membership still owns its existing reports. Attachment to
        # the contract is required for a new draft, separately from draft lookup.
        pending = {i.target for o in orders for i in o.items
                   if i.state == "pending" and i.contract_id not in trashed and i.target not in held}
        closed_ids = {i.report_id for o in orders for i in o.items if i.report_id}
        existing = set(db.scalars(select(Report.id).where(Report.id.in_(closed_ids))))
        item_ids = [i.id for o in orders for i in o.items if i.state in ("closed", "invalid")]
        recovery = {}
        for item_id, rid in db.execute(select(WorkItemDraft.item_id, WorkItemDraft.report_id)
                .where(WorkItemDraft.item_id.in_(item_ids))):
            recovery.setdefault(rid, []).append(item_id)
        historical = resolved_visit_reports()
        reports, recovered = {}, {}
        count = select(func.count(Photo.id)).where(Photo.report_id == Report.id).scalar_subquery()
        for rid, target, owner, name, revision, photo_count, archived in db.execute(
                select(Report.id, Report.charger_id, Report.owner_id, User.name, Report.revision, count, Report.id.in_(historical))
                .join(User, User.id == Report.owner_id)
                .where(Report.id.in_(ordinary_drafts()), (Report.charger_id.in_(pending) | Report.id.in_(recovery)))
                .order_by(Report.updated.desc(), Report.id)):
            summary = dict(id=rid, owner_id=owner, owner_name=name, revision=revision, photo_count=photo_count)
            if target in pending and not archived:
                reports.setdefault(target, []).append(summary)
            if target not in held and (u.role == "admin" or owner == u.id):
                for iid in recovery.get(rid, []):
                    recovered.setdefault(iid, []).append(summary)
        today = lisbon_now().date().isoformat()
        result = []
        for o in orders:
            rows = []
            for i in o.items:
                c = chargers.get(i.target) if i.state != "cancelled" else None
                rows.append(dict(id=i.id, target=i.target, kind=i.kind, contract_id=i.contract_id,
                        contract=i.contract_name, reference=references.get(i.contract_id), client=i.client,
                        external_id=i.external_id, details=c.details if c else {},
                        station_updated=c.station.updated if c and c.station else None,
                        contract_notes=c.contract_note.text if c and c.contract_note else "",
                        notes_revision=c.contract_note.revision if c and c.contract_note else 0,
                        available=(i.report_id in existing and i.contract_id not in trashed and i.target not in held) if i.state == "closed" else c is not None,
                        report_removed=i.state == "closed" and i.report_id not in existing, station_id=i.station_id,
                        state=i.state, note=i.note, report_id=i.report_id, resolved_at=i.resolved_at,
                        resolved_by=i.resolved_by, drafts=reports.get(i.target, []) if i.state == "pending" else ([] if i.contract_id in trashed else recovered.get(i.id, []))))
            cancelled = cancellations.get(o.id)
            result.append(dict(cancelled_at=cancelled.cancelled_at if cancelled else None,
                               cancelled_by=cancelled.actor_id if cancelled else None, id=o.id, code=o.code, start=o.start, due=o.due, completed=o.completed,
                               revision=o.revision, following=u.id in o.followers, technicians=assignees.get(o.id, []),
                               items=rows, started=has_started(o, today)))
        return {"today": today, "orders": result}

    @app.get("/api/work-order-candidates")
    def candidates(u=Depends(admin), db=Depends(db_dep)):
        return [dict(target=row.id, contract_id=row.contract_id, contract=row.name, external_id=row.external_id,
                     kind=row.kind, reference=row.reference if row.kind in INTERVENTION_KINDS else None)
                for row in db.execute(available(db))]

    @app.put("/api/work-orders/{oid}")
    def save(oid: UUID, body: Plan, u=Depends(admin), db=Depends(db_dep)):
        if oid != body.id:
            raise HTTPException(422, "Identificador inválido.")
        lock_catalogue(db)
        order = db.get(WorkOrder, str(oid))
        cancellation = db.get(WorkOrderCancellation, order.id) if order else None
        if (cancellation and cancellation.cancelled_at <= cancellation_cutoff()) or (not order and db.get(DeletedWorkOrder, str(oid))):
            raise HTTPException(410, "Esta ordem de trabalho foi eliminada.")
        if cancellation:
            raise HTTPException(409, "Esta ordem foi cancelada.")
        start, due = body.start.isoformat(), body.due.isoformat()
        techs, targets = [str(i) for i in body.technicians], [str(i) for i in body.targets]
        if len(set(techs)) != len(techs) or len(set(targets)) != len(targets):
            raise HTTPException(422, "Existem técnicos ou IDs repetidos.")
        previous = set(db.scalars(select(WorkAssignee.user_id).where(WorkAssignee.order_id == str(oid))))
        if order and order.start == start and order.due == due and set(techs) == previous and targets == [i.target for i in order.items]:
            return {"id": order.id, "revision": order.revision}
        if (order.revision if order else 0) != body.revision or (order and order.completed):
            raise HTTPException(409, "A ordem mudou. Atualize antes de guardar.")
        today = lisbon_now().date().isoformat()
        if due < start or (start < today and (not order or start != order.start)):
            raise HTTPException(422, "Verifique as datas. Um novo início deve ser hoje ou posterior.")
        if order and start != order.start and has_started(order, today):
            raise HTTPException(409, "A ordem já começou. Mantenha a data de início e ajuste o prazo.")
        if set(techs) != set(db.scalars(select(User.id).where(User.role == "tech", User.id.in_(techs)))):
            raise HTTPException(422, "Selecione técnicos ativos.")
        old = {i.target: i for i in order.items} if order else {}
        if any(i.state != "pending" and i.target not in targets for i in old.values()):
            raise HTTPException(409, "Os resultados já registados devem permanecer na ordem.")
        for target in old.keys() - set(targets):
            if db.scalar(current_drafts(old[target]).limit(1)):
                raise HTTPException(409, "O ID tem um rascunho. Conserve a tarefa e o acesso ao report.")
        additions = set(targets) - old.keys()
        eligible = {row.id: row for row in db.execute(available(db).where(Charger.id.in_(additions)))}
        if eligible.keys() != additions:
            raise HTTPException(409, "Um ID já está planeado, concluído ou indisponível.")
        changed_start = not order or order.start != start
        if not order:
            prefix = f"OT-{body.start:%Y%m}-"
            # Include retired numbers in the original month, even after the last order is deleted.
            sequence = 1 + sum(db.scalar(select(func.count()).select_from(table).where(table.code.startswith(prefix)))
                               for table in (WorkOrder, DeletedWorkOrder))
            order = WorkOrder(id=str(oid), code=f"{prefix}{sequence:04d}", revision=0, followers=[u.id])
            db.add(order)
        order.start, order.due = start, due
        order.revision += 1
        rows = []
        for pos, target in enumerate(targets):
            item = old.get(target)
            if not item:
                c = eligible[target]
                item = WorkItem(id=str(uuid4()), kind=c.kind, target=target, contract_id=c.contract_id,
                                external_id=c.external_id, contract_name=c.name, client=c.company,
                                station_id=c.station_id, state="pending")
            item.position = pos
            rows.append(item)
        order.items = rows
        db.flush()
        removed = previous - set(techs)
        db.execute(delete(WorkAssignee).where(WorkAssignee.order_id == order.id, WorkAssignee.user_id.in_(removed)))
        db.execute(delete(WorkNotice).where(WorkNotice.order_id == order.id, WorkNotice.user_id.in_(removed),
                                           WorkNotice.kind.in_(["assigned", "start"])))
        new = set(techs) - previous
        db.add_all(WorkAssignee(order_id=order.id, user_id=uid) for uid in new)
        notice(db, order, new, "assigned", "Recebeu uma nova ordem de trabalho.")
        if changed_start:
            db.execute(delete(WorkNotice).where(WorkNotice.order_id == order.id, WorkNotice.kind == "start"))
        notice(db, order, techs if changed_start else new, "start", "A ordem de trabalho começa hoje.", start)
        finish_if_ready(db, order, now())
        audit(db, u, "work-order.save", order.id)
        db.commit()
        return {"id": order.id, "revision": order.revision}

    @app.put("/api/work-orders/{oid}/cancel")
    def cancel(oid: UUID, body: OrderRevision, u=Depends(admin), db=Depends(db_dep)):
        lock_catalogue(db)
        order = order_for(db, oid, u)
        previous = db.get(WorkOrderCancellation, order.id)
        if previous:
            return {"cancelled_at": previous.cancelled_at, "revision": order.revision}
        if order.revision != body.revision or order.completed or has_started(order, lisbon_now().date().isoformat()):
            raise HTTPException(409, "Só pode cancelar uma ordem futura. Atualize a ordem.")
        timestamp = now()
        for item in order.items:
            # An early independent result keeps its report ID and history, but
            # no visit in a cancelled order should grant crew access to it.
            if item.state == "pending" or item.resolved_before_start:
                item.state = "cancelled"
        db.add(WorkOrderCancellation(order_id=order.id, cancelled_at=timestamp, actor_id=u.id))
        db.execute(delete(WorkNotice).where(WorkNotice.order_id == order.id, WorkNotice.kind.in_(["assigned", "start"])))
        order.revision += 1
        audit(db, u, "work-order.cancel", order.id)
        db.commit()
        return {"cancelled_at": timestamp, "revision": order.revision}

    @app.put("/api/work-orders/{oid}/follow")
    def follow(oid: UUID, body: Follow, u=Depends(admin), db=Depends(db_dep)):
        lock_catalogue(db)
        order = order_for(db, oid, u)
        if db.get(WorkOrderCancellation, order.id):
            raise HTTPException(409, "Esta ordem foi cancelada.")
        if order.completed and body.following:
            raise HTTPException(409, "Esta ordem já foi concluída.")
        followers = set(order.followers)
        if body.following:
            followers.add(u.id)
        else:
            followers.discard(u.id)
            db.execute(delete(WorkNotice).where(WorkNotice.order_id == order.id, WorkNotice.user_id == u.id,
                                                WorkNotice.kind.in_(["invalid", "completed"])))
        order.followers = sorted(followers)
        db.commit()
        return {"following": body.following}

    @app.put("/api/work-orders/{oid}/items/{iid}/invalid")
    def invalidate(oid: UUID, iid: UUID, body: InvalidItem, u=Depends(user_dep), db=Depends(db_dep)):
        lock_catalogue(db)
        order = order_for(db, oid, u)
        if not db.get(WorkAssignee, (order.id, u.id)):
            raise HTTPException(403, "Apenas um técnico atribuído pode assinalar o impedimento.")
        item = next((i for i in order.items if i.id == str(iid)), None)
        if not item:
            raise HTTPException(404, "Tarefa inexistente.")
        try:
            note = normalize_contract_notes(body.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not note:
            raise HTTPException(422, "Indique o motivo na observação.")
        if item.state == "invalid" and item.resolved_by == u.id and item.note == note:
            return {"ok": True}
        today = lisbon_now().date().isoformat()
        if order.revision != body.revision or item.state != "pending" or not has_started(order, today):
            raise HTTPException(409, "Atualize a ordem. Esta tarefa ainda não começou ou já mudou.")
        # Check the marker after taking the lock used by trash and restore.
        db.scalar(select(Contract.id).where(Contract.id == item.contract_id).with_for_update(read=True))
        if db.get(ContractTrash, item.contract_id):
            raise HTTPException(423, "Contrato apagado; aguarda recuperação pelo administrador. Dados locais conservados.")
        if db.scalar(select(Charger.id).where(Charger.id == item.target, attached_charger())) is None:
            raise HTTPException(409, "O ID está indisponível. Atualize a ordem.")
        preserve_drafts(db, item)
        item.state, item.note, item.resolved_by, item.resolved_at = "invalid", note, u.id, now()
        order.revision += 1
        notice(db, order, order.followers, "invalid", f"{item.external_id}: tarefa marcada como inválida.")
        finish_if_ready(db, order, item.resolved_at)
        audit(db, u, "work-order.invalid", item.id)
        db.commit()
        return {"ok": True}

    @app.get("/api/work-order-notices/{device}")
    def notices(device: UUID, u=Depends(user_dep), db=Depends(db_dep)):
        local = lisbon_now()
        allowed = WorkNotice.day.is_(None)
        if 7 <= local.hour < 20:
            allowed = allowed | ((WorkNotice.day == local.date().isoformat()) & WorkOrder.completed.is_(None))
        seen = select(NoticeReceipt.notice_id).where(NoticeReceipt.device == str(device))
        kinds = ("completed", "invalid") if u.role == "admin" else ("assigned", "start")
        query = (select(WorkNotice).join(WorkOrder).where(WorkNotice.user_id == u.id, WorkNotice.kind.in_(kinds), allowed,
                  ~WorkNotice.id.in_(seen)).order_by(WorkNotice.created, WorkNotice.id).limit(100))
        deadline = local.replace(hour=20, minute=0, second=0, microsecond=0)
        valid_ms = max(0, int((deadline - local).total_seconds() * 1000))
        return [dict(id=n.id, kind=n.kind, valid_ms=valid_ms if n.day else None,
                     message=n.message) for n in db.scalars(query)]

    @app.put("/api/work-order-notices/{device}/{nid}")
    def received(device: UUID, nid: UUID, u=Depends(user_dep), db=Depends(db_dep)):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        event = db.get(WorkNotice, str(nid))
        if not event or event.user_id != u.id:
            raise HTTPException(404, "Aviso inexistente.")
        insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
        db.execute(insert(NoticeReceipt).values(notice_id=str(nid), device=str(device)).on_conflict_do_nothing())
        db.commit()
        return {"ok": True}
