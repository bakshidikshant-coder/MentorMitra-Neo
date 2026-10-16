from flask import Blueprint, g, jsonify

from common import ApiError, json_body, need
from db import get_db
from security import auth_required, hash_password, make_token, verify_password

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.post("/login")
def login():
    d = json_body()
    need(d, "email", "password")
    u = get_db().execute("SELECT * FROM users WHERE email=? AND active=1", (d["email"],)).fetchone()
    if not u or not verify_password(d["password"], u["password_hash"]):
        raise ApiError("Invalid email or password", 401)
    return jsonify(token=make_token(u["id"], u["role"]),
                   user={"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]})


@bp.get("/me")
@auth_required()
def me():
    return jsonify(g.user)


@bp.post("/change-password")
@auth_required()
def change_password():
    d = json_body()
    need(d, "old_password", "new_password")
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
    if not verify_password(d["old_password"], u["password_hash"]):
        raise ApiError("Old password is incorrect", 403)
    if len(d["new_password"]) < 8:
        raise ApiError("New password must be at least 8 characters")
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(d["new_password"]), u["id"]))
    db.commit()
    return jsonify(ok=True)
