import argparse
import getpass
import os
from pathlib import Path
from sqlalchemy import select, text
from lazarr.config import RuntimeConfig
from lazarr.db import Database
from lazarr.models import User
from lazarr.security import password_hash, audit


def main():
    parser = argparse.ArgumentParser(prog="lazarr")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("create-admin", help="Create the initial administrator")
    setup.add_argument("username")
    sub.add_parser("migrate")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument("--log", choices=["standard", "performance"], default=None)
    args = parser.parse_args()
    if args.data_dir:
        os.environ["LAZARR_DATA_DIR"] = str(Path(args.data_dir).absolute())
    if args.command == "serve":
        import uvicorn

        if args.log is not None:
            os.environ["LAZARR_LOG"] = args.log
        uvicorn.run("lazarr.app:create_app", factory=True, host=args.host, port=args.port, workers=1)
        return
    config = RuntimeConfig(args.data_dir)
    db = Database(config.data_dir / "lazarr.sqlite")
    db.migrate()
    if args.command == "create-admin":
        if not args.username.strip():
            parser.error("Username cannot be empty")
        with db.session() as session:
            if session.scalar(select(User.id).limit(1)) is not None:
                parser.error("Initial account already exists; add accounts in Settings")
        password = getpass.getpass("Password (10+ characters): ")
        if password != getpass.getpass("Repeat password: "):
            parser.error("Passwords do not match")
        encoded = password_hash(password)
        with db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if session.scalar(select(User.id).limit(1)) is not None:
                parser.error("Initial account already exists; add accounts in Settings")
            user = User(username=args.username.strip(), password_hash=encoded)
            session.add(user)
            session.flush()
            audit(session, user.id, "account.bootstrap", str(user.id))
        print("Administrator created. Run lazarr serve.")
    else:
        print("Database migrations applied.")
    db.engine.dispose()


if __name__ == "__main__":
    main()
