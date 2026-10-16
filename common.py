import datetime as dt

from flask import current_app, request


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    pinned = current_app.config.get("TODAY")
    return dt.date.fromisoformat(pinned) if pinned else dt.date.today()


def parse_date(value, field="date"):
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise ApiError(f"{field} must be a date in YYYY-MM-DD format")


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("A JSON object body is required")
    return data


def need(data, *fields):
    missing = [f for f in fields if data.get(f) in (None, "")]
    if missing:
        raise ApiError("Missing required field(s): " + ", ".join(missing))


def norm_code(value):
    """Programme/section codes are compared case-insensitively."""
    return str(value or "").strip().upper()


def fetch_all(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def fetch_one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None
