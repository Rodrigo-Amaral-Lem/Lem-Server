from .report_extras import validate_extras
from sqlalchemy.orm import joinedload
from .stations import lock_catalogue, get_or_create_station, close_identity, StationConflict
# FastAPI composition root: dependencies, authorization and transactional endpoints.
# All handlers are registered inside create_app so tests can supply an isolated DB.
# Read docs/developer/server-and-api.md alongside this file; models.py owns the schema.
# Comments explain current behavior, including deletion and conflict exceptions.

import json, os, secrets, hashlib, uuid, threading, asyncio, logging
from contextlib import asynccontextmanager
from tempfile import SpooledTemporaryFile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update, delete, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from .models import (
    database,
    User,
    SessionToken,
    Contract,
    ContractTrash,
    Assignment,
    Charger,
    Station,
    Report,
    DeletedReport,
    OwnershipTransfer,
    Photo,
    ImportProfile,
    ImportBatch,
    Audit,
    now,
)
from .security import hash_password, verify, digest, LoginLimiter
from .request_limits import RequestBodyLimit
from .compatibility import ClientPolicy, VERSION
from .importer import analyse, parse
from .images import InvalidImage
from .media import inspect_evidence
from .checklist import for_charger, corrected_draft_template
from .measurements import digits as measurement_digits, format_value, not_applicable
from .exports import write_bundle, file_chunks, MissingEvidence
from .contract_trash import RETENTION, utc_now, purge_expired

TEMPLATE = json.loads(
    (
        Path(
            os.getenv(
                "CHECKLIST_FILE",
                str(Path(__file__).resolve().parents[2] / "shared/checklist-v1.json"),
            )
        )
    ).read_text(encoding="utf-8")
)


# Docker copies shared into server/shared; development uses sibling shared.
class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class NewUser(Login):
    name: str = Field(min_length=1, max_length=200)
    role: str = "tech"


class NewContract(BaseModel):
    company: str = Field(min_length=1, max_length=120)
    year: int = Field(ge=2020, le=2100)
    semester: int = Field(ge=1, le=2)
    kind: str = Field(min_length=1, max_length=40)

    @field_validator("company", "kind", mode="before")
    @classmethod
    def trim_required_text(cls, value):
        # Apply length constraints after trimming, not before accepting whitespace.
        return value.strip() if isinstance(value, str) else value


class TransferInput(BaseModel):
    owner_id: str
    base_revision: int = Field(ge=1)


class TrashContractInput(BaseModel):
    confirmation: str = Field(max_length=20)


class ReportInput(BaseModel):
    checklist_version: int = Field(ge=1, strict=True)
    load_test_connectors: list[str] | None = Field(default=None, max_length=64)
    charger_id: str
    base_revision: int = Field(ge=0)
    status: str = "draft"
    payload: dict


def uid():
    return str(uuid.uuid4())


def uuid_check(value):
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(422, "UUID inválido")


# Create an engine/session factory, bootstrap only an empty installation, and register routes.
def create_app(url=None, bootstrap=True):
    engine, Factory = database(url)
    if bootstrap:
        with Factory() as db:
            if not db.scalar(select(User).limit(1)):
                password = os.getenv("ADMIN_PASSWORD", "")
                if not password:
                    raise RuntimeError(
                        "Defina ADMIN_PASSWORD (mínimo 12 caracteres) para criar o administrador"
                    )
                db.add(
                    User(
                        id=uid(),
                        email=os.getenv("ADMIN_EMAIL", "admin@lem.pt").lower(),
                        name=(os.getenv("ADMIN_NAME", "Administrador").strip() or "Administrador"),
                        password=hash_password(password),
                        role="admin",
                    )
                )
                db.commit()
    policy = ClientPolicy()

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()

        async def sweep():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(purge_expired, Factory)
                except Exception:
                    logging.getLogger(__name__).exception("Contract retention sweep failed; retrying")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            stop.set()
            await task

    def compatible_client(
        request: Request, x_lem_client_version: str | None = Header(default=None)
    ):
        if not request.url.path.startswith("/api/") or request.url.path in (
            "/api/health",
            "/api/client-compatibility",
        ):
            return
        result = policy.evaluate(x_lem_client_version)
        if not result["compatible"]:
            raise HTTPException(426, detail=result)

    # The interactive documentation/schema is public when enabled: keep it for development only.
    api_docs = os.getenv("LEM_API_DOCS", "") == "1"
    app = FastAPI(
        title="Lem API",
        lifespan=lifespan,
        version=VERSION,
        dependencies=[Depends(compatible_client)],
        docs_url="/docs" if api_docs else None,
        redoc_url="/redoc" if api_docs else None,
        openapi_url="/openapi.json" if api_docs else None,
    )
    app.state.client_policy = policy
    app.state.factory = Factory
    app.state.purge_expired_contracts = lambda instant=None: purge_expired(Factory, instant)
    bearer = HTTPBearer()
    app.add_middleware(RequestBodyLimit)
    limiter = LoginLimiter()
    # Use the same scrypt work for unknown users, avoiding the cheap early exit.
    dummy_password = hash_password(secrets.token_urlsafe(32))

    def resource_limit(name, default, maximum):
        try:
            return max(1, min(maximum, int(os.getenv(name, str(default)))))
        except ValueError:
            return default

    export_slots = threading.BoundedSemaphore(resource_limit("LEM_EXPORT_CONCURRENCY", 2, 4))
    image_slots = threading.BoundedSemaphore(resource_limit("LEM_IMAGE_CONCURRENCY", 1, 2))

    def admission(slots, operation):
        # Fail fast BEFORE authentication opens a DB transaction. Waiting while
        # holding that connection can starve login/sync under concurrent exports.
        if not slots.acquire(blocking=False):
            raise HTTPException(
                503,
                f"Servidor ocupado: {operation}; tente novamente",
                headers={"Retry-After": "15"},
            )
        try:
            yield
        finally:
            slots.release()

    def export_admission():
        yield from admission(export_slots, "exportação")

    def image_admission():
        yield from admission(image_slots, "validação de fotografias")

    # One SQLAlchemy session per request; endpoint handlers explicitly commit accepted writes.
    def db_dep():
        with Factory() as db:
            yield db

    # Resolve only the digest of a bearer token; raw session tokens are not stored in the DB.
    def user_dep(auth: HTTPAuthorizationCredentials = Depends(bearer), db=Depends(db_dep)):
        token = db.get(SessionToken, digest(auth.credentials))
        if not token or token.expires < now():
            raise HTTPException(401, "Sessão expirada; entre novamente")
        user = db.scalar(select(User).where(User.id == token.user_id)
                         .with_for_update(read=True).execution_options(populate_existing=True))
        if not user or user.role not in ("admin", "tech"):
            raise HTTPException(401, "Utilizador inexistente")
        # If a credential change won while this request waited for the User lock,
        # the token read above may already have been revoked. Recheck afterwards.
        token = db.scalar(select(SessionToken).where(SessionToken.digest == token.digest)
                          .execution_options(populate_existing=True))
        if not token or token.expires < now():
            raise HTTPException(401, "Sessão expirada; entre novamente")
        return user

    # Administrative access is not an implicit right to edit another owner's draft.
    def admin(user=Depends(user_dep)):
        if user.role != "admin":
            raise HTTPException(403, "Apenas administrador")
        return user

    # Contract scope is checked independently from report ownership. Administrators see all contracts.
    def accessible(db, user, contract_id, write=False):
        c = db.scalar(select(Contract).where(Contract.id == contract_id).with_for_update(read=not write))
        if not c:
            raise HTTPException(404, "Contrato inexistente")
        if user.role != "admin" and not db.get(Assignment, (user.id, c.id)):
            raise HTTPException(403, "Contrato não atribuído")
        # Read the marker AFTER obtaining the lock, with a fresh statement
        # snapshot if we waited for a concurrent trash transaction to commit.
        if db.get(ContractTrash, c.id):
            # A trashed contract is unavailable, not a permanently deleted report.
            # Do not send report HTTP 410: older clients erase local copies for it.
            raise HTTPException(423, "Contrato apagado; aguarda recuperação pelo administrador. Dados locais conservados.")
        return c

    # Common read/write gate, including permanent deletion markers.
    # Write access requires the current owner even when the caller is an administrator.
    def report_access(db, user, rid, write=False):
        r = db.get(Report, rid)
        if not r:
            tombstone = db.get(DeletedReport, rid)
            if tombstone:
                charger = db.get(Charger, tombstone.charger_id)
                accessible(db, user, charger.contract_id)
                raise HTTPException(410, "Report apagado definitivamente")
            raise HTTPException(404, "Report inexistente")
        charger = db.get(Charger, r.charger_id)
        accessible(db, user, charger.contract_id)
        if write and r.owner_id != user.id:
            raise HTTPException(
                403,
                "Só o proprietário atual pode editar ou concluir; um administrador deve transferir a propriedade primeiro",
            )
        return r

    def audit(db, u, action, target):
        db.add(Audit(id=uid(), user_id=u.id, action=action, target=target))

    # Project columns only: catalogue/report reads must not load binary image blobs.
    def photo_metadata(db, rid):
        return [
            {
                "id": p.id,
                "checkpoint": p.checkpoint,
                "filename": p.filename,
                "mime": p.mime,
                "digest": p.digest,
                "size": p.size,
            }
            for p in db.execute(
                select(
                    Photo.id,
                    Photo.checkpoint,
                    Photo.filename,
                    Photo.mime,
                    Photo.digest,
                    func.length(Photo.data).label("size"),
                ).where(Photo.report_id == rid)
            )
        ]

    # Serialize one report, including its frozen context/checklist and photograph metadata.
    def report_json(db, r):
        owner = db.get(User, r.owner_id)
        return {
            "id": r.id,
            "charger_id": r.charger_id,
            "owner_id": r.owner_id if owner and owner.role in ("admin", "tech") else "",
            "technician": owner.name if owner and owner.role in ("admin", "tech") else "Sem proprietário",
            "revision": r.revision,
            "status": r.status,
            "payload": r.payload,
            "snapshot": r.snapshot,
            "template": corrected_draft_template(r.template, r.status),
            "started": r.started,
            "updated": r.updated,
            "finished": r.finished,
            "photos": photo_metadata(db, r.id),
        }

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": VERSION}

    @app.get("/api/client-compatibility")
    def client_compatibility(request: Request, response: Response):
        # Public and cheap: a client can check this before transmitting a password.
        response.headers["Cache-Control"] = "no-store"
        return policy.evaluate(request.headers.get("X-Lem-Client-Version"))

    # Normalize identity before all rate limits and run scrypt even for absent users.
    @app.post("/api/login")
    def login(body: Login, request: Request, db=Depends(db_dep)):
        email = body.email.strip().lower()
        limiter.consume(request.client.host if request.client else "", email)
        user = db.scalar(select(User).where(User.email == email).with_for_update(read=True))
        valid = verify(body.password, user.password if user else dummy_password)
        if not user or user.role not in ("admin", "tech") or not valid:
            raise HTTPException(401, "Credenciais inválidas")
        token = secrets.token_urlsafe(32)
        db.execute(delete(SessionToken).where(SessionToken.expires < now()))
        db.add(
            SessionToken(
                digest=digest(token),
                user_id=user.id,
                expires=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            )
        )
        db.commit()
        return {
            "token": token,
            "user": {"id": user.id, "name": user.name, "email": user.email, "role": user.role},
        }

    @app.post("/api/logout")
    def logout(
        auth: HTTPAuthorizationCredentials = Depends(bearer),
        u=Depends(user_dep),
        db=Depends(db_dep),
    ):
        db.execute(delete(SessionToken).where(SessionToken.digest == digest(auth.credentials)))
        db.commit()
        return {"ok": True}

    @app.get("/api/template")
    def template(u=Depends(user_dep)):
        return TEMPLATE

    @app.get("/api/users")
    def users(u=Depends(admin), db=Depends(db_dep)):
        return [
            {"id": x.id, "email": x.email, "name": x.name, "role": x.role}
            for x in db.scalars(select(User).where(User.role.in_(("admin", "tech"))))
        ]

    @app.post("/api/users")
    def add_user(body: NewUser, u=Depends(admin), db=Depends(db_dep)):
        if body.role not in ["admin", "tech"]:
            raise HTTPException(422, "Perfil inválido")
        if db.scalar(select(User).where(User.email == body.email.lower().strip())):
            raise HTTPException(409, "Email já existe")
        v = User(
            id=uid(),
            email=body.email.strip().lower(),
            name=body.name,
            password=hash_password(body.password),
            role=body.role,
        )
        db.add(v)
        audit(db, u, "user.create", v.id)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Email já existe") from None
        return {"id": v.id}

    @app.get("/api/contracts")
    def contracts(u=Depends(user_dep), db=Depends(db_dep)):
        query = select(Contract).where(~Contract.id.in_(select(ContractTrash.contract_id))).order_by(Contract.year.desc(), Contract.company, Contract.semester)
        if u.role != "admin":
            query = query.join(Assignment, Assignment.contract_id == Contract.id).where(
                Assignment.user_id == u.id
            )
        return [
            {
                "id": c.id,
                "company": c.company,
                "year": c.year,
                "semester": c.semester,
                "kind": c.kind,
                "name": c.name,
            }
            for c in db.scalars(query)
        ]

    @app.get("/api/admin/deleted-contracts")
    def deleted_contracts(u=Depends(admin), db=Depends(db_dep)):
        # Expired entries cannot be recovered, even if the next physical sweep
        # has not run yet (or the server was stopped at their exact deadline).
        return [dict(id=c.id, name=c.name, deleted_at=t.deleted_at,
                     purge_after=t.purge_after, deleted_by=t.deleted_by)
                for c, t in db.execute(select(Contract, ContractTrash)
                    .join(ContractTrash, ContractTrash.contract_id == Contract.id)
                    .where(ContractTrash.purge_after > now())
                    .order_by(ContractTrash.deleted_at.desc()))]

    @app.post("/api/contracts/{cid}/trash")
    def trash_contract(cid: str, body: TrashContractInput, u=Depends(admin), db=Depends(db_dep)):
        if body.confirmation != "APAGAR":
            raise HTTPException(422, 'Escreva exatamente "APAGAR" para confirmar')
        c = db.scalar(select(Contract).where(Contract.id == cid).with_for_update())
        if c is None:
            raise HTTPException(404, "Contrato inexistente")
        t = db.get(ContractTrash, cid)
        if t is None:
            instant = utc_now()
            t = ContractTrash(contract_id=cid, deleted_by=u.id,
                              deleted_at=instant.isoformat(),
                              purge_after=(instant + RETENTION).isoformat())
            db.add(t)
            audit(db, u, "contract.trash", cid)
            db.commit()
        # Retrying a lost acknowledgement must never extend the deadline.
        return dict(id=cid, deleted=True, purge_after=t.purge_after)

    @app.post("/api/contracts/{cid}/restore")
    def restore_contract(cid: str, u=Depends(admin), db=Depends(db_dep)):
        c = db.scalar(select(Contract).where(Contract.id == cid).with_for_update())
        if c is None:
            raise HTTPException(404, "Contrato inexistente ou já eliminado definitivamente")
        t = db.get(ContractTrash, cid)
        if t is not None:
            if t.purge_after <= now():
                raise HTTPException(410, "Prazo de recuperação de 30 dias terminado")
            db.delete(t)
            audit(db, u, "contract.restore", cid)
            db.commit()
        return dict(id=cid, restored=True, name=c.name)

    @app.post("/api/contracts")
    def add_contract(body: NewContract, u=Depends(admin), db=Depends(db_dep)):
        name = f"{body.year}_{body.semester}ºS_{body.company.strip()}_{body.kind.strip()}"
        if db.scalar(select(Contract).where(Contract.name == name)):
            raise HTTPException(409, "Contrato já existe")
        c = Contract(
            id=uid(),
            company=body.company.strip(),
            year=body.year,
            semester=body.semester,
            kind=body.kind.strip(),
            name=name,
        )
        db.add(c)
        audit(db, u, "contract.create", c.id)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Contrato já existe") from None
        return {"id": c.id, "name": c.name}

    @app.put("/api/contracts/{cid}/users/{user_id}")
    def assign(cid: str, user_id: str, u=Depends(admin), db=Depends(db_dep)):
        target = db.scalar(select(User).where(User.id == user_id)
                           .with_for_update(read=True).execution_options(populate_existing=True))
        if not target or target.role not in ("admin", "tech"):
            raise HTTPException(404, "Utilizador inexistente")
        accessible(db, u, cid)
        # PUT is idempotent even when another administrator assigns the same
        # account concurrently after our permission checks.
        insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        db.execute(insert(Assignment).values(user_id=user_id, contract_id=cid)
                   .on_conflict_do_nothing(index_elements=[Assignment.user_id, Assignment.contract_id]))
        audit(db, u, "contract.assign", cid + ":" + user_id)
        db.commit()
        return {"ok": True}

    # Return lightweight report summaries in bulk; avoid loading payloads/photos per charger.
    @app.get("/api/contracts/{cid}/chargers")
    def chargers(cid: str, u=Depends(user_dep), db=Depends(db_dep)):
        accessible(db, u, cid)
        # Bulk metadata queries; no answer payloads, templates or photo BLOBs.
        summaries = {}
        rows = db.execute(
            select(Report.charger_id, Report.id, Report.status, Report.owner_id, Report.updated)
            .join(Charger, Charger.id == Report.charger_id)
            .where(Charger.contract_id == cid)
            .order_by(Report.updated.desc())
        )
        for row in rows:
            summaries.setdefault(row.charger_id, []).append(
                {
                    "id": row.id,
                    "status": row.status,
                    "owner_id": row.owner_id,
                    "updated": row.updated,
                }
            )
        removed = {}
        for row in db.execute(
            select(DeletedReport.charger_id, DeletedReport.id)
            .join(Charger, Charger.id == DeletedReport.charger_id)
            .where(Charger.contract_id == cid)
        ):
            removed.setdefault(row.charger_id, []).append(row.id)
        return [
            {
                "id": c.id,
                "contract_id": cid,
                "station_id": c.station_id,
                "external_id": c.external_id,
                "details": c.details,
                "reports": summaries.get(c.id, []),
                "deleted_reports": removed.get(c.id, []),
            }
            for c in db.scalars(
                select(Charger).options(joinedload(Charger.station)).where(Charger.contract_id == cid).order_by(Charger.external_id)
            )
        ]

    @app.get("/api/companies/{company}/import-profile")
    def profile(company: str, u=Depends(admin), db=Depends(db_dep)):
        p = db.get(ImportProfile, company)
        return p.mapping if p else {}

    # These endpoints use synchronous SQLAlchemy, XML and image processing.
    # Run the complete operation in FastAPI's bounded worker pool, keeping the
    # event loop available for uploads and other clients (including health checks).
    def file_bytes(file, limit=20 * 1024 * 1024):
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, "Máximo 20 MB por ficheiro")
        return data

    @app.post("/api/imports/analyse")
    def import_analyse(file: UploadFile = File(), u=Depends(admin)):
        return analyse(file_bytes(file))

    # Preview and commit run the same parser. Reject the entire import on validation errors;
    # within a contract, update existing Station IDs and insert previously unseen ones.
    @app.post("/api/contracts/{cid}/import")
    def import_file(
        cid: str,
        file: UploadFile = File(),
        mapping: str = Form(),
        commit: bool = Form(False),
        u=Depends(admin),
        db=Depends(db_dep),
    ):
        try:
            config = json.loads(mapping)
        except ValueError:
            raise HTTPException(422, "Mapeamento JSON inválido")
        if not isinstance(config, dict):
            raise HTTPException(422, "Mapeamento deve ser objeto")
        data = file_bytes(file)
        result = parse(data, config)
        # Parsing does not need a contract lock: keep ordinary field requests
        # moving while a large workbook is decoded. Recheck access/retention
        # afterwards, then acquire the final lock directly (never upgrade a
        # shared lock). Only the database commit phase serializes writers.
        contract = accessible(db, u, cid, write=commit)
        if not commit:
            return {
                "count": result["count"],
                "errors": result["errors"],
                "sample": result["records"][:10],
                "records": result["records"],
                "profile": result.get("profile"),
            }
        if result["errors"]:
            raise HTTPException(
                422, {"message": "Importação bloqueada; corrija os IDs", "errors": result["errors"]}
            )
        created = changed = 0
        lock_catalogue(db)
        existing = {
            c.external_id: c for c in db.scalars(select(Charger).where(Charger.contract_id == cid))
        }
        stations = {c.station.external_id:c.station for c in existing.values() if c.station is not None}
        station_ids = [r["external_id"] for r in result["records"] if r["external_id"] not in stations]
        for offset in range(0, len(station_ids), 500):
            stations.update({s.external_id:s for s in db.scalars(
                select(Station).where(Station.external_id.in_(station_ids[offset:offset + 500]))
            )})
        for rec in result["records"]:
            station = get_or_create_station(db, rec["external_id"], rec["details"], stations)
            c = existing.get(rec["external_id"])
            if c:
                c.station = station
                changed += 1
            else:
                c = Charger(id=uid(), contract_id=cid, external_id=rec["external_id"], station=station)
                db.add(c)
                existing[c.external_id] = c
                created += 1
        # Companies can span several contracts. A per-contract lock cannot
        # protect their shared profile key: use one atomic upsert instead of
        # SELECT-then-INSERT. The last committed mapping remains the default.
        insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        profile = insert(ImportProfile).values(company=contract.company, mapping=config)
        db.execute(profile.on_conflict_do_update(
            index_elements=[ImportProfile.company], set_={"mapping": profile.excluded.mapping}))
        out = {"created": created, "updated": changed}
        batch = uid()
        db.add(
            ImportBatch(
                id=batch,
                contract_id=cid,
                mapping=config,
                digest=hashlib.sha256(data).hexdigest(),
                result=out,
            )
        )
        audit(db, u, "import.commit", batch)
        db.commit()
        return out

    # A revision-zero client may be recovering from a lost creation acknowledgement.
    # A tombstone produces 410, not exists=false, so stale clients cannot recreate a deleted report.
    @app.get("/api/reports/{rid}/exists")
    def report_exists(rid: str, u=Depends(user_dep), db=Depends(db_dep)):
        r = db.get(Report, rid)
        if not r:
            if db.get(DeletedReport, rid):
                report_access(db, u, rid)
            return {"exists": False}
        r = report_access(db, u, rid)
        return {"exists": True, **report_json(db, r)}

    @app.get("/api/reports/{rid}")
    def get_report(rid: str, u=Depends(user_dep), db=Depends(db_dep)):
        return report_json(db, report_access(db, u, rid))

    # Accept timezone-aware ISO dates and normalize to UTC; missing dates use server time.
    def report_time(payload, key):
        raw = payload.get(key)
        if not raw:
            return now()
        if not isinstance(raw, str):
            raise HTTPException(422, "Data inválida")
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("timezone necessária")
            return value.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise HTTPException(422, "Data ISO 8601 inválida")

    # Create or update a report with optimistic concurrency. Ownership/revision conditions
    # are enforced in the final UPDATE, not just in an earlier read. Closure validates all evidence.
    @app.put("/api/reports/{rid}")
    def put_report(rid: str, body: ReportInput, u=Depends(user_dep), db=Depends(db_dep)):
        uuid_check(rid)
        if body.status not in ["draft", "closed"]:
            raise HTTPException(422, "Estado inválido")
        if len(json.dumps(body.payload)) > 300000:
            raise HTTPException(413, "Report demasiado grande")
        charger = db.get(Charger, body.charger_id)
        if not charger:
            raise HTTPException(404, "Carregador inexistente")
        contract = accessible(db, u, charger.contract_id)
        # Shared-identity mutations use catalogue -> membership lock order,
        # including console renames that update all contract memberships.
        if body.status == "closed":
            lock_catalogue(db)
        # Always lock the contract before its charger, matching trash/purge.
        charger = db.scalar(select(Charger).where(Charger.id == body.charger_id).with_for_update())
        if db.get(DeletedReport, rid):
            raise HTTPException(410, "Report apagado definitivamente; não pode ser recriado")
        r = db.get(Report, rid)
        if r:
            if body.checklist_version != r.template.get("version"):
                raise HTTPException(409, "A versão da checklist não corresponde ao report; cópia local conservada")
            report_access(db, u, rid, write=True)
            if r.charger_id != charger.id:
                raise HTTPException(409, "Não pode alterar carregador do report")
            if r.revision != body.base_revision:
                raise HTTPException(
                    409, {"message": "Report alterado noutro dispositivo", "revision": r.revision}
                )
            if r.status == "closed":
                raise HTTPException(409, "Report fechado é imutável; crie nova intervenção")
        else:
            if body.checklist_version != TEMPLATE["version"]:
                raise HTTPException(409, "A checklist mudou. Crie um novo report; o rascunho antigo foi conservado para consulta")
            if body.base_revision != 0:
                raise HTTPException(409, "Report inexistente com revisão diferente de zero")
            try:
                checklist = for_charger(TEMPLATE, charger.details)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            if (
                body.load_test_connectors is not None
                and body.load_test_connectors != checklist["load_test_connectors"]
            ):
                raise HTTPException(
                    409,
                    "Os conectores do carregador foram alterados. Atualize o catálogo e crie uma nova intervenção; a cópia local é conservada.",
                )
            r = Report(
                id=rid,
                charger_id=charger.id,
                owner_id=u.id,
                revision=0,
                payload={},
                started=report_time(body.payload, "started"),
                snapshot={
                    "charger": {**charger.details, "external_id": charger.external_id, "station_id": charger.station_id},
                    "contract": {
                        "name": contract.name,
                        "company": contract.company,
                        "year": contract.year,
                        "semester": contract.semester,
                        "kind": contract.kind,
                    },
                },
                template=checklist,
            )
            db.add(r)
            db.flush()
        r.template = corrected_draft_template(r.template, r.status)
        items = {i["id"]: i for s in r.template["sections"] for i in s["items"]}
        answers = body.payload.get("answers", {})
        if not isinstance(answers, dict) or any(k not in items for k in answers):
            raise HTTPException(422, "Resposta/checkpoint inválido")
        for key, a in answers.items():
            if (
                not isinstance(a, dict)
                or not isinstance(a.get("value", ""), str)
                or not isinstance(a.get("notes", ""), str)
            ):
                raise HTTPException(422, "Resposta deve ter texto e observações")
            if len(a.get("value", "")) > 8000 or len(a.get("notes", "")) > 8000:
                raise HTTPException(422, "Texto demasiado longo")
            if "assessment" in a and (not items[key].get("reading_required") or a["assessment"] not in ("OK", "NOK")):
                raise HTTPException(422, "Avaliação inválida")
            if items[key]["kind"] == "status" and a.get("value", "") not in ["", "OK", "NOK", "NA"]:
                raise HTTPException(422, "Estado deve ser OK, NOK ou NA")
        try:
            validate_extras(body.payload, items)
        except ValueError as error:
            raise HTTPException(422, str(error))
        if body.status == "closed":
            # Drafts retain in-progress typing (e.g. a trailing decimal separator).
            # A closed report must contain complete, formatted measurements.
            for key, answer in answers.items():
                if measurement_digits(key):
                    try:
                        answer["value"] = format_value(key, answer.get("value", ""))
                    except ValueError as error:
                        raise HTTPException(422, str(error))
            photos = photo_metadata(db, rid)
            try:
                validate_extras(body.payload, items, photos)
            except ValueError as error:
                raise HTTPException(422, str(error))
            errors = []
            for key, item in items.items():
                a = answers.get(key, {})
                value = a.get("value", "").strip()
                if not value and not item.get("optional"):
                    errors.append(key + ": falta resposta")
                if item.get("reading_required") and value.upper() in ("OK", "NOK"):
                    errors.append(key + ": falta valor medido")
                if not item.get("notes_disabled") and (value == "NOK" or a.get("assessment") == "NOK") and not a.get("notes", "").strip():
                    errors.append(key + ": NOK precisa de observação")
                if item.get("na_notes_required") and not_applicable(value) and not a.get("notes", "").strip():
                    errors.append(key + ": N/A precisa de observação")
                if (
                    item["photo_required"]
                    and (item.get("photo_always") or not not_applicable(value))
                    and not any(p["checkpoint"] == key and not p["mime"].startswith("video/") for p in photos)
                ):
                    errors.append(key + ": falta fotografia")
            if errors:
                raise HTTPException(422, {"message": "Checklist incompleta", "items": errors})
        next_snapshot = r.snapshot
        if body.status == "closed":
            try:
                next_snapshot = close_identity(db, charger, r, body.payload, u.id)
            except StationConflict as error:
                raise HTTPException(409, str(error))
            except ValueError as error:
                raise HTTPException(422, str(error))
        new_revision = body.base_revision + 1
        updated = now()
        result = db.execute(
            update(Report)
            .where(Report.id == rid, Report.owner_id == u.id, Report.revision == body.base_revision)
            .values(
                payload=body.payload,
                snapshot=next_snapshot,
                revision=new_revision,
                status=body.status,
                updated=updated,
                finished=report_time(body.payload, "finished") if body.status == "closed" else None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise HTTPException(409, "Conflito de revisão; atualize antes de editar")
        audit(db, u, "report." + body.status, rid)
        db.commit()
        db.expire_all()
        out = report_json(db, db.get(Report, rid))
        if body.status == "closed":
            out["station_update"] = {"station_id": charger.station_id, "external_id": charger.external_id,
                                     "details": charger.details}
        return out

    # List administrators plus technicians assigned to this report's contract.
    @app.get("/api/reports/{rid}/owners")
    def report_owners(rid: str, u=Depends(admin), db=Depends(db_dep)):
        r = report_access(db, u, rid)
        cid = db.get(Charger, r.charger_id).contract_id
        users = db.scalars(
            select(User).where(User.role.in_(("admin", "tech"))).where(
                (User.role == "admin")
                | User.id.in_(select(Assignment.user_id).where(Assignment.contract_id == cid))
            )
        )
        return [
            {"id": x.id, "name": x.name, "label": x.name + " • " + x.email, "role": x.role}
            for x in users
        ]

    # Serialize against edits/deletion, increment revision, and commit ownership history
    # in the same transaction. A later transfer back still invalidates old offline revisions.
    @app.post("/api/reports/{rid}/owner")
    def transfer_owner(rid: str, body: TransferInput, u=Depends(admin), db=Depends(db_dep)):
        # Actor/target User locks precede contract/report locks, matching console
        # credential changes. A deleted target cannot receive a new assignment.
        target = db.scalar(select(User).where(User.id == body.owner_id)
                           .with_for_update(read=True).execution_options(populate_existing=True))
        if not target or target.role not in ("admin", "tech"):
            raise HTTPException(422, "Proprietário inexistente")
        r = report_access(db, u, rid)
        # Same lock order as PUT/delete; all photo mutations also lock this report.
        db.scalar(select(Charger).where(Charger.id == r.charger_id).with_for_update())
        r = db.scalar(
            select(Report)
            .where(Report.id == rid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not r:
            raise HTTPException(410, "Report apagado")
        if r.status != "draft":
            raise HTTPException(409, "Só pode transferir rascunhos")
        if r.revision != body.base_revision:
            raise HTTPException(409, "Report alterado; consulte a versão atual antes de transferir")
        accessible(db, target, db.get(Charger, r.charger_id).contract_id)
        if r.owner_id == target.id:
            return report_json(db, r)
        old = r.owner_id
        changed = db.execute(
            update(Report)
            .where(
                Report.id == rid,
                Report.revision == body.base_revision,
                Report.owner_id == old,
                Report.status == "draft",
            )
            .values(owner_id=target.id, revision=body.base_revision + 1, updated=now())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise HTTPException(409, "Propriedade ou revisão alterada; tente novamente")
        db.add(
            OwnershipTransfer(
                id=uid(), report_id=rid, old_owner_id=old, new_owner_id=target.id, actor_id=u.id
            )
        )
        audit(db, u, "report.transfer", rid)
        db.commit()
        db.expire_all()
        return report_json(db, db.get(Report, rid))

    # Administrative deletion is intentionally broader than editing ownership.
    # Delete image rows and create a permanent report tombstone in the same transaction.
    @app.delete("/api/reports/{rid}")
    def delete_report(rid: str, base_revision: int, u=Depends(user_dep), db=Depends(db_dep)):
        uuid_check(rid)
        r = db.get(Report, rid) or db.get(DeletedReport, rid)
        if not r:
            raise HTTPException(404, "Report inexistente")
        # Contract -> charger -> report matches retention and console mutations.
        charger = db.get(Charger, r.charger_id)
        accessible(db, u, charger.contract_id)
        charger = db.scalar(select(Charger).where(Charger.id == r.charger_id).with_for_update())
        r = db.scalar(
            select(Report)
            .where(Report.id == rid)
            .with_for_update()
            .execution_options(populate_existing=True)
        ) or db.get(DeletedReport, rid)
        if not r:
            raise HTTPException(404, "Report inexistente")
        if u.role != "admin" and (r.owner_id != u.id or r.status != "draft"):
            raise HTTPException(
                403, "Só pode apagar os seus rascunhos; reports fechados exigem administrador"
            )
        if isinstance(r, DeletedReport):
            return {"id": rid, "deleted": True}
        if r.revision != base_revision:
            raise HTTPException(
                409, "Report alterado noutro dispositivo; sincronize ou atualize antes de apagar"
            )
        db.execute(delete(Photo).where(Photo.report_id == rid))
        db.add(
            DeletedReport(id=r.id, charger_id=r.charger_id, owner_id=r.owner_id, status=r.status)
        )
        db.delete(r)
        audit(db, u, "report.delete", rid)
        db.commit()
        return {"id": rid, "deleted": True}

    # Lock/recheck the owner before mutating photographs. Repeated IDs are accepted only
    # when report/checkpoint/digest match, making a lost upload acknowledgement retryable.
    @app.put("/api/reports/{rid}/photos/{pid}")
    def add_photo(
        rid: str,
        pid: str,
        checkpoint: str = Form(),
        base_revision: int = Form(),
        file: UploadFile = File(),
        _slot=Depends(image_admission),
        u=Depends(user_dep),
        db=Depends(db_dep),
    ):
        uuid_check(pid)
        r = report_access(db, u, rid, write=True)
        # Row lock serializes photos with closure on PostgreSQL.
        # Re-read under the lock: deletion may win after report_access().
        # Refreshing the stale object would raise an internal SQLAlchemy error.
        r = db.scalar(
            select(Report).where(Report.id == rid).with_for_update()
            .execution_options(populate_existing=True)
        )
        if r is None:
            raise HTTPException(410, "Report apagado durante a operação")
        if r.owner_id != u.id:
            raise HTTPException(
                403, "A propriedade do rascunho mudou; cópia local deve ser conservada"
            )
        if checkpoint not in {i["id"] for s in r.template["sections"] for i in s["items"]}:
            raise HTTPException(422, "Checkpoint inválido")
        data = file_bytes(file)
        # Images are fully decoded; videos receive bounded container/stream inspection.
        # Reject unsupported evidence before committing an immutable attachment.
        try:
            mime = inspect_evidence(data)
        except InvalidImage as error:
            raise HTTPException(422, str(error))
        sha = hashlib.sha256(data).hexdigest()
        # Compare identity columns only; do not load the stored image for a retry.
        existing = db.execute(
            select(Photo.report_id, Photo.checkpoint, Photo.digest).where(Photo.id == pid)
        ).first()
        if existing:
            if (
                existing.report_id != rid
                or existing.checkpoint != checkpoint
                or existing.digest != sha
            ):
                raise HTTPException(409, "ID de fotografia já utilizado")
            return {"id": pid, "digest": sha}
        if r.status == "closed":
            raise HTTPException(409, "Report fechado")
        if r.revision != base_revision:
            raise HTTPException(409, "Revisão alterada antes do envio da fotografia")
        count, total = db.execute(
            select(func.count(Photo.id), func.coalesce(func.sum(func.length(Photo.data)), 0)).where(
                Photo.report_id == rid
            )
        ).one()
        if count >= 200 or total + len(data) > 250 * 1024 * 1024:
            raise HTTPException(413, "Máximo 200 fotografias e 250 MB por report")
        db.add(
            Photo(
                id=pid,
                report_id=rid,
                checkpoint=checkpoint,
                filename=Path(file.filename or "foto").name[:250],
                mime=mime,
                digest=sha,
                data=data,
            )
        )
        audit(db, u, "photo.add", pid)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "ID de fotografia já utilizado") from None
        return {"id": pid, "digest": sha}

    # An already absent photograph is an idempotent success; a real removal advances revision.
    @app.delete("/api/reports/{rid}/photos/{pid}")
    def delete_photo(
        rid: str, pid: str, base_revision: int, u=Depends(user_dep), db=Depends(db_dep)
    ):
        uuid_check(pid)
        r = report_access(db, u, rid, write=True)
        # Re-read under the lock: deletion may win after report_access().
        # Refreshing the stale object would raise an internal SQLAlchemy error.
        r = db.scalar(
            select(Report).where(Report.id == rid).with_for_update()
            .execution_options(populate_existing=True)
        )
        if r is None:
            raise HTTPException(410, "Report apagado durante a operação")
        if r.owner_id != u.id:
            raise HTTPException(
                403, "A propriedade do rascunho mudou; cópia local deve ser conservada"
            )
        if r.status != "draft":
            raise HTTPException(409, "Só é possível remover fotografias de reports em rascunho")
        p = db.get(Photo, pid)
        if p and p.report_id != rid:
            raise HTTPException(404, "Fotografia inexistente neste report")
        # Idempotent retry after a lost acknowledgement: do not advance revision twice.
        if not p:
            return {"id": pid, "deleted": True, "revision": r.revision}
        if r.revision != base_revision:
            raise HTTPException(
                409, "Report alterado noutro dispositivo; atualize antes de remover"
            )
        changed = db.execute(
            update(Report)
            .where(
                Report.id == rid,
                Report.owner_id == u.id,
                Report.revision == base_revision,
                Report.status == "draft",
            )
            .values(revision=base_revision + 1, updated=now())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise HTTPException(409, "Conflito de revisão ao remover fotografia")
        db.delete(p)
        audit(db, u, "photo.delete", pid)
        db.commit()
        return {"id": pid, "deleted": True, "revision": base_revision + 1}

    # Return original bytes only after contract access and report/photo association checks.
    @app.get("/api/reports/{rid}/photos/{pid}")
    def get_photo(rid: str, pid: str, u=Depends(user_dep), db=Depends(db_dep)):
        report_access(db, u, rid)
        p = db.get(Photo, pid)
        if not p or p.report_id != rid:
            raise HTTPException(404, "Fotografia inexistente")
        return Response(p.data, media_type=p.mime)

    class ExportFileResponse(StreamingResponse):
        """Close the spool even if sending headers fails before iteration starts."""

        def __init__(self, source, size):
            self.source = source
            super().__init__(
                file_chunks(source),
                media_type="application/zip",
                headers={"Content-Length": str(size)},
            )

        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                self.source.close()

    def closed_export_report(db, user, rid):
        report_access(db, user, rid)
        # Closed reports are immutable. A shared row lock on PostgreSQL prevents
        # administrative deletion until the document/archive is fully built.
        report = db.scalar(
            select(Report)
            .where(Report.id == rid)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if not report:
            raise HTTPException(404, "Report inexistente")
        if report.status != "closed":
            raise HTTPException(409, "Conclua o report antes de exportar PDF ou ZIP.")
        return report

    # Metadata now, image bytes only while that photograph is rendered or archived:
    # one BLOB in memory at a time instead of every photograph of the report.
    class ExportPhoto:
        def __init__(self, db, row):
            self._db = db
            self.id = row.id
            self.checkpoint = row.checkpoint
            self.filename = row.filename
            self.mime = row.mime

        @property
        def data(self):
            return self._db.scalar(select(Photo.data).where(Photo.id == self.id))

    def export_photos(db, rid):
        return [
            ExportPhoto(db, row)
            for row in db.execute(
                select(Photo.id, Photo.checkpoint, Photo.filename, Photo.mime)
                .where(Photo.report_id == rid)
                .order_by(Photo.created, Photo.id)
            )
        ]

    # Render from the stored report snapshot and checklist, not today's imported charger details.
    @app.get("/api/reports/{rid}/pdf")
    def pdf(rid: str, _slot=Depends(export_admission), u=Depends(user_dep), db=Depends(db_dep)):
        from .pdf_report import build_pdf

        r = closed_export_report(db, u, rid)
        try:
            document = build_pdf(report_json(db, r), export_photos(db, rid))
        except MissingEvidence as error:
            raise HTTPException(409, str(error))
        finally:
            db.close()
        return Response(
            document,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="report-{rid}.pdf"'},
        )

    # ZIP contains PDF, report JSON and original image bytes arranged by checkpoint.
    @app.get("/api/reports/{rid}/bundle")
    def bundle(rid: str, _slot=Depends(export_admission), u=Depends(user_dep), db=Depends(db_dep)):
        from .pdf_report import build_pdf

        r = closed_export_report(db, u, rid)
        spool = SpooledTemporaryFile(max_size=16 * 1024 * 1024)
        try:
            doc = report_json(db, r)
            photos = export_photos(db, rid)
            size = write_bundle(spool, doc, photos, build_pdf(doc, photos))
        except MissingEvidence as error:
            spool.close()
            raise HTTPException(409, str(error))
        except BaseException:
            spool.close()
            raise
        finally:
            # The remaining response reads only the generated file. A slow client
            # must not retain a database connection or the report's shared lock.
            db.close()
        return ExportFileResponse(spool, size)

    return app
