# SPDX-License-Identifier: GPL-3.0-only
"""Remove expired cancelled orders without deleting the reports or their evidence."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select

from .models import (DeletedWorkOrder, ExpiredVisitDraft, Report, WorkAssignee, WorkItem,
                     WorkItemDraft, WorkNotice, WorkOrder, WorkOrderCancellation)
from .stations import lock_catalogue


def cancellation_cutoff(instant=None):
    instant = instant or datetime.now(timezone.utc)
    return (instant.astimezone(timezone.utc) - timedelta(hours=24)).isoformat(timespec="microseconds")


def purge_cancelled_orders(factory, instant=None):
    cutoff = cancellation_cutoff(instant)
    due = select(WorkOrderCancellation.order_id).where(WorkOrderCancellation.cancelled_at <= cutoff)
    with factory() as db:
        ids = list(db.scalars(due))
    removed = 0
    for oid in ids:
        with factory.begin() as db:
            # Planning and other sweep workers share this lock. Recheck after obtaining it.
            lock_catalogue(db)
            cancellation = db.get(WorkOrderCancellation, oid)
            if cancellation is None or cancellation.cancelled_at > cutoff:
                continue
            order = db.get(WorkOrder, oid)
            db.add(DeletedWorkOrder(id=oid, code=order.code))
            items = select(WorkItem.id).where(WorkItem.order_id == oid)
            # A historically bound draft must never become an unbound candidate for a later visit.
            bindings = (select(WorkItemDraft.report_id, func.min(WorkItemDraft.item_id))
                        .join(Report, Report.id == WorkItemDraft.report_id)
                        .where(WorkItemDraft.item_id.in_(items), Report.status == "draft",
                               ~WorkItemDraft.report_id.in_(select(ExpiredVisitDraft.report_id)))
                        .group_by(WorkItemDraft.report_id))
            db.execute(insert(ExpiredVisitDraft).from_select(["report_id", "item_id"], bindings))
            # Foreign keys cascade from notices to receipts and from items to visit bindings.
            for table in (WorkNotice, WorkAssignee, WorkItem, WorkOrderCancellation):
                db.execute(delete(table).where(table.order_id == oid))
            db.execute(delete(WorkOrder).where(WorkOrder.id == oid))
        removed += 1
    return removed
