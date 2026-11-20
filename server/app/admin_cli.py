# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Local server administration. OS/Docker access is the authorization boundary.

Passwords are never command-line arguments or included in account listings.
Deleted accounts retain an anonymized UUID for foreign keys/audit history;
their reports are presented as unowned until assigned to an active account.
"""

import argparse
import getpass
import json
import secrets
import shlex
import sys
import uuid
import warnings
import math
from pathlib import Path
from sqlalchemy import select, delete, update, func
from sqlalchemy.exc import IntegrityError
from .stations import lock_catalogue, get_or_create_station
from .models import DetachedCharger, attached_charger
from .import_profiles import clean_station_details, is_contract_notes, obsolete_station_field
from .models import (
    database,
    User,
    AccountAdminLock,
    SessionToken,
    Contract,
    ContractTrash,
    Assignment,
    Charger,
    Report,
    ReportCorrection,
    DeletedReport,
    OwnershipTransfer,
    Photo,
    Audit,
    now,
)
from .security import hash_password
from .contract_trash import RETENTION, utc_now, purge_contract
from .checklist import for_charger
from .corrections import lock_charger, lock_report_users

ACTIVE = ("admin", "tech")


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def need(db, cls, key):
    item = db.get(cls, key)
    if item is None:
        raise ValueError(f"{cls.__name__}: ID inexistente: {key}")
    return item


def account_write_guard(db):
    db.scalar(select(AccountAdminLock).where(AccountAdminLock.id == 1).with_for_update())


def account(db, key, active=True, lock=None):
    if lock == "write":
        account_write_guard(db)
    query = select(User).where((User.id == key) | (User.email == key.strip().lower()))
    if lock:
        # NO KEY UPDATE protects role/credential state while allowing audit/FK
        # KEY SHARE checks during report cleanup. Never lock every admin row.
        query = query.with_for_update(read=lock == "read", key_share=lock == "write")
    user = db.scalar(query.execution_options(populate_existing=True))
    if not user or (active and user.role not in ACTIVE):
        raise ValueError("Conta ativa inexistente: " + key)
    return user


def public_user(db, u):
    # Explicit projection: never serialize User.__dict__ or password hashes.
    return dict(
        id=u.id,
        name=u.name,
        email=u.email,
        role=u.role,
        reports=db.scalar(select(func.count(Report.id)).where(Report.owner_id == u.id)),
        contracts=list(
            db.scalars(select(Assignment.contract_id).where(Assignment.user_id == u.id))
        ),
    )


def active_contract(db, cid, allow_trash=False, write=True):
    c = db.scalar(select(Contract).where(Contract.id == cid).with_for_update(read=not write))
    if c is None:
        raise ValueError("Contrato inexistente")
    if not allow_trash and db.get(ContractTrash, cid):
        raise ValueError("Contrato na área de recuperação; recupere-o antes de o alterar")
    return c


def locked_report(db, rid):
    lock_report_users(db, rid)
    r = need(db, Report, rid)
    charger = need(db, Charger, r.charger_id)
    active_contract(db, charger.contract_id, allow_trash=True)
    lock_charger(db, charger.id)
    r = db.scalar(
        select(Report)
        .where(Report.id == rid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if r is None:
        raise ValueError("Report já apagado")
    return r


def audit(db, action, target):
    # Non-login principal: do not attribute console work to an arbitrary admin.
    actor = db.scalar(select(User).where(User.role == "system").limit(1))
    if actor is None:
        actor = User(
            id=str(uuid.uuid4()),
            name="Administração local",
            role="system",
            email="console-" + secrets.token_hex(12) + "@system.invalid",
            password=hash_password(secrets.token_urlsafe(40)),
        )
        db.add(actor)
        db.flush()
    db.add(Audit(id=str(uuid.uuid4()), user_id=actor.id, action="console." + action, target=target))


def confirm(target):
    print("Eliminação: " + target)
    if input("Escreva exatamente APAGAR para confirmar: ") != "APAGAR":
        raise ValueError("Eliminação cancelada; confirmação incorreta")


def password_input(generate=False):
    if generate:
        return secrets.token_urlsafe(24)
    # Refuse getpass's echoing fallback when no private terminal is available.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("Nova password (mínimo 12 caracteres): ")
            repeated = getpass.getpass("Repetir password: ")
        except getpass.GetPassWarning:
            raise ValueError(
                "Use um terminal privado ou --generate; leitura sem eco indisponível"
            ) from None
    if password != repeated:
        raise ValueError("As passwords não coincidem")
    if not 12 <= len(password) <= 200:
        raise ValueError("Password: entre 12 e 200 caracteres")
    hash_password(password)  # Validate before changing any row.
    return password


def keep_last_admin(db, user, new_role):
    # account(..., lock="write") already holds the singleton mutation guard.
    # One target row only: locking all admins could deadlock a cross-account API
    # operation whose actor and target hold shared locks in a different order.
    count = db.scalar(select(func.count(User.id)).where(User.role == "admin"))
    if user.role == "admin" and new_role != "admin" and count <= 1:
        raise ValueError("Não pode remover/despromover o último administrador; crie outro primeiro")


def user_commands(db, args):
    action = args.action
    if action == "list":
        roles = (*ACTIVE, "deleted") if args.include_deleted else ACTIVE
        return [
            public_user(db, u)
            for u in db.scalars(select(User).where(User.role.in_(roles)).order_by(User.name))
        ]
    if action == "create":
        email, name = args.email.strip().lower(), args.name.strip()
        if not name or len(name) > 200 or "@" not in email or len(email) > 254:
            raise ValueError("Nome/email inválido")
        existing = db.scalar(select(User).where(User.email == email))
        if existing:
            if args.if_missing and existing.role == args.role:
                return public_user(db, existing)
            raise ValueError("Email já existe com este ou outro perfil; use users update")
        password = password_input(args.generate)
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            name=name,
            role=args.role,
            password=hash_password(password),
        )
        db.add(user)
        db.flush()
        audit(db, "user.create", user.id)
        result = public_user(db, user)
        if args.show_password and args.generate:
            result["new_password"] = password  # One newly generated password only.
        return result
    if action == "revoke-sessions":
        statement = delete(SessionToken)
        if args.all:
            account_write_guard(db)
            target = "all"
        else:
            user = account(db, args.user, lock="write")
            statement = statement.where(SessionToken.user_id == user.id)
            target = user.id
        # Delete and audit in the same transaction. The login guard prevents
        # concurrent issuance from escaping a revocation already in progress.
        count = db.execute(statement).rowcount
        audit(db, "sessions.revoke", target)
        return dict(target=target, sessions_revoked=count)
    user = account(db, args.user)
    if action in ("assign", "unassign"):
        user = account(db, user.id, lock="read")
        active_contract(db, args.contract)
        assignment = db.get(Assignment, (user.id, args.contract))
        if action == "assign" and assignment is None:
            db.add(Assignment(user_id=user.id, contract_id=args.contract))
        elif action == "unassign" and assignment is not None:
            db.delete(assignment)
        audit(db, "user." + action, user.id + ":" + args.contract)
        return dict(id=user.id, contract_id=args.contract, assigned=action == "assign")
    if action == "show":
        return public_user(db, user)
    if action == "password-hash":
        return dict(
            id=user.id,
            email=user.email,
            password_hash=user.password,
            note="Hash scrypt; não permite recuperar a password original",
        )
    if action == "password":
        confirmed_id = user.id
        db.rollback()
        password = password_input(args.generate)
        user = account(db, confirmed_id, lock="write")
        user.password = hash_password(password)
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
        audit(db, "user.password", user.id)
        result = dict(id=user.id, changed=True, sessions_revoked=True)
        if args.show_password and args.generate:
            result["new_password"] = password
        return result
    if action == "update":
        if not any((args.name is not None, args.email is not None, args.role is not None)):
            raise ValueError("Indique --name, --email ou --role")
        user = account(db, user.id, lock="write")
        keep_last_admin(db, user, args.role or user.role)
        if args.name is not None:
            if not args.name.strip() or len(args.name.strip()) > 200:
                raise ValueError("Nome inválido")
            user.name = args.name.strip()
        if args.email is not None:
            email = args.email.strip().lower()
            if "@" not in email or len(email) > 254:
                raise ValueError("Email inválido")
            if db.scalar(select(User.id).where(User.email == email, User.id != user.id)):
                raise ValueError("Email já existe")
            user.email = email
        if args.role:
            user.role = args.role
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
        audit(db, "user.update", user.id)
        return public_user(db, user)
    if action == "delete":
        confirmed_id = user.id
        confirm(
            f"conta {user.name} <{user.email}> [{user.id}]. Reports ficam sem proprietário ativo."
        )
        db.rollback()  # Never wait for operator input while holding write locks.
        user = account(db, confirmed_id, lock="write")
        keep_last_admin(db, user, "deleted")
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
        db.execute(delete(Assignment).where(Assignment.user_id == user.id))
        db.execute(
            update(Report)
            .where(Report.owner_id == user.id)
            .values(revision=Report.revision + 1, updated=now())
        )
        user.name = "Conta eliminada"
        user.email = "deleted-" + user.id + "@invalid"
        user.password = hash_password(secrets.token_urlsafe(40))
        user.role = "deleted"
        audit(db, "user.delete", user.id)
        return dict(
            id=user.id,
            deleted=True,
            note="Identidade anonimizada para integridade do histórico; sem login",
        )


def contract_commands(db, args, factory):
    if args.action == "list":
        rows = db.execute(
            select(Contract, ContractTrash)
            .outerjoin(ContractTrash, ContractTrash.contract_id == Contract.id)
            .order_by(Contract.name)
        )
        return [
            dict(
                id=c.id,
                name=c.name,
                deleted_at=t.deleted_at if t else None,
                purge_after=t.purge_after if t else None,
            )
            for c, t in rows
            if not args.deleted or t is not None
        ]
    if args.action in ("trash", "purge"):
        preview = need(db, Contract, args.contract)
        confirm(
            (
                "eliminação DEFINITIVA de "
                if args.action == "purge"
                else "apagar com recuperação de 30 dias: "
            )
            + preview.name
            + " ["
            + preview.id
            + "]"
        )
        db.rollback()
    c = active_contract(db, args.contract, allow_trash=True)
    entry = db.get(ContractTrash, c.id)
    if args.action == "trash":
        if entry is None:
            instant = utc_now()
            entry = ContractTrash(
                contract_id=c.id,
                deleted_by=console_actor(db),
                deleted_at=instant.isoformat(),
                purge_after=(instant + RETENTION).isoformat(),
            )
            db.add(entry)
            audit(db, "contract.trash", c.id)
        return dict(id=c.id, purge_after=entry.purge_after)
    if args.action == "restore":
        if entry:
            if entry.purge_after <= now():
                raise ValueError("Prazo de recuperação terminado")
            db.delete(entry)
            audit(db, "contract.restore", c.id)
        return dict(id=c.id, restored=True)
    if args.action == "purge":
        if entry is None:
            raise ValueError(
                "Primeiro coloque o contrato na área de recuperação: contracts trash ID"
            )
        # Keep the exclusive contract lock until every row has been removed.
        # A separate commit/sweep would allow restore to win while we reported
        # a successful deletion. execute() owns the sole commit.
        cid = c.id
        audit(db, "contract.force-purge", cid)
        purge_contract(db, c, entry)
        return dict(id=cid, purged=True)


def console_actor(db):
    audit(db, "session", "local")
    return db.scalar(select(User.id).where(User.role == "system").limit(1))


def report_commands(db, args):
    if args.action in ("list", "show"):
        query = (
            select(Report, User)
            .outerjoin(User, User.id == Report.owner_id)
            .join(Charger, Charger.id == Report.charger_id)
        )
        if args.action == "show":
            query = query.where(Report.id == args.report)
        if getattr(args, "contract", None):
            query = query.where(Charger.contract_id == args.contract)
        rows = list(db.execute(query.order_by(Report.updated.desc())))
        return [
            dict(
                id=r.id,
                charger_id=r.charger_id,
                status=r.status,
                revision=r.revision,
                owner_id=u.id if u and u.role in ACTIVE else None,
                owner_name=u.name if u and u.role in ACTIVE else "Sem proprietário",
            )
            for r, u in rows
            if not getattr(args, "unowned", False) or not u or u.role not in ACTIVE
        ]
    if args.action == "delete":
        preview = need(db, Report, args.report)
        confirmed_revision, confirmed_status = preview.revision, preview.status
        confirm(
            f"report {preview.id} ({preview.status}); fotografias incluídas; eliminação definitiva"
        )
        db.rollback()
    owner = account(db, args.owner, lock="read") if args.action == "owner" else None
    r = locked_report(db, args.report)
    correction = db.get(ReportCorrection, r.id)
    if args.action == "owner":
        if correction and correction.state == "editing":
            raise ValueError("Cópia de correção em curso: cancele a cópia antes de transferir a propriedade")
        cid = need(db, Charger, r.charger_id).contract_id
        if owner.role == "tech" and not db.get(Assignment, (owner.id, cid)):
            if not args.assign_access:
                raise ValueError("Técnico sem acesso ao contrato; acrescente --assign-access")
            db.add(Assignment(user_id=owner.id, contract_id=cid))
        old = r.owner_id
        if old != owner.id:
            r.owner_id = owner.id
            r.revision += 1
            r.updated = now()
            if old:
                db.add(
                    OwnershipTransfer(
                        id=str(uuid.uuid4()),
                        report_id=r.id,
                        old_owner_id=old,
                        new_owner_id=owner.id,
                        actor_id=console_actor(db),
                    )
                )
            audit(db, "report.owner", r.id)
        return dict(id=r.id, owner_id=owner.id, revision=r.revision, status=r.status)
    if args.action == "delete":
        if r.revision != confirmed_revision or r.status != confirmed_status:
            raise ValueError(
                "Report alterado durante a confirmação; consulte a versão atual e confirme novamente"
            )
        if db.scalar(select(ReportCorrection.id).where(ReportCorrection.source_id == r.id,
                ReportCorrection.state == "editing").limit(1)):
            raise ValueError("Existe uma correção em curso; cancele/apague a cópia antes de apagar o original")
        claimed = db.execute(update(Report).where(Report.id == r.id, Report.revision == confirmed_revision,
            Report.status == confirmed_status).values(revision=confirmed_revision+1)
            .execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise ValueError("Report alterado durante a remoção; dados conservados")
        if correction and correction.state == "editing":
            correction.state = "cancelled"
        db.execute(delete(Photo).where(Photo.report_id == r.id))
        db.add(
            DeletedReport(id=r.id, charger_id=r.charger_id, owner_id=r.owner_id, status=r.status)
        )
        db.delete(r)
        audit(db, "report.delete", args.report)
        return dict(id=args.report, deleted=True)


def charger_commands(db, args):
    c = active_contract(db, args.contract, write=args.action != "list")
    if args.action == "list":
        return [
            dict(id=x.id, external_id=x.external_id, details=x.details)
            for x in db.scalars(
                select(Charger).where(Charger.contract_id == c.id, attached_charger()).order_by(Charger.external_id)
            )
        ]
    details = {}
    if args.details_json:
        if args.details_json == "-":
            text = sys.stdin.read(100001)
        else:
            path = Path(args.details_json)
            if path.stat().st_size > 100000:
                raise ValueError("JSON demasiado grande (máximo 100 KB)")
            text = path.read_text(encoding="utf-8")
        if len(text) > 100000:
            raise ValueError("JSON demasiado grande (máximo 100 KB)")
        details = json.loads(text)
        if not isinstance(details, dict):
            raise ValueError("O ficheiro deve conter um objeto JSON")
    for field in args.set or []:
        key, separator, value = field.partition("=")
        if not separator or not key.strip():
            raise ValueError("Use --set campo=valor")
        try:
            value = json.loads(value)
        except ValueError:
            pass
        if key.strip() in (
            "model",
            "serial",
            "cpe",
            "address",
            "postal_code",
            "manufacturer",
        ):
            # Identifiers such as serial 123 are strings in the app/import model.
            if not isinstance(value, str):
                value = field.partition("=")[2]
        details[key.strip()] = value
    lock_catalogue(db)
    if args.action == "add":
        if (
            not args.external_id
            or not args.external_id.strip()
            or len(args.external_id.strip()) > 200
        ):
            raise ValueError("Indique --external-id válido")
        lock_catalogue(db)
        station = get_or_create_station(db, args.external_id.strip(), {})
        charger = db.scalar(select(Charger).where(Charger.contract_id == c.id, Charger.station_id == station.id))
        if charger is None:
            charger = Charger(id=str(uuid.uuid4()), contract_id=c.id, external_id=station.external_id, station=station)
            db.add(charger)
        elif marker := db.get(DetachedCharger, charger.id):
            db.delete(marker)
        else:
            raise ValueError("O PCVE já pertence ao contrato")
    else:
        charger = need(db, Charger, args.charger)
        if charger.contract_id != c.id:
            raise ValueError("Carregador não pertence ao contrato indicado")
        if args.external_id is not None and args.external_id.strip() != charger.external_id:
            raise ValueError("O ID do PCVE é permanente.")
    if "external_id" in details:
        raise ValueError(
            "external_id não pertence aos detalhes; o ID do PCVE é permanente"
        )
    if any(is_contract_notes(key) for key in details):
        raise ValueError("Edite as notas deste ID na página de notas do contrato.")
    if any(obsolete_station_field(key) for key in details):
        raise ValueError("Campo excluído da ficha do PCVE.")
    merged = clean_station_details({**(charger.details or {}), **details})
    # Old metadata cannot shadow the authoritative Charger.external_id column.
    merged.pop("external_id", None)
    # Console JSON must follow the same scalar/string contract as imports and
    # the native client's QString readers. A numeric serial otherwise vanishes.
    text_fields = (
        "model",
        "serial",
        "cpe",
        "address",
        "postal_code",
        "manufacturer",
        "region",
        "energy_supply",
        "power",
        "power_limit",
        "power_unit",
    )
    json.dumps(merged, allow_nan=False)
    for field in text_fields:
        if field not in merged:
            continue
        value = merged[field]
        if isinstance(value, (list, dict, bool)):
            raise ValueError(field + " deve ser texto ou um valor escalar")
        merged[field] = "" if value is None else str(value)
    for field, limit in (("latitude", 90), ("longitude", 180)):
        value = merged.get(field)
        if value is None or value == "":
            continue
        try:
            number = float(str(value).replace(",", "."))
        except ValueError:
            raise ValueError(field + " inválida") from None
        if isinstance(value, bool) or not math.isfinite(number) or abs(number) > limit:
            raise ValueError(field + " fora dos limites")
        # Python float accepts forms such as '1_0'/' +1e1'; QML's string parser
        # deliberately does not. Persist a number that the client can use directly.
        merged[field] = number
    if not isinstance(merged.get("model"), str) or not merged["model"].strip():
        raise ValueError("Indique o modelo: --set model=MODELO")
    if "connectors" in merged and not (
        isinstance(merged["connectors"], str)
        or isinstance(merged["connectors"], list)
        and all(isinstance(x, str) for x in merged["connectors"])
    ):
        raise ValueError("connectors deve ser texto ou lista de textos")
    # Validate connector configuration without importing the API/bootstrap module.
    shared = Path(__file__).resolve().parents[2] / "shared/checklist-v1.json"
    if not shared.exists():
        shared = Path(__file__).resolve().parents[2].parent / "shared/checklist-v1.json"
    for_charger(json.loads(shared.read_text(encoding="utf-8")), merged)
    lock_catalogue(db)
    charger.details = merged
    audit(db, "charger." + args.action, charger.id)
    return dict(
        id=charger.id,
        contract_id=c.id,
        external_id=charger.external_id,
        details=merged,
        note="Snapshots de reports existentes são conservados; novos reports usam estes dados",
    )


def parser():
    p = argparse.ArgumentParser(description="LEM: administração local (use help para exemplos)")
    groups = p.add_subparsers(dest="group", required=True)
    users = groups.add_parser("users", help="Contas, perfis e passwords").add_subparsers(
        dest="action", required=True
    )
    users.add_parser("list").add_argument("--include-deleted", action="store_true")
    for name in ["show", "password-hash", "delete"]:
        users.add_parser(name).add_argument("user", help="ID ou email")
    for name in ("assign", "unassign"):
        cmd = users.add_parser(name)
        cmd.add_argument("user")
        cmd.add_argument("contract")
    create = users.add_parser("create")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--role", choices=ACTIVE, default="tech")
    create.add_argument("--if-missing", action="store_true")
    edit = users.add_parser("update")
    edit.add_argument("user")
    edit.add_argument("--name")
    edit.add_argument("--email")
    edit.add_argument("--role", choices=ACTIVE)
    password = users.add_parser("password")
    password.add_argument("user")
    revoke = users.add_parser("revoke-sessions", help="Obrigar a iniciar sessão novamente")
    scope = revoke.add_mutually_exclusive_group(required=True)
    scope.add_argument("user", nargs="?", help="ID ou email")
    scope.add_argument("--all", action="store_true", help="Todas as contas")
    for command in [create, password]:
        command.add_argument("--generate", action="store_true")
        command.add_argument(
            "--show-password",
            action="store_true",
            help="Mostrar só a nova password gerada desta conta",
        )
    contracts = groups.add_parser("contracts").add_subparsers(dest="action", required=True)
    contracts.add_parser("list").add_argument("--deleted", action="store_true")
    for name in ["trash", "restore", "purge"]:
        contracts.add_parser(name).add_argument("contract")
    reports = groups.add_parser("reports").add_subparsers(dest="action", required=True)
    listing = reports.add_parser("list")
    listing.add_argument("--contract")
    listing.add_argument("--unowned", action="store_true")
    reports.add_parser("show").add_argument("report")
    reports.add_parser("delete").add_argument("report")
    owner = reports.add_parser("owner")
    owner.add_argument("report")
    owner.add_argument("owner", help="ID/email da nova conta")
    owner.add_argument("--assign-access", action="store_true")
    chargers = groups.add_parser("chargers").add_subparsers(dest="action", required=True)
    chargers.add_parser("list").add_argument("contract")
    for name in ["add", "update"]:
        cmd = chargers.add_parser(name)
        cmd.add_argument("contract")
        if name == "update":
            cmd.add_argument("charger")
        cmd.add_argument("--external-id", help=argparse.SUPPRESS if name == "update" else "ID permanente do PCVE")
        cmd.add_argument("--details-json", help="Objeto JSON; update mantém as outras chaves")
        cmd.add_argument(
            "--set", action="append", help="campo=valor (repetível); valor JSON ou texto"
        )
    return p


def execute(factory, args):
    with factory() as db:
        handlers = {
            "users": user_commands,
            "contracts": lambda d, a: contract_commands(d, a, factory),
            "reports": report_commands,
            "chargers": charger_commands,
        }
        result = handlers[args.group](db, args)
        db.commit()
        return result


HELP = """Administração LEM — exemplos (IDs aceites onde indicado):
users list | users show EMAIL | users create --email EMAIL --name "Nome" --role admin --if-missing
users update EMAIL --name "Nome" --email NOVO_EMAIL --role tech
users assign EMAIL CONTRACT_ID | users unassign EMAIL CONTRACT_ID
users password EMAIL [--generate --show-password] | users password-hash EMAIL | users delete EMAIL
users revoke-sessions EMAIL_OR_ID | users revoke-sessions --all
contracts list [--deleted] | contracts trash ID | contracts restore ID | contracts purge ID
reports list [--contract ID] [--unowned] | reports show ID
reports owner REPORT_ID EMAIL [--assign-access] | reports delete ID
chargers list CONTRACT_ID
chargers add CONTRACT_ID --external-id ID --details-json /caminho/details.json
chargers update CONTRACT_ID CHARGER_UUID --details-json /caminho/patch.json
Password original: não é recuperável; existe apenas o hash scrypt.
Eliminações exigem escrever APAGAR. 'purge' é definitivo. 'exit' para sair.
"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("help", "--help", "-h"):
        print(HELP)
        return 0
    _, factory = database()

    def run(words):
        try:
            emit(execute(factory, parser().parse_args(words)))
            return 0
        except (ValueError, IntegrityError, OSError) as error:
            print(
                "Erro: "
                + (
                    "Conflito de dados; operação cancelada"
                    if isinstance(error, IntegrityError)
                    else str(error)
                ),
                file=sys.stderr,
            )
            return 1

    if argv:
        try:
            return run(argv)
        except (EOFError, KeyboardInterrupt):
            print("Operação cancelada", file=sys.stderr)
            return 130
    print(HELP)
    while True:
        try:
            command = input("lem-admin> ").strip()
            if command in ("exit", "quit", "sair"):
                return 0
            if command in ("help", "ajuda"):
                print(HELP)
                continue
            if command:
                try:
                    run(shlex.split(command))
                except SystemExit:
                    pass
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
