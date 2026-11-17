# SPDX-License-Identifier: GPL-3.0-only
"""Admin-owned contract presentation, revisioned independently from report content."""
import json
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from .models import Contract, ContractBranding, ContractReportStyle

CATALOGUE = json.loads((Path(__file__).resolve().parents[2] / "shared/branding/report-logos.json").read_text(encoding="utf-8"))
BY_ID = {row["id"]: row for row in CATALOGUE}
ISSUERS = ("Lem", "CSMTEC")

def selection(ids):
    if (not isinstance(ids, list) or not 0 <= len(ids) <= 2
            or any(not isinstance(i, str) or i not in BY_ID for i in ids)
            or len(set(ids)) != len(ids)):
        raise HTTPException(422, "Escolha até dois logótipos adicionais diferentes")
    return [dict(BY_ID[i]) for i in ids]

def contract_logos(db, cid):
    row = db.get(ContractBranding, cid)
    return row.logos if row else []


def contract_presentation(db, cid):
    # One SQL snapshot prevents mixing an old issuer with newly committed logos
    # on SQLite, where PostgreSQL's shared contract row lock is unavailable.
    row = db.execute(select(ContractReportStyle.issuer, ContractReportStyle.revision, ContractBranding.logos)
        .select_from(Contract).outerjoin(ContractReportStyle, ContractReportStyle.contract_id == Contract.id)
        .outerjoin(ContractBranding, ContractBranding.contract_id == Contract.id)
        .where(Contract.id == cid)).first()
    issuer, revision, logos = row if row else (None, None, None)
    return {"issuer": issuer or "Lem", "revision": revision or 0,
            "client_logos": [logo for logo in (logos or []) if logo["id"] not in ISSUERS]}


def save_presentation(db, cid, issuer, logos, base_revision=None):
    """Caller holds the contract write lock; admin endpoints own authorization."""
    if issuer not in ISSUERS:
        raise HTTPException(422, "Escolha Lem ou CSMTEC como entidade do report")
    current = contract_presentation(db, cid)
    if base_revision is not None and base_revision != current["revision"]:
        raise HTTPException(409, "O formato do contrato foi alterado por outro administrador. Atualize antes de guardar.")
    selected = [entry for entry in selection(logos) if entry["id"] not in ISSUERS]
    if issuer == current["issuer"] and selected == current["client_logos"]:
        return current
    # PostgreSQL honors the caller's contract lock; SQLite ignores FOR UPDATE.
    # Claim the exact presentation revision atomically on both engines, including
    # the first write when no style row exists. Never let a losing writer touch logos.
    if db.get(ContractReportStyle, cid) is None:
        insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        claim = insert(ContractReportStyle).values(contract_id=cid, issuer=issuer, revision=1).on_conflict_do_nothing(index_elements=[ContractReportStyle.contract_id])
    else:
        claim = update(ContractReportStyle).where(
            ContractReportStyle.contract_id == cid,
            ContractReportStyle.revision == current["revision"],
        ).values(issuer=issuer, revision=current["revision"] + 1).execution_options(synchronize_session=False)
    # psycopg clears INSERT rowcount when SQLAlchemy closes its cursor: a
    # successful first write can report -1. RETURNING tests the actual claim,
    # including a genuine conflict, on both PostgreSQL and SQLite.
    claimed = db.execute(claim.returning(ContractReportStyle.contract_id)).scalar_one_or_none()
    if claimed != cid:
        raise HTTPException(409, "O formato do contrato foi alterado por outro administrador. Atualize antes de guardar.")
    db.expire_all()
    row = db.get(ContractBranding, cid)
    if row is None:
        db.add(ContractBranding(contract_id=cid, logos=selected))
    else:
        row.logos = selected
    db.flush()
    return contract_presentation(db, cid)
