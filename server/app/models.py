# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Persistent schema shared by SQLite development/tests and PostgreSQL deployment.
# create_all creates missing tables; it is not an automatic column-migration system.
# Keep storage identifiers stable across app branding changes. See docs/architecture-and-api.md in the Server repository.

import os, json, uuid
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine,
    String,
    Integer,
    Text,
    JSON,
    LargeBinary,
    ForeignKey,
    UniqueConstraint,
    Index,
    text,
    inspect,
    select,
)
from .import_profiles import clean_station_details, is_contract_notes, normalize_contract_notes
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class AccountAdminLock(Base):
    """Singleton serializes console account changes, revocation and last-admin checks.

    Login shares this lock when issuing a token. Authenticated requests lock
    only their User rows, allowing unrelated normal work.
    """

    __tablename__ = "account_admin_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


# Login roles are admin/tech. Console-only system and anonymized deleted UUIDs
# retain audit/ownership foreign keys but are denied login and active listings.
# password contains a salted scrypt representation, never recoverable plaintext.
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(10), default="tech")


# Store the SHA-256 token digest and UTC expiry; never the raw bearer token.
class SessionToken(Base):
    __tablename__ = "sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires: Mapped[str] = mapped_column(String(50))


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company: Mapped[str] = mapped_column(String(120))
    year: Mapped[int] = mapped_column(Integer)
    semester: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(240), unique=True)


# Separate table permits a non-destructive upgrade of existing installations.
# Brand artwork is retained independently of future catalogue updates.
class ContractBranding(Base):
    __tablename__ = "contract_branding"
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), primary_key=True)
    logos: Mapped[list] = mapped_column(JSON)


class ContractReportStyle(Base):
    """Presentation settings have their own revision, independent of report answers."""
    __tablename__ = "contract_report_styles"
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(20), default="Lem")
    revision: Mapped[int] = mapped_column(Integer, default=1)


# Active contracts have no row here. All dependent data stays in place until expiry.
class ContractTrash(Base):
    __tablename__ = "contract_trash"
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), primary_key=True)
    deleted_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    deleted_at: Mapped[str] = mapped_column(String(50))
    purge_after: Mapped[str] = mapped_column(String(50), index=True)


# Composite key grants one technician access to one contract.
class Assignment(Base):
    __tablename__ = "assignments"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), primary_key=True)


# One independent physical station can be maintained under several contracts.
class Station(Base):
    __tablename__ = "stations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200), unique=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    verified_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    updated: Mapped[str] = mapped_column(String(50), default=now)


class StationCatalogueLock(Base):
    __tablename__ = "station_catalogue_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


# Historical table name/UUIDs are retained as contract memberships, so reports
# and permissions stay scoped to their original contract during upgrades.
class Charger(Base):
    __tablename__ = "chargers"
    __table_args__ = (UniqueConstraint("contract_id", "external_id"),
                      Index("ix_chargers_contract_station", "contract_id", "station_id", unique=True),
                      Index("ix_chargers_station", "station_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"))
    station_id: Mapped[str | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    external_id: Mapped[str] = mapped_column(String(200))
    # Kept only to retain the exact legacy import during migration/recovery.
    legacy_details: Mapped[dict] = mapped_column("details", JSON, default=dict, deferred=True)
    station: Mapped[Station | None] = relationship(lazy="selectin")
    contract_note: Mapped["ContractChargerNote | None"] = relationship(cascade="all, delete-orphan")

    @property
    def details(self):
        return self.station.details if self.station is not None else self.legacy_details

    @details.setter
    def details(self, value):
        if self.station is not None:
            self.station.details = value
            self.station.updated = now()
        else:
            self.legacy_details = value


class ContractChargerNote(Base):
    """One instruction per contract membership. A missing row means empty revision zero."""
    __tablename__ = "contract_charger_notes"
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)


def set_contract_notes(charger, value):
    note = charger.contract_note
    if value == (note.text if note else ""):
        return False
    if note is None:
        charger.contract_note = ContractChargerNote(text=value, revision=1)
    else:
        # Clearing also advances the revision, protecting against stale editors.
        note.text = value
        note.revision += 1
    return True


class DetachedCharger(Base):
    """Keep membership UUIDs for existing reports after removal from a contract."""
    __tablename__ = "detached_chargers"
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id", ondelete="CASCADE"), primary_key=True)


def attached_charger():
    return ~select(DetachedCharger.charger_id).where(DetachedCharger.charger_id == Charger.id).exists()


class DataMigration(Base):
    __tablename__ = "completed_data_migrations"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)


def clean_existing_station_data(engine):
    # The marker and both updates commit together. Concurrent startup waits
    # for the first transaction; an interrupted cleanup is retried.
    with engine.begin() as connection:
        claimed = connection.execute(text(
            "INSERT INTO completed_data_migrations (name) VALUES ('pcve-contract-notes-v1') "
            "ON CONFLICT (name) DO NOTHING RETURNING name"
        )).first()
        if claimed is None:
            return
        for model in (Station, Charger):
            table = model.__table__
            for key, data in connection.execute(select(table.c.id, table.c.details)):
                # Only a legacy membership's own import can establish a contract
                # of origin. Never fan a shared station's raw source out to its contracts.
                if model is Charger and data:
                    source = data.get("source")
                    fields = {**source, **data} if isinstance(source, dict) else data
                    raw = next((v for k, v in fields.items() if is_contract_notes(k) and v), "")
                    value = normalize_contract_notes(raw)
                    if value:
                        connection.execute(text(
                            "INSERT INTO contract_charger_notes (charger_id, text, revision) "
                            "VALUES (:id, :value, 1) ON CONFLICT (charger_id) DO NOTHING"
                        ), {"id": key, "value": value})
                clean = clean_station_details(data)
                if data and clean != data:
                    values = {"details": clean}
                    if model is Station:
                        values["updated"] = now()
                    connection.execute(table.update().where(table.c.id == key).values(**values))


# Mutable owner-controlled draft or immutable closed report. snapshot/template freeze
# context at creation; payload contains answers/notes and optional source-report lineage.
class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_charger_updated", "charger_id", "updated"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="draft")
    payload: Mapped[dict] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON)
    template: Mapped[dict] = mapped_column(JSON)
    started: Mapped[str] = mapped_column(String(50), default=now)
    updated: Mapped[str] = mapped_column(String(50), default=now)
    finished: Mapped[str | None] = mapped_column(String(50), nullable=True)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    start: Mapped[str] = mapped_column(String(10))
    due: Mapped[str] = mapped_column(String(10))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    completed: Mapped[str | None] = mapped_column(String(50))
    followers: Mapped[list] = mapped_column(JSON)
    items: Mapped[list["WorkItem"]] = relationship(order_by="WorkItem.position",
                                                    cascade="all, delete-orphan")


class WorkItem(Base):
    __tablename__ = "work_order_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    # Stable target references and display snapshots survive contract retention/purge.
    # Later maintenance kinds can address a station directly without a contract.
    target: Mapped[str] = mapped_column(String(36), index=True)
    contract_id: Mapped[str | None] = mapped_column(String(36), index=True)
    contract_name: Mapped[str | None] = mapped_column(String(240))
    station_id: Mapped[str | None] = mapped_column(String(36))
    client: Mapped[str] = mapped_column(String(120))
    external_id: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(12), default="pending")
    report_id: Mapped[str | None] = mapped_column(String(36), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    resolved_by: Mapped[str | None] = mapped_column(String(36))
    resolved_at: Mapped[str | None] = mapped_column(String(50))


class WorkOrderCancellation(Base):
    __tablename__ = "work_order_cancellations"
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), primary_key=True)
    cancelled_at: Mapped[str] = mapped_column(String(50))
    actor_id: Mapped[str] = mapped_column(String(36))


class WorkItemDraft(Base):
    # Snapshot competing drafts at resolution. Replanning the ID must not
    # make drafts created on a later visit part of the earlier order.
    __tablename__ = "work_order_recovery_drafts"
    item_id: Mapped[str] = mapped_column(ForeignKey("work_order_items.id", ondelete="CASCADE"), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)


class WorkAssignee(Base):
    __tablename__ = "work_order_assignees"
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)


class WorkNotice(Base):
    __tablename__ = "work_order_notices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    created: Mapped[str] = mapped_column(String(50), default=now)
    day: Mapped[str | None] = mapped_column(String(10))


class NoticeReceipt(Base):
    __tablename__ = "work_order_notice_receipts"
    notice_id: Mapped[str] = mapped_column(ForeignKey("work_order_notices.id", ondelete="CASCADE"), primary_key=True)
    device: Mapped[str] = mapped_column(String(36), primary_key=True)


# Independent correction history survives source deletion and records replacement links.
class ReportCorrection(Base):
    __tablename__ = "report_corrections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    editor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    original_owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(12), default="editing")
    photo_map: Mapped[dict] = mapped_column(JSON)
    identity_baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    commit_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created: Mapped[str] = mapped_column(String(50), default=now)
    committed: Mapped[str | None] = mapped_column(String(50), nullable=True)


# Permanent deletion tombstone prevents a stale device from recreating this UUID.
class DeletedReport(Base):
    __tablename__ = "deleted_reports"
    __table_args__ = (Index("ix_deleted_reports_charger", "charger_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(10))
    deleted: Mapped[str] = mapped_column(String(50), default=now)


# Independent audit history survives report deletion, so report_id is not a foreign key.
class OwnershipTransfer(Base):
    __tablename__ = "report_ownership_transfers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(String(36), index=True)
    old_owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    new_owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created: Mapped[str] = mapped_column(String(50), default=now)


# Original JPEG/PNG bytes live in the database; API projections exclude data unless needed.
class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (Index("ix_photos_report", "report_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"))
    checkpoint: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(250))
    mime: Mapped[str] = mapped_column(String(80))
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created: Mapped[str] = mapped_column(String(50), default=now)


class ImportBatch(Base):
    __tablename__ = "imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"))
    mapping: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created: Mapped[str] = mapped_column(String(50), default=now)


# Last committed mapping per company; source layouts also have versioned built-in profiles.
class ImportProfile(Base):
    __tablename__ = "import_profiles"
    company: Mapped[str] = mapped_column(String(120), primary_key=True)
    mapping: Mapped[dict] = mapped_column(JSON)


# General action trail. OwnershipTransfer stores the additional old/new-owner details.
class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(100))
    created: Mapped[str] = mapped_column(String(50), default=now)


def migrate_correction_identity(engine):
    """Add the private conflict baseline without rewriting existing corrections."""
    with engine.begin() as connection:
        table = ReportCorrection.__tablename__
        if any(c["name"] == "identity_baseline" for c in inspect(connection).get_columns(table)):
            return
        if connection.dialect.name == "postgresql":
            # A second worker may have added the column after our schema read.
            connection.exec_driver_sql(
                "ALTER TABLE report_corrections ADD COLUMN IF NOT EXISTS identity_baseline JSON")
        else:
            # SQLite lacks ADD COLUMN IF NOT EXISTS. Recheck under its writer lock.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            if not any(c["name"] == "identity_baseline" for c in inspect(connection).get_columns(table)):
                connection.exec_driver_sql("ALTER TABLE report_corrections ADD COLUMN identity_baseline JSON")


def migrate_stations(engine):
    with engine.begin() as connection:
        columns = {c["name"] for c in inspect(connection).get_columns("chargers")}
        if "station_id" not in columns:
            connection.execute(text("ALTER TABLE chargers ADD COLUMN station_id VARCHAR(36) REFERENCES stations(id)"))
        connection.execute(text("INSERT INTO station_catalogue_lock (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))
        # Serialize parallel startup migrations on PostgreSQL.
        connection.execute(select(StationCatalogueLock).with_for_update())
        rows = connection.execute(text(
            "SELECT c.id, c.external_id, c.details FROM chargers c JOIN contracts ct ON ct.id=c.contract_id "
            "WHERE c.station_id IS NULL ORDER BY ct.year, ct.semester, ct.id, c.id"
        )).all()
        if not rows:
            return
        existing = {r.external_id: dict(r._mapping) for r in connection.execute(Station.__table__.select())}
        for row in rows:
            details = json.loads(row.details) if isinstance(row.details, str) else (row.details or {})
            station = existing.get(row.external_id)
            if station is None:
                station = dict(id=str(uuid.uuid4()), external_id=row.external_id, details=details,
                               verified_fields={}, updated=now())
                connection.execute(Station.__table__.insert().values(**station))
                existing[row.external_id] = station
            else:
                merged = {**station["details"], **{k:v for k,v in details.items() if k not in station["verified_fields"]}}
                connection.execute(Station.__table__.update().where(Station.id == station["id"]).values(details=merged))
                station["details"] = merged
            connection.execute(Charger.__table__.update().where(Charger.id == row.id).values(station_id=station["id"]))


# Create missing tables and return a request-session factory. SQLite enables foreign keys
# explicitly; PostgreSQL supplies row locking used by mutation endpoints.
def database(url=None):
    url = url or os.getenv("DATABASE_URL", "sqlite:///./lem.db")
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    migrate_correction_identity(engine)
    migrate_stations(engine)
    clean_existing_station_data(engine)
    with engine.begin() as connection:
        claimed = connection.execute(text(
            "INSERT INTO completed_data_migrations (name) VALUES ('work-order-recovery-drafts-v1') "
            "ON CONFLICT (name) DO NOTHING RETURNING name"
        )).first()
        if claimed:
            # Earlier orders have no draft snapshot. Prefer the first server save
            # over a field device's clock. Older imports may only have a start date.
            connection.execute(text("""
                INSERT INTO work_order_recovery_drafts (item_id, report_id)
                SELECT i.id, r.id FROM work_order_items i JOIN reports r ON r.charger_id = i.target
                LEFT JOIN (
                    SELECT target, MIN(created) AS first_saved FROM audit
                    WHERE action = 'report.draft' GROUP BY target
                ) saved ON saved.target = r.id
                WHERE i.kind = 'preventive' AND i.state IN ('closed', 'invalid')
                  AND r.status = 'draft' AND COALESCE(saved.first_saved, r.started) <= i.resolved_at
                  AND NOT EXISTS (SELECT 1 FROM report_corrections c WHERE c.id = r.id)
                ON CONFLICT (item_id, report_id) DO NOTHING
            """))
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO account_admin_lock (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        )
    # create_all skips indexes on tables that already exist. Add only these
    # explicitly versioned indexes on prototype upgrades; no data reset needed.
    for table_name, index_name in (
        ("chargers", "ix_chargers_contract_station"),
        ("chargers", "ix_chargers_station"),
        ("reports", "ix_reports_charger_updated"),
        ("deleted_reports", "ix_deleted_reports_charger"),
        ("photos", "ix_photos_report"),
    ):
        index = next(i for i in Base.metadata.tables[table_name].indexes if i.name == index_name)
        index.create(engine, checkfirst=True)
    return engine, sessionmaker(engine, expire_on_commit=False)
