# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

from .report_extras import validate_extras
from sqlalchemy.orm import joinedload, load_only
from .stations import (
    close_identity, edit_station, get_or_create_station, lock_catalogue,
    opening_identity, station_record, stations_for_import, StationConflict,
)
# FastAPI composition root: dependencies, authorization and transactional endpoints.
# All handlers are registered inside create_app so tests can supply an isolated DB.
# Read docs/architecture-and-api.md alongside this file; models.py owns the schema.
# Comments explain current behavior, including deletion and conflict exceptions.

import json, os, secrets, hashlib, uuid, threading, asyncio, logging
from contextlib import asynccontextmanager
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, update, delete, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from .models import (
    database,
    User,
    AccountAdminLock,
    SessionToken,
    Contract,
    ContractTrash,
    Assignment,
    Charger,
    set_contract_notes,
    DetachedCharger,
    attached_charger,
    Station,
    Report,
    ReportCorrection,
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
from .branding import selection, contract_logos, contract_presentation, save_presentation, ISSUERS
from .importer import analyse, parse
from .import_profiles import normalize_contract_notes
from .images import InvalidImage
from .media import inspect_evidence
from .checklist import for_charger, corrected_draft_template, corrected_draft_payload, connector_names
from .measurements import digits as measurement_digits, format_value, not_applicable, canonical, milliamp
from .maintenance_rules import apply_assessments, reading as maintenance_reading
from .maintenance_migration import upgrade_checkpoint, persist_upgrade
from .exports import write_bundle, file_chunks, MissingEvidence
from .report_naming import EVIDENCE_EXTENSIONS, content_disposition, evidence_stem, report_identifier
from .contract_trash import RETENTION, utc_now, purge_expired
from . import corrections

SESSION_LIFETIME = timedelta(days=365)
from .evidence_cache import EvidenceCache, EvidenceResponse

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


# Preserve already-closed offline v6 reports during an upgrade. They are history,
# unlike editable drafts/correction copies, and must retain their original checklist.
LEGACY_TEMPLATE = json.loads((Path(__file__).resolve().parents[2] / "shared/checklist-v6.json").read_text(encoding="utf-8"))

# Docker copies shared into server/shared; development uses sibling shared.
class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class NewUser(Login):
    name: str = Field(min_length=1, max_length=200)
    role: str = "tech"


class ResetTechnicianPassword(BaseModel):
    # Do not echo a password in a length-validation response or model repr.
    password: SecretStr


class OwnPasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr
    new_password: SecretStr
    confirmation: SecretStr


class StationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_updated: str = Field(min_length=1, max_length=50)
    fields: dict[str, str] = Field(min_length=1, max_length=256)


class ContractNotesEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: int = Field(ge=0, strict=True)
    notes: str = Field(max_length=32767)


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


class OpeningStationIdentity(BaseModel):
    manufacturer: str = Field(max_length=32767)
    model: str = Field(max_length=32767)
    serial: str = Field(max_length=32767)


class ReportInput(BaseModel):
    station_identity_at_start: OpeningStationIdentity | None = None
    # A v6 report already closed offline still uploads photos in a draft staging phase.
    frozen_history: bool = Field(default=False, strict=True)
    checklist_version: int = Field(ge=1, strict=True)
    load_test_connectors: list[str] | None = Field(default=None, max_length=64)
    charger_id: str
    base_revision: int = Field(ge=0)
    status: str = "draft"
    payload: dict


class CorrectionInput(BaseModel):
    id: str
    base_revision: int = Field(ge=1)


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
    # Missing/ill-typed fields can make Pydantic include the original input in
    # its errors. A credential form must never send that input back to a client.
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path == "/api/account/password":
            return JSONResponse({"detail": "Campos de password inválidos"}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    app.state.factory = Factory
    evidence_cache = EvidenceCache()
    app.state.evidence_cache = evidence_cache
    app.state.purge_expired_contracts = lambda instant=None: purge_expired(Factory, instant)
    bearer = HTTPBearer()
    app.add_middleware(RequestBodyLimit)
    limiter = LoginLimiter()
    password_change_limiter = LoginLimiter()
    # Use the same scrypt work for unknown users, avoiding the cheap early exit.
    dummy_password = hash_password(secrets.token_urlsafe(32))

    def resource_limit(name, default, maximum):
        try:
            return max(1, min(maximum, int(os.getenv(name, str(default)))))
        except ValueError:
            return default

    export_slots = threading.BoundedSemaphore(resource_limit("LEM_EXPORT_CONCURRENCY", 2, 4))
    image_slots = threading.BoundedSemaphore(resource_limit("LEM_IMAGE_CONCURRENCY", 1, 2))
    download_slots = threading.BoundedSemaphore(resource_limit("LEM_DOWNLOAD_CONCURRENCY", 8, 16))

    def admission(slots, operation, retry_after=15):
        # Fail fast BEFORE authentication opens a DB transaction. Waiting while
        # holding that connection can starve login/sync under concurrent exports.
        if not slots.acquire(blocking=False):
            raise HTTPException(
                503,
                f"Servidor ocupado: {operation}; tente novamente",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            yield
        finally:
            slots.release()

    def export_admission():
        yield from admission(export_slots, "exportação")

    def image_admission():
        yield from admission(image_slots, "validação de fotografias")

    def download_admission():
        yield from admission(download_slots, "transferência de evidências", 2)

    # One SQLAlchemy session per request; endpoint handlers explicitly commit accepted writes.
    def db_dep():
        with Factory() as db:
            yield db

    def begin_credential_write(db):
        # SQLite ignores FOR UPDATE. Take its writer reservation before a login
        # or password change reads the hash and decides which sessions may live.
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")

    def authenticated_user(request, auth, db, *, changing_password=False):
        if changing_password:
            begin_credential_write(db)
        db.info["client_platform"] = request.headers.get("X-Lem-Client-Platform", "")
        token = db.get(SessionToken, digest(auth.credentials))
        if not token or token.expires <= now():
            raise HTTPException(401, "Sessão expirada; entre novamente")
        # Take the required lock immediately. Two simultaneous changes must
        # never both hold a shared lock and then wait for each other to upgrade.
        lookup = select(User).where(User.id == token.user_id)
        lookup = lookup.with_for_update(key_share=True) if changing_password else lookup.with_for_update(read=True)
        user = db.scalar(lookup.execution_options(populate_existing=True))
        if not user or user.role not in ("admin", "tech"):
            raise HTTPException(401, "Utilizador inexistente")
        # If a credential change won while this request waited for the User lock,
        # the token read above may already have been revoked. Recheck afterwards.
        token = db.scalar(select(SessionToken).where(SessionToken.digest == token.digest)
                          .execution_options(populate_existing=True))
        if not token or token.expires <= now():
            raise HTTPException(401, "Sessão expirada; entre novamente")
        return user

    def user_dep(request: Request, auth: HTTPAuthorizationCredentials = Depends(bearer), db=Depends(db_dep)):
        return authenticated_user(request, auth, db)

    # Administrative access is not an implicit right to edit another owner's draft.
    def admin(user=Depends(user_dep)):
        if user.role != "admin":
            raise HTTPException(403, "Apenas administrador")
        return user

    def station_admin(user=Depends(admin), db=Depends(db_dep)):
        if db.info.get("client_platform") not in ("windows", "linux", "macos"):
            raise HTTPException(403, "A gestão dos IDs de PCVEs exige a app desktop")
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
                raise HTTPException(410, corrections.replaced_detail(db, rid))
            raise HTTPException(404, "Report inexistente")
        charger = db.get(Charger, r.charger_id)
        accessible(db, user, charger.contract_id)
        corrections.gate(db, user, rid)
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
        projection_status = "closed" if r.template.get("frozen_upload") else r.status
        correction = db.get(ReportCorrection, r.id)
        schema, payload = r.template, r.payload
        photos = photo_metadata(db, r.id)
        if (correction and correction.state == "editing" and r.status == "draft"
                and schema.get("version", 0) < 6):
            # Existing correction copies also receive the current form on GET;
            # the matching write projection persists under the report lock.
            from .legacy_correction import project
            schema, payload, mapping = project(schema, payload, photos, r.snapshot, TEMPLATE)
            photos = [dict(photo, checkpoint=mapping[photo["checkpoint"]]) for photo in photos]
        technician = db.get(User, correction.original_owner_id) if correction and correction.state == "editing" else owner
        charger = db.get(Charger, r.charger_id)
        contract_id = charger.contract_id
        # Return current station data on both commit and GET recovery after a lost
        # acknowledgement. Historical snapshots must never revert a newer model.
        station_update = ({"station_update": {"station_id": charger.station_id,
                           "external_id": charger.external_id, "details": charger.details,
                           "updated": charger.station.updated if charger.station is not None else None}}
                          if correction and correction.state == "committed" else {})
        return {
            "id": r.id,
            "contract_id": contract_id,
            "station_id": charger.station_id,
            "presentation": contract_presentation(db, contract_id),
            "identifier": report_identifier({"snapshot": r.snapshot}),
            "charger_id": r.charger_id,
            "owner_id": r.owner_id if owner and owner.role in ("admin", "tech") else "",
            "technician": technician.name if technician and technician.role in ("admin", "tech") else "Sem proprietário",
            **({"correction": corrections.metadata(correction)} if correction else {}),
            **station_update,
            "revision": r.revision,
            "status": r.status,
            "payload": corrected_draft_payload(payload, schema, projection_status),
            "snapshot": r.snapshot,
            "template": corrected_draft_template(schema, projection_status),
            "started": r.started,
            "updated": r.updated,
            "finished": r.finished,
            "photos": [dict(p, checkpoint=upgrade_checkpoint(p["checkpoint"]))
                       if projection_status == "draft" and schema.get("version") == 6 else p
                       for p in photos],
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
    def login(body: Login, request: Request, response: Response, db=Depends(db_dep)):
        email = body.email.strip().lower()
        limiter.consume(request.client.host if request.client else "", email)
        begin_credential_write(db)
        # Console revocation takes this guard exclusively. Order it before the
        # User lock so an issued token lands wholly before or after revocation.
        db.scalar(select(AccountAdminLock.id).where(AccountAdminLock.id == 1)
                  .with_for_update(read=True))
        user = db.scalar(select(User).where(User.email == email).with_for_update(read=True))
        valid = verify(body.password, user.password if user else dummy_password)
        if not user or user.role not in ("admin", "tech") or not valid:
            raise HTTPException(401, "Credenciais inválidas")
        token = secrets.token_urlsafe(32)
        issued = datetime.now(timezone.utc)
        db.execute(delete(SessionToken).where(SessionToken.expires <= issued.isoformat()))
        db.add(
            SessionToken(
                digest=digest(token),
                user_id=user.id,
                expires=(issued + SESSION_LIFETIME).isoformat(),
            )
        )
        db.commit()
        response.headers["Cache-Control"] = "no-store"
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

    @app.put("/api/account/password")
    def change_own_password(body: OwnPasswordChange, request: Request,
                            auth: HTTPAuthorizationCredentials = Depends(bearer), db=Depends(db_dep)):
        user = authenticated_user(request, auth, db, changing_password=True)
        current = body.current_password.get_secret_value()
        new = body.new_password.get_secret_value()
        if not 1 <= len(current) <= 200 or not 12 <= len(new) <= 200:
            raise HTTPException(422, "Verifique o comprimento das passwords")
        if new != body.confirmation.get_secret_value():
            raise HTTPException(422, "As passwords não coincidem")
        password_change_limiter.consume(request.client.host if request.client else "", user.email)
        if not verify(current, user.password):
            # A wrong current password does not invalidate an authenticated session.
            raise HTTPException(403, "Password atual incorreta")
        user.password = hash_password(new)
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id,
                                              SessionToken.digest != digest(auth.credentials)))
        audit(db, user, "user.password.change", user.id)
        db.commit()
        return {"ok": True, "user_id": user.id}

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

    @app.put("/api/users/{user_id}/password")
    def reset_technician_password(user_id: str, body: ResetTechnicianPassword,
                                  u=Depends(admin), db=Depends(db_dep)):
        password = body.password.get_secret_value()
        if not 12 <= len(password) <= 200:
            raise HTTPException(422, "Password entre 12 e 200 caracteres")
        encoded = hash_password(password)
        # Filter BEFORE locking: administrators cannot target each other and
        # deadlock while each request already holds its caller's shared User lock.
        # NO KEY UPDATE also serializes against login, console changes and resets.
        technician = db.scalar(select(User).where(User.id == user_id, User.role == "tech")
                               .with_for_update(key_share=True)
                               .execution_options(populate_existing=True))
        if technician is None:
            if db.get(User, user_id) is None:
                raise HTTPException(404, "Técnico inexistente")
            raise HTTPException(403, "Só é possível repor passwords de técnicos")
        technician.password = encoded
        db.execute(delete(SessionToken).where(SessionToken.user_id == technician.id))
        audit(db, u, "user.password.reset", technician.id)
        db.commit()
        return {"ok": True, "user_id": technician.id}

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
                "report_logos": contract_logos(db, c.id),
                "presentation": contract_presentation(db, c.id),
            }
            for c in db.scalars(query)
        ]

    class ReportPresentationInput(BaseModel):
        issuer: str
        client_logos: list[str] = Field(default_factory=list, max_length=2)
        base_revision: int = Field(ge=0)

    @app.get("/api/contracts/{cid}/presentation")
    def current_presentation(cid: str, response: Response, u=Depends(user_dep), db=Depends(db_dep)):
        accessible(db, u, cid)
        response.headers["Cache-Control"] = "no-store"
        return contract_presentation(db, cid)

    @app.put("/api/contracts/{cid}/presentation")
    def update_presentation(cid: str, body: ReportPresentationInput, u=Depends(admin), db=Depends(db_dep)):
        accessible(db, u, cid, write=True)
        if any(logo in ISSUERS for logo in body.client_logos):
            raise HTTPException(422, "Os logótipos do cliente são adicionais à entidade do report")
        result = save_presentation(db, cid, body.issuer, body.client_logos, body.base_revision)
        audit(db, u, "contract.presentation", cid)
        db.commit()
        return result

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
            select(Report.charger_id, Report.id, Report.status, Report.owner_id, Report.updated, Report.revision,
                   Report.started, Report.finished, ReportCorrection.state.label("correction_state"),
                   ReportCorrection.editor_id.label("correction_editor"),
                   Report.snapshot["charger"]["external_id"].as_string().label("report_charger"),
                   Report.snapshot["contract"]["name"].as_string().label("report_contract"))
            .join(Charger, Charger.id == Report.charger_id)
            .outerjoin(ReportCorrection, ReportCorrection.id == Report.id)
            .where(Charger.contract_id == cid)
            .order_by(Report.updated.desc())
        )
        for row in rows:
            if row.correction_state == "editing" and (u.role != "admin" or row.correction_editor != u.id or
                               db.info.get("client_platform") not in ("windows", "linux")):
                continue
            summaries.setdefault(row.charger_id, []).append(
                {
                    "id": row.id,
                    "identifier": report_identifier({
                        "snapshot": {"charger": {"external_id": row.report_charger},
                                     "contract": {"name": row.report_contract}}}),
                    "status": row.status,
                    "owner_id": row.owner_id,
                    "updated": row.updated,
                    "started": row.started,
                    "finished": row.finished,
                    "revision": row.revision,
                }
            )
        removed = {}
        replacements = {}
        for row in db.execute(
            select(DeletedReport.charger_id, DeletedReport.id, ReportCorrection.id.label("replacement_id"))
            .select_from(DeletedReport)
            .join(Charger, Charger.id == DeletedReport.charger_id)
            .outerjoin(ReportCorrection, (ReportCorrection.source_id == DeletedReport.id) &
                       (ReportCorrection.state == "committed"))
            .where(Charger.contract_id == cid)
        ):
            removed.setdefault(row.charger_id, []).append(row.id)
            if row.replacement_id:
                replacements[row.id] = row.replacement_id
        return [
            {
                "id": c.id,
                "contract_id": cid,
                "station_id": c.station_id,
                "station_updated": c.station.updated if c.station is not None else None,
                "external_id": c.external_id,
                "details": c.details,
                **note_fields(c),
                "reports": summaries.get(c.id, []),
                "deleted_reports": removed.get(c.id, []),
                "report_replacements": {rid: replacements[rid] for rid in removed.get(c.id, []) if rid in replacements},
            }
            for c in db.scalars(
                select(Charger).options(joinedload(Charger.station), joinedload(Charger.contract_note))
                .where(Charger.contract_id == cid, attached_charger()).order_by(Charger.external_id)
            )
        ]

    def note_fields(charger):
        note = charger.contract_note
        return {"contract_notes": note.text if note else "", "notes_revision": note.revision if note else 0}

    def membership_notes(db, cid, charger_id):
        charger = db.get(Charger, charger_id)
        if charger is None or charger.contract_id != cid:
            raise HTTPException(404, "PCVE inexistente neste contrato")
        attached = db.get(DetachedCharger, charger_id) is None
        return charger, attached

    def notes_response(charger, attached):
        return {"id": charger.id, "contract_id": charger.contract_id, "external_id": charger.external_id,
                "attached": attached, **note_fields(charger)}

    @app.get("/api/contracts/{cid}/chargers/{charger_id}/notes")
    def read_notes(cid: str, charger_id: str, u=Depends(user_dep), db=Depends(db_dep)):
        accessible(db, u, cid)
        return notes_response(*membership_notes(db, cid, charger_id))

    @app.put("/api/contracts/{cid}/chargers/{charger_id}/notes")
    def save_notes(cid: str, charger_id: str, body: ContractNotesEdit, u=Depends(admin), db=Depends(db_dep)):
        accessible(db, u, cid, write=True)
        # Imports and detach use this same lock, including on SQLite.
        lock_catalogue(db)
        charger, attached = membership_notes(db, cid, charger_id)
        if not attached:
            raise HTTPException(409, "Este ID foi retirado do contrato")
        try:
            value = normalize_contract_notes(body.notes)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        current = note_fields(charger)
        if current["contract_notes"] != value:
            if current["notes_revision"] != body.base_revision:
                raise HTTPException(409, "As notas foram alteradas. Consulte a versão atual antes de guardar.")
            set_contract_notes(charger, value)
            audit(db, u, "contract.notes", charger_id)
        result = notes_response(charger, attached)
        db.commit()
        return result

    def visible_stations(u):
        query = select(Station).order_by(Station.external_id)
        if u.role != "admin":
            # EXISTS gives one row per physical station even when multiple
            # active, assigned contracts include that station.
            assigned = (select(Charger.id)
                        .join(Assignment, Assignment.contract_id == Charger.contract_id)
                        .where(Charger.station_id == Station.id, Assignment.user_id == u.id, attached_charger(),
                               ~select(ContractTrash.contract_id)
                               .where(ContractTrash.contract_id == Charger.contract_id).exists())
                        .exists())
            query = query.where(assigned)
        return query

    @app.get("/api/stations")
    def station_catalogue(u=Depends(user_dep), db=Depends(db_dep)):
        return [station_record(station) for station in db.scalars(visible_stations(u))]

    @app.delete("/api/contracts/{cid}/chargers/{charger_id}")
    def detach_charger(cid: str, charger_id: str, u=Depends(admin), db=Depends(db_dep)):
        accessible(db, u, cid, write=True)
        lock_catalogue(db)
        charger = db.scalar(select(Charger).where(Charger.id == charger_id, Charger.contract_id == cid)
                            .with_for_update())
        if charger is None:
            raise HTTPException(404, "PCVE inexistente neste contrato")
        if not db.get(DetachedCharger, charger_id):
            db.add(DetachedCharger(charger_id=charger_id))
            audit(db, u, "contract.detach-pcve", charger_id)
        db.commit()
        return {"ok": True}

    @app.get("/api/stations/{sid}/reports")
    def station_reports(sid: str, u=Depends(user_dep), db=Depends(db_dep)):
        if db.scalar(visible_stations(u).where(Station.id == sid)) is None:
            raise HTTPException(404, "PCVE inexistente")
        # Historical memberships remain valid for report history. Contracts in
        # the trash and other users' correction drafts follow the normal gates.
        query = (select(Report.id, Report.charger_id, Report.status, Report.started, Report.finished, Contract.name.label("contract"),
                        Contract.id.label("contract_id"),
                        Report.snapshot["charger"]["external_id"].as_string().label("pcve"),
                        Report.snapshot["contract"]["name"].as_string().label("original_contract"))
                 .join(Charger, Charger.id == Report.charger_id)
                 .join(Contract, Contract.id == Charger.contract_id)
                 .outerjoin(ReportCorrection, ReportCorrection.id == Report.id)
                 .where(Charger.station_id == sid, ~select(ContractTrash.contract_id)
                        .where(ContractTrash.contract_id == Contract.id).exists())
                 .order_by(Report.started.desc(), Report.id))
        if u.role != "admin":
            query = query.join(Assignment, Assignment.contract_id == Contract.id).where(Assignment.user_id == u.id)
        allow_corrections = u.role == "admin" and db.info.get("client_platform") in ("windows", "linux")
        visible = (ReportCorrection.id.is_(None) | (ReportCorrection.state != "editing"))
        if allow_corrections:
            visible = visible | (ReportCorrection.editor_id == u.id)
        return [{"id": row.id, "charger_id": row.charger_id, "status": row.status, "contract": row.contract,
                 "contract_id": row.contract_id, "started": row.started, "finished": row.finished,
                 "identifier": report_identifier({"snapshot": {"charger": {"external_id": row.pcve},
                                                              "contract": {"name": row.original_contract}}})}
                for row in db.execute(query.where(visible))]

    @app.patch("/api/stations/{sid}")
    def save_station(sid: str, body: StationEdit, u=Depends(station_admin), db=Depends(db_dep)):
        lock_catalogue(db)
        station = db.get(Station, sid)
        if station is None:
            raise HTTPException(404, "ID de PCVE inexistente")
        try:
            if edit_station(station, body.base_updated, body.fields, u.id):
                audit(db, u, "stations.edit", sid)
        except StationConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        result = station_record(station)
        db.commit()
        return result

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
            raise HTTPException(413, f"Máximo {limit // (1024 * 1024)} MiB por ficheiro")
        return data

    @app.post("/api/imports/analyse")
    def import_analyse(file: UploadFile = File(), u=Depends(admin)):
        return analyse(file_bytes(file))

    def import_mapping(raw):
        try:
            config = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "Mapeamento JSON inválido") from None
        if not isinstance(config, dict):
            raise HTTPException(422, "Mapeamento deve ser objeto")
        return config

    def import_preview(result):
        return {"count": result["count"], "errors": result["errors"],
                "sample": result["records"][:10], "records": result["records"],
                "profile": result.get("profile")}

    def import_records(result):
        if result["errors"]:
            raise HTTPException(422, {
                "message": "Importação bloqueada; corrija os IDs", "errors": result["errors"],
            })
        return result["records"]

    @app.post("/api/stations/import")
    def import_stations(file: UploadFile = File(), mapping: str = Form(), commit: bool = Form(False),
                        u=Depends(station_admin), db=Depends(db_dep)):
        data = file_bytes(file)
        result = parse(data, import_mapping(mapping), contract_notes=False)
        if not commit:
            return import_preview(result)
        records = import_records(result)
        if not records:
            raise HTTPException(422, "O Excel não contém IDs de PCVEs")
        lock_catalogue(db)
        existing = stations_for_import(db, records)
        updated = len(existing)
        for record in records:
            get_or_create_station(db, record["external_id"], record["details"], existing)
        audit(db, u, "stations.import", hashlib.sha256(data).hexdigest())
        db.commit()
        return {"created": len(records) - updated, "updated": updated}

    # A contract import writes canonical station data and its memberships in
    # one transaction. Its counters still refer to memberships for older apps.
    @app.post("/api/contracts/{cid}/import")
    def import_file(
        cid: str,
        file: UploadFile = File(),
        mapping: str = Form(),
        commit: bool = Form(False),
        u=Depends(admin),
        db=Depends(db_dep),
    ):
        config = import_mapping(mapping)
        logos = selection(config["report_logos"]) if "report_logos" in config else None
        issuer = config.get("report_issuer")
        if issuer is not None and issuer not in ISSUERS:
            raise HTTPException(422, "Escolha Lem ou CSMTEC como entidade do report")
        branding_revision = config.get("branding_revision")
        if branding_revision is not None and (type(branding_revision) is not int or branding_revision < 0):
            raise HTTPException(422, "Revisão do formato inválida")
        data = file_bytes(file)
        result = parse(data, config)
        # Parsing does not need a contract lock: keep ordinary field requests
        # moving while a large workbook is decoded. Recheck access/retention
        # afterwards, then acquire the final lock directly (never upgrade a
        # shared lock). Only the database commit phase serializes writers.
        contract = accessible(db, u, cid, write=commit)
        if not commit:
            return import_preview(result)
        records = import_records(result)
        if logos is not None or issuer is not None:
            current_style = contract_presentation(db, cid)
            save_presentation(db, cid, issuer or current_style["issuer"],
                [logo["id"] for logo in (logos if logos is not None else current_style["client_logos"])],
                branding_revision)
        created = changed = 0
        lock_catalogue(db)
        existing = {
            c.external_id: c for c in db.scalars(select(Charger).options(joinedload(Charger.contract_note))
                                                  .where(Charger.contract_id == cid))
        }
        stations = stations_for_import(db, records)
        for rec in records:
            station = get_or_create_station(db, rec["external_id"], rec["details"], stations)
            c = existing.get(rec["external_id"])
            if c:
                db.execute(delete(DetachedCharger).where(DetachedCharger.charger_id == c.id))
                c.station = station
                changed += 1
            else:
                c = Charger(id=uid(), contract_id=cid, external_id=rec["external_id"], station=station)
                db.add(c)
                existing[c.external_id] = c
                created += 1
            if "contract_notes" in rec:
                set_contract_notes(c, rec["contract_notes"])
        # Companies can span several contracts. A per-contract lock cannot
        # protect their shared profile key: use one atomic upsert instead of
        # SELECT-then-INSERT. The last committed mapping remains the default.
        insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        profile_mapping = {k: v for k, v in config.items() if k not in ("report_logos", "report_issuer", "branding_revision")}
        profile = insert(ImportProfile).values(company=contract.company, mapping=profile_mapping)
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

    def validate_report_payload(db, r, body):
        # Both ordinary closure and direct correction commits must enforce current
        # draft rules. A copied historical template must not bypass photo checks.
        legacy_closure = body.checklist_version == 6 and (body.frozen_history or body.status == "closed") and r.template.get("version") == 6
        if body.frozen_history:
            if not legacy_closure or not body.payload.get("started") or not body.payload.get("finished"):
                raise HTTPException(422, "O envio histórico exige um report v6 já fechado, com início e fim")
            corrections.checked_times(body.payload)
        if r.template.get("frozen_upload") and not legacy_closure:
            if body.status != "draft" or body.checklist_version != TEMPLATE["version"]:
                raise HTTPException(409, "Este report antigo está a terminar a sincronização do fecho")
            # Owner/revision were checked under the write lock by put_report.
            # Explicitly reopening as a current draft abandons a rejected old
            # closure; remove the transfer marker and migrate in this transaction.
            r.template = {key: value for key, value in r.template.items() if key != "frozen_upload"}
        if legacy_closure:
            frozen = dict(r.template)
            if body.status == "draft": frozen["frozen_upload"] = True
            else: frozen.pop("frozen_upload", None)
            r.template = corrected_draft_template(frozen, r.status, upgrade=False)
        else:
            persist_upgrade(db, r, TEMPLATE)
            r.template = corrected_draft_template(r.template, r.status)
        if body.checklist_version != r.template.get("version"):
            raise HTTPException(409, "A checklist mudou. Atualize a app e o report; a cópia local foi conservada")
        # Validate the projected text, not only the shorter inputs from which
        # migration may have merged it. GET and PUT must return the same payload.
        body.payload = corrected_draft_payload(body.payload, r.template, r.status, upgrade=False)
        if len(json.dumps(body.payload)) > 300000:
            raise HTTPException(413, "Report demasiado grande")
        items = {i["id"]: i for s in r.template["sections"] for i in s["items"]}
        answers = body.payload.get("answers", {})
        if not isinstance(answers, dict) or any(k not in items for k in answers):
            raise HTTPException(422, "Resposta/checkpoint inválido")
        # Compatibility with clients that inserted null for untouched optional fields.
        # Required answers and non-null invalid data still undergo strict validation.
        answers = {key: a for key, a in answers.items()
                   if a is not None or not items[key].get("optional")}
        if "answers" in body.payload:
            body.payload["answers"] = answers
        for key, a in answers.items():
            if (
                not isinstance(a, dict)
                or not isinstance(a.get("value", ""), str)
                or not isinstance(a.get("notes", ""), str)
            ):
                raise HTTPException(422, key + ": resposta deve ter texto e observações")
            # General notes can merge several formerly independent 8000-character
            # fields (up to 24002 in the v6 migration). Keep a bounded allowance
            # for that complete text, plus the unchanged total payload limit.
            notes_limit = 32000 if items[key]["kind"] == "notes" else 8000
            if len(a.get("value", "")) > 8000 or len(a.get("notes", "")) > notes_limit:
                raise HTTPException(422, "Texto demasiado longo")
            if "assessment" in a and (not items[key].get("reading_required") or a["assessment"] not in ("OK", "NOK")):
                raise HTTPException(422, "Avaliação inválida")
            if items[key]["kind"] == "status" and a.get("value", "") not in ["", "OK", "NOK", "NA"]:
                raise HTTPException(422, "Estado deve ser OK, NOK ou NA")
        answers = body.payload.get("answers", {})
        if r.template.get("version", 0) >= 7:
            # Recompute independently of the client; never silently refill a note
            # the technician cleared to enter their own observation.
            answers = apply_assessments(answers)
            body.payload["answers"] = answers
        try:
            validate_extras(body.payload, items)
        except ValueError as error:
            raise HTTPException(422, str(error))
        if body.status == "closed":
            # Drafts retain in-progress typing (e.g. a trailing decimal separator).
            # A closed report must contain complete, formatted measurements.
            for key, answer in answers.items():
                measurement_key = canonical(key) if r.template.get("version", 0) < 7 and milliamp(key) else key
                if measurement_digits(measurement_key):
                    try:
                        answer["value"] = format_value(measurement_key, answer.get("value", ""))
                    except ValueError as error:
                        raise HTTPException(422, str(error))
            photos = photo_metadata(db, r.id)
            try:
                validate_extras(body.payload, items, photos)
            except ValueError as error:
                raise HTTPException(422, str(error))
            errors = []
            if r.template.get("version", 0) >= 7 and maintenance_reading(answers, "MAN.11") == 0:
                errors.append("MAN.11: o calibre configurado deve ser superior a zero")
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
        # including contract membership changes.
        if body.status == "closed":
            lock_catalogue(db)
        # Always lock the contract before its charger, matching trash/purge.
        charger = db.scalar(select(Charger).where(Charger.id == body.charger_id).with_for_update())
        if db.get(DeletedReport, rid):
            raise HTTPException(410, corrections.replaced_detail(db, rid))
        # Serialize answer/closure validation with photo writes and ownership changes.
        r = db.scalar(select(Report).where(Report.id == rid).with_for_update()
                      .execution_options(populate_existing=True))
        creating = r is None
        if r:
            # Authorize and check the revision before migrating. Validation below
            # atomically upgrades editable v6 data and checks the resulting schema.
            report_access(db, u, rid, write=True)
            if db.get(ReportCorrection, rid) and body.status == "closed":
                raise HTTPException(409, "Guarde a correção pela operação de substituição do original")
            if r.charger_id != charger.id:
                raise HTTPException(409, "Não pode alterar carregador do report")
            if r.revision != body.base_revision:
                raise HTTPException(
                    409, {"message": "Report alterado noutro dispositivo", "revision": r.revision}
                )
            if r.status == "closed":
                raise HTTPException(409, "Report fechado é imutável; crie nova intervenção")
        else:
            if db.get(DetachedCharger, charger.id):
                raise HTTPException(409, "Este PCVE foi retirado do contrato. O rascunho local foi conservado.")
            legacy_closed = body.checklist_version == 6 and (body.status == "closed" or body.frozen_history)
            if body.checklist_version != TEMPLATE["version"] and not legacy_closed:
                raise HTTPException(409, "A checklist mudou. Crie um novo report; o rascunho antigo foi conservado para consulta")
            if body.base_revision != 0:
                raise HTTPException(409, "Report inexistente com revisão diferente de zero")
            try:
                checklist = for_charger(LEGACY_TEMPLATE if legacy_closed else TEMPLATE, charger.details)
                # Older compatible clients still send the former Type 2 name.
                # Compare physical connector order/count after alias normalization.
                supplied_connectors = (connector_names(body.load_test_connectors)
                                       if body.load_test_connectors is not None else None)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            if (
                body.load_test_connectors is not None
                and supplied_connectors != checklist["load_test_connectors"]
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
                        "report_logos": contract_logos(db, contract.id),
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
        validate_report_payload(db, r, body)
        if creating and r.template.get("version", 0) >= 4:
            # Freeze the identity once. Subsequent edits and retried uploads cannot
            # replace the reference used to detect a concurrent catalogue change.
            opening = opening_identity(r.snapshot["charger"], body.station_identity_at_start,
                                       body.payload.get("answers", {}))
            r.snapshot = {**r.snapshot, "charger": opening}
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
                                     "details": charger.details,
                                     "updated": charger.station.updated if charger.station is not None else None}
        return out

    @app.post("/api/reports/{rid}/corrections")
    def begin_correction(rid: str, body: CorrectionInput, u=Depends(admin), db=Depends(db_dep)):
        corrections.desktop_admin(db, u)
        corrections.lock_report_users(db, rid)
        source = report_access(db, u, rid)
        report = corrections.begin(db, u, source, body.id, body.base_revision, TEMPLATE)
        audit(db, u, "report.correction.begin", report.id)
        db.commit()
        return report_json(db, report)

    @app.post("/api/reports/{rid}/correction/commit")
    def commit_correction(rid: str, body: ReportInput, u=Depends(admin), db=Depends(db_dep)):
        corrections.desktop_admin(db, u)
        corrections.lock_report_users(db, rid)
        report = report_access(db, u, rid)
        report = corrections.commit(db, u, report, body, validate_report_payload, TEMPLATE)
        audit(db, u, "report.correction.commit", report.id)
        db.commit()
        return report_json(db, report)

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
        if db.get(ReportCorrection, rid):
            raise HTTPException(409, "Uma cópia de correção não pode ser transferida")
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
    def delete_report(rid: str, base_revision: int, correction_only: bool = False, u=Depends(user_dep), db=Depends(db_dep)):
        uuid_check(rid)
        corrections.lock_report_users(db, rid)
        r = db.get(Report, rid) or db.get(DeletedReport, rid)
        if not r:
            raise HTTPException(404, "Report inexistente")
        # Contract -> charger -> report matches retention and console mutations.
        charger = db.get(Charger, r.charger_id)
        accessible(db, u, charger.contract_id)
        charger = corrections.lock_charger(db, r.charger_id)
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
        correction = corrections.gate(db, u, rid)
        if correction_only and (not correction or correction.state != "editing"):
            raise HTTPException(409, "A correção já foi concluída; o report final foi conservado")
        if db.scalar(select(ReportCorrection.id).where(ReportCorrection.source_id == rid,
                ReportCorrection.state == "editing").limit(1)):
            raise HTTPException(409, "Existe uma correção em curso; cancele-a antes de apagar o original")
        if correction and correction.state == "editing":
            correction.state = "cancelled"
        if r.revision != base_revision:
            raise HTTPException(
                409, "Report alterado noutro dispositivo; sincronize ou atualize antes de apagar"
            )
        claimed = db.execute(update(Report).where(Report.id == rid, Report.revision == base_revision,
            Report.owner_id == r.owner_id, Report.status == r.status).values(revision=base_revision+1)
            .execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise HTTPException(409, "Report alterado durante a remoção; dados conservados")
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
        # GET can expose the upgraded editable form before the next answers PUT.
        # Accept those checkpoints for independent media uploads too, under the
        # same write lock/transaction. Invalid uploads roll the migration back.
        if r.status == "draft" and r.revision == base_revision:
            persist_upgrade(db, r, TEMPLATE)
        if checkpoint not in {i["id"] for s in r.template["sections"] for i in s["items"]}:
            raise HTTPException(422, "Checkpoint inválido")
        data = file_bytes(file, limit=60 * 1024 * 1024)
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
            db.commit()  # Also retain any migration needed for this exact retry.
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
        if count >= 200 or total + len(data) > 400 * 1024 * 1024:
            raise HTTPException(413, "Máximo 200 evidências e 400 MiB por report")
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

    # A current-base no-op is idempotent; a real removal advances revision once.
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
        # Removing an attachment (including an inherited correction copy) needs
        # only its identity and owning report. Keep its binary in the database.
        p = db.get(Photo, pid, options=[load_only(Photo.report_id)])
        if p and p.report_id != rid:
            raise HTTPException(404, "Fotografia inexistente neste report")
        if r.revision != base_revision:
            raise HTTPException(
                409, "Report alterado noutro dispositivo; atualize antes de remover"
            )
        # Even a no-op must respect the revision check. An absent photo cannot
        # acknowledge newer report answers the client has never received.
        if not p:
            return {"id": pid, "deleted": True, "revision": base_revision}
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
    def get_photo(rid: str, pid: str, request: Request, _slot=Depends(download_admission), u=Depends(user_dep), db=Depends(db_dep)):
        from .downloads import byte_range
        try:
            report = report_access(db, u, rid)
            p = db.execute(select(Photo.mime, Photo.digest, func.length(Photo.data).label("size"))
                .where(Photo.id == pid, Photo.report_id == rid)).first()
            if not p:
                raise HTTPException(404, "Fotografia inexistente")
            etag = '"' + p.digest + '"'
            span = byte_range(request.headers.get("range"), p.size, request.headers.get("if-range"), etag)
            headers = {"Accept-Ranges": "bytes", "ETag": etag, "Cache-Control": "private, no-transform"}
            if extension := EVIDENCE_EXTENSIONS.get(p.mime):
                identifier = report_identifier({"snapshot": report.snapshot})
                headers["Content-Disposition"] = content_disposition(
                    evidence_stem(identifier, pid, extension), extension, inline=True)
            start, end = 0, p.size - 1
            if span:
                start, end = span
                headers["Content-Range"] = f"bytes {start}-{end}/{p.size}"
            db.close()
            def load():
                with Factory() as source_db:
                    return source_db.scalar(select(Photo.data).where(Photo.id == pid, Photo.report_id == rid))
            try:
                lease = evidence_cache.acquire((rid, pid, p.digest, p.size), p.size, load)
            except FileNotFoundError:
                raise HTTPException(404, "Fotografia inexistente") from None
            return EvidenceResponse(lease, start, end-start+1, p.mime, headers, 206 if span else 200)
        finally:
            # Sending bytes over a slow connection must not occupy the DB pool.
            db.close()

    class ExportFileResponse(StreamingResponse):
        """Close the spool even if sending headers fails before iteration starts."""

        def __init__(self, source, size, identifier):
            self.source = source
            super().__init__(
                file_chunks(source),
                media_type="application/zip",
                headers={"Content-Length": str(size),
                         "Content-Disposition": content_disposition(identifier, "zip")},
            )

        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                self.source.close()

    def closed_export_report(db, user, rid):
        report_access(db, user, rid)
        # Closed reports are immutable. A shared row lock on PostgreSQL prevents
        # administrative deletion until metadata and original bytes are staged.
        # PDF rendering/ZIP assembly use that detached snapshot, without row locks.
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

    # Stage originals one BLOB at a time, then retain only metadata/private paths
    # while rendering. Never keep every photograph of the report in memory.
    class ExportPhoto:
        def __init__(self, db, row, cache=None):
            self._db = db
            self.id = row.id
            self.checkpoint = row.checkpoint
            self.filename = row.filename
            self.mime = row.mime
            self._cached_path = Path(cache) / hashlib.sha256(self.id.encode()).hexdigest() if cache else None

        @property
        def source_path(self):
            if self._cached_path is not None and not self._cached_path.exists():
                if self._db is None:
                    # Never reopen a closed Session after releasing the snapshot
                    # locks: the original may already have been replaced/deleted.
                    raise MissingEvidence("Evidência temporária em falta durante a exportação; tente novamente")
                data = self._db.scalar(select(Photo.data).where(Photo.id == self.id))
                if data is None:
                    raise MissingEvidence("Fotografia em falta durante a exportação; tente novamente")
                self._cached_path.write_bytes(data)
            return self._cached_path

        @property
        def data(self):
            if self._cached_path is not None:
                return self.source_path.read_bytes()
            return self._db.scalar(select(Photo.data).where(Photo.id == self.id))

        def detach(self, *, stage_original):
            # A PDF lists videos by metadata. Only stage their bytes for ZIP.
            # Materialize required originals while the source is still locked.
            if stage_original:
                self.source_path
            self._db = None

    def export_photos(db, rid, cache=None):
        return [
            ExportPhoto(db, row, cache)
            for row in db.execute(
                select(Photo.id, Photo.checkpoint, Photo.filename, Photo.mime)
                .where(Photo.report_id == rid)
                .order_by(Photo.created, Photo.id)
            )
        ]

    def export_snapshot(db, user, rid, cache, *, include_video_bytes):
        """Freeze one consistent export, then release DB locks before rendering."""
        try:
            report = closed_export_report(db, user, rid)
            document = report_json(db, report)
            photos = export_photos(db, rid, cache)
            for photo in photos:
                photo.detach(stage_original=include_video_bytes or not photo.mime.startswith("video/"))
            return document, photos
        finally:
            # Corrections, branding changes and account operations must not wait
            # for the renderer, ZIP assembly or a slow device receiving bytes.
            db.close()

    # Render from the stored report snapshot and checklist, not today's imported charger details.
    @app.get("/api/reports/{rid}/pdf")
    def pdf(rid: str, _slot=Depends(export_admission), u=Depends(user_dep), db=Depends(db_dep)):
        from .pdf_report import build_pdf

        try:
            with TemporaryDirectory(prefix="lem-pdf-") as cache:
                doc, photos = export_snapshot(db, u, rid, cache, include_video_bytes=False)
                document = build_pdf(doc, photos)
        except MissingEvidence as error:
            raise HTTPException(409, str(error))
        finally:
            db.close()
        return Response(
            document,
            media_type="application/pdf",
            headers={"Content-Disposition": content_disposition(doc["identifier"], "pdf")},
        )

    # ZIP contains PDF, report JSON and original image bytes arranged by checkpoint.
    @app.get("/api/reports/{rid}/bundle")
    def bundle(rid: str, _slot=Depends(export_admission), u=Depends(user_dep), db=Depends(db_dep)):
        from .pdf_report import build_pdf

        spool = SpooledTemporaryFile(max_size=16 * 1024 * 1024)
        try:
            # Fetch each original from the DB once; the PDF worker and ZIP reuse
            # the detached snapshot even if a correction replaces the source.
            with TemporaryDirectory(prefix="lem-bundle-") as cache:
                doc, photos = export_snapshot(db, u, rid, cache, include_video_bytes=True)
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
        return ExportFileResponse(spool, size, doc["identifier"])

    return app
