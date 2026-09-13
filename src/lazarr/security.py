import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.fernet import Fernet
from sqlalchemy import select, func
from lazarr.models import User, LoginSession, AuditEvent

PASSWORDS = PasswordHasher()
PERMISSIONS = {
    "admin": {"accounts", "settings", "providers", "tasks", "downloads", "library"},
    "user": {"tasks", "downloads", "library"},
}


def permitted(user: User, permission: str) -> bool:
    return user.active and permission in PERMISSIONS.get(user.role, set())


def password_hash(password: str) -> str:
    if len(password) < 10 or len(password) > 1024:
        raise ValueError("Пароль должен содержать от 10 до 1024 символов")
    return PASSWORDS.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return PASSWORDS.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(db, user: User, ttl=86400 * 7):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(
        LoginSession(token_hash=hash_token(token), user_id=user.id, csrf=csrf, expires_at=time.time() + ttl)
    )
    return token, csrf


def change_account(db, user: User, *, active: bool | None = None, password: str | None = None):
    if active is False and user.active and user.role == "admin":
        count = db.scalar(
            select(func.count()).select_from(User).where(User.active.is_(True), User.role == "admin")
        )
        if count <= 1:
            raise ValueError("Нельзя отключить последнего администратора")
    if active is not None:
        user.active = active
    if password:
        user.password_hash = password_hash(password)
    if active is False or password:
        for session in db.scalars(select(LoginSession).where(LoginSession.user_id == user.id)):
            db.delete(session)


def audit(db, user_id: int | None, action: str, target: str, details: dict | None = None):
    db.add(AuditEvent(user_id=user_id, action=action, target=target, details=details or {}))


class SecretStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(Fernet.generate_key())
        self.fernet = Fernet(path.read_bytes())

    def encrypt(self, data: dict) -> str:
        return self.fernet.encrypt(json.dumps(data).encode()).decode()

    def decrypt(self, value: str) -> dict:
        return json.loads(self.fernet.decrypt(value.encode())) if value else {}
