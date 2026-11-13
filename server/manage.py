# Local bootstrap/recovery CLI: create-admin or reset-password, with required --email.
# Uses DATABASE_URL (SQLite in the current directory by default), prompts twice and commits directly.
# Reset revokes that account's sessions. Prefer app.admin_cli for routine administration.
# Run from ongoing_work with the development environment active; see docs/administration.md.

"""Local bootstrap CLI. Password is prompted, never written into source."""
import argparse, getpass
from sqlalchemy import select
from app.models import database, User
from app.security import hash_password
from app.main import uid

p = argparse.ArgumentParser()
p.add_argument("command", choices=["create-admin", "reset-password"])
p.add_argument("--email", required=True)
args = p.parse_args()
_, Factory = database()
with Factory() as db:
    user = db.scalar(select(User).where(User.email == args.email.lower()))
    password = getpass.getpass("Nova password (mínimo 12 caracteres): ")
    if getpass.getpass("Confirmar: ") != password:
        raise SystemExit("Passwords diferentes")
    if args.command == "create-admin":
        if user:
            raise SystemExit("Utilizador já existe")
        user = User(
            id=uid(), email=args.email.lower(), name="Administrador", role="admin",
            password=hash_password(password),
        )
        db.add(user)
    else:
        if not user:
            raise SystemExit("Utilizador inexistente")
        user.password = hash_password(password)
        from app.models import SessionToken
        from sqlalchemy import delete
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    db.commit()
    print("Concluído")
