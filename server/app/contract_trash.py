# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Contract retention and physical deletion. Never load photo BLOBs to purge.

Lock order: contract -> charger/report -> photograph. API access takes a shared
contract lock; trash/restore/purge take an exclusive lock. PostgreSQL therefore
finishes an accepted operation before hiding/deleting its contract.
"""

from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete
from .models import (
    Contract,
    ContractBranding,
    ContractReportStyle,
    ContractTrash,
    Assignment,
    Charger,
    Report,
    ReportCorrection,
    DeletedReport,
    OwnershipTransfer,
    Photo,
    ImportBatch,
    Audit,
)
import uuid

RETENTION = timedelta(days=30)


def purge_contract(db, contract, entry):
    """Caller holds the contract lock; keep the entire purge in its transaction."""
    cid = contract.id
    chargers = select(Charger.id).where(Charger.contract_id == cid)
    reports = select(Report.id).where(Report.charger_id.in_(chargers))
    removed_reports = select(DeletedReport.id).where(DeletedReport.charger_id.in_(chargers))
    operations = [
        delete(ReportCorrection).where(ReportCorrection.contract_id == cid),
        delete(Photo).where(Photo.report_id.in_(reports)),
        delete(OwnershipTransfer).where(
            OwnershipTransfer.report_id.in_(reports)
            | OwnershipTransfer.report_id.in_(removed_reports)
        ),
        delete(Report).where(Report.charger_id.in_(chargers)),
        delete(DeletedReport).where(DeletedReport.charger_id.in_(chargers)),
        delete(ImportBatch).where(ImportBatch.contract_id == cid),
        delete(ContractBranding).where(ContractBranding.contract_id == cid),
        delete(ContractReportStyle).where(ContractReportStyle.contract_id == cid),
        delete(Assignment).where(Assignment.contract_id == cid),
        delete(Charger).where(Charger.contract_id == cid),
    ]
    for operation in operations:
        db.execute(operation.execution_options(synchronize_session=False))
    db.add(
        Audit(id=str(uuid.uuid4()), user_id=entry.deleted_by, action="contract.purge", target=cid)
    )
    db.delete(entry)
    db.flush()
    db.delete(contract)


def utc_now():
    return datetime.now(timezone.utc)


def purge_expired(factory, instant=None, only_contract=None):
    instant = instant or utc_now()
    # Keep each contract in its own transaction. SKIP LOCKED makes multiple API
    # workers safe, and a busy contract can be retried on the next minute's sweep.
    with factory() as db:
        query = select(ContractTrash.contract_id).where(
            ContractTrash.purge_after <= instant.isoformat()
        )
        if only_contract is not None:
            query = query.where(ContractTrash.contract_id == only_contract)
        ids = list(db.scalars(query))
    count = 0
    for cid in ids:
        with factory() as db:
            contract = db.scalar(
                select(Contract)
                .where(Contract.id == cid)
                .with_for_update(skip_locked=only_contract is None)
            )
            if contract is None:
                continue
            entry = db.get(ContractTrash, cid)
            if entry is None or entry.purge_after > instant.isoformat():
                continue  # Another admin restored it while this sweep was queued.
            purge_contract(db, contract, entry)
            db.commit()
            count += 1
    return count
