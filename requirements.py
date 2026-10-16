import datetime as dt

from common import ApiError, fetch_all, now_iso

STATUSES = {
    "exam_form": ["not_submitted", "submitted"],
    "informed": ["not_informed", "informed"],
    "sessional": ["pending", "completed"],
    "fees": ["pending", "partly_paid", "paid"],
}
DONE = {"exam_form": "submitted", "informed": "informed", "sessional": "completed", "fees": "paid"}
INITIAL = {t: s[0] for t, s in STATUSES.items()}
URGENCY_RANK = {"overdue": 0, "due_soon": 1, "on_track": 2, "no_deadline": 3, "done": 4}


def urgency(req, today: dt.date, due_soon_days: int):
    if req["status"] == DONE[req["type"]]:
        return "done"
    if not req["deadline"]:
        return "no_deadline"
    d = dt.date.fromisoformat(req["deadline"])
    if d < today:
        return "overdue"
    return "due_soon" if (d - today).days <= due_soon_days else "on_track"


def validate_status(rtype, status):
    if status not in STATUSES[rtype]:
        raise ApiError(f"Invalid status '{status}' for {rtype}; allowed: {', '.join(STATUSES[rtype])}")


def requirements_for(db, student_ids, session, today, due_days):
    out = {sid: [] for sid in student_ids}
    if not student_ids:
        return out
    marks = ",".join("?" * len(student_ids))
    for r in fetch_all(db, f"SELECT * FROM requirements WHERE session=? AND student_id IN ({marks}) ORDER BY id",
                       (session, *student_ids)):
        r["urgency"] = urgency(r, today, due_days)
        r["is_done"] = r["urgency"] == "done"
        out[r["student_id"]].append(r)
    return out


def initialize(db, session, deadlines, actor_user_id):
    """Create the four requirement rows for every active student (existing rows untouched)."""
    ts, created = now_iso(), 0
    for s in db.execute("SELECT id FROM students WHERE active=1").fetchall():
        for rtype in STATUSES:
            cur = db.execute(
                """INSERT OR IGNORE INTO requirements(student_id, session, type, status, deadline, updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (s["id"], session, rtype, INITIAL[rtype], deadlines.get(rtype), actor_user_id, ts))
            created += cur.rowcount
    return created
