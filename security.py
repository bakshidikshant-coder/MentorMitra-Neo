import datetime as dt
import hashlib
import hmac
import os
import secrets
from functools import wraps

import jwt
from flask import current_app, g, request

from common import ApiError, now_iso
from db import get_db


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


def make_token(user_id: int, role: str) -> str:
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=current_app.config["TOKEN_TTL_HOURS"])
    return jwt.encode({"sub": str(user_id), "role": role, "exp": exp}, current_app.config["SECRET_KEY"], "HS256")


def create_user(db, email, name, role, password=None):
    """Returns (user_id, temp_password or None). A temp password is generated when none is given."""
    if len(str(email or "")) < 3 or "@" not in email:
        raise ApiError(f"Invalid email: {email!r}")
    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise ApiError(f"A user with email {email} already exists", 409)
    temp = None
    if not password:
        password = temp = secrets.token_urlsafe(8)
    if len(password) < 8:
        raise ApiError("Password must be at least 8 characters")
    cur = db.execute(
        "INSERT INTO users(email, name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
        (email.strip(), name.strip(), role, hash_password(password), now_iso()),
    )
    return cur.lastrowid, temp


def auth_required(*roles):
    """Decorator: valid bearer token + (optionally) one of the given roles. Sets g.user."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise ApiError("Missing bearer token", 401)
            try:
                payload = jwt.decode(header[7:], current_app.config["SECRET_KEY"], algorithms=["HS256"])
                uid = int(payload["sub"])
            except (jwt.PyJWTError, KeyError, ValueError):
                raise ApiError("Invalid or expired token", 401)
            row = get_db().execute(
                """SELECT u.id, u.email, u.name, u.role, t.id AS teacher_id, s.id AS student_id
                   FROM users u LEFT JOIN teachers t ON t.user_id=u.id AND t.active=1
                                LEFT JOIN students s ON s.user_id=u.id AND s.active=1
                   WHERE u.id=? AND u.active=1""", (uid,)).fetchone()
            if not row:
                raise ApiError("Account not found or disabled", 401)
            if roles and row["role"] not in roles:
                raise ApiError("You do not have permission for this action", 403)
            g.user = dict(row)
            return fn(*args, **kwargs)
        return wrapper
    return deco
