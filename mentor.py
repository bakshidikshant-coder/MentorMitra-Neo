import datetime as dt

from flask import Blueprint, g, jsonify, request

from common import ApiError, fetch_all, fetch_one, json_body, need, now_iso, parse_date, today
from db import audit, get_db, get_setting
from security import auth_required
from services import requirements as reqsvc
from services.notify import notify
from services.risk import compute_risk

bp = Blueprint("mentor", __name__, url_prefix="/api/mentor")
mentor_only = auth_required("mentor")
ACTIONS = {"reminder", "call", "meeting", "email", "note"}
QUERY_STATUS = {"open", "in_progress", "escalated", "resolved"}


def _ctx(db):
    return get_setting(db, "current_session"), int(get_setting(db, "due_soon_days")), int(get_setting(db, "stale_query_days"))


def mentee_rows(db, teacher_id, session):
    return fetch_all(db, """SELECT s.id, s.roll_no, s.programme, s.semester, s.section, u.name, u.email, s.user_id
        FROM mentor_assignments a JOIN students s ON s.id=a.student_id JOIN users u ON u.id=s.user_id
        WHERE a.teacher_id=? AND a.session=? AND a.active=1 AND s.active=1 ORDER BY s.roll_no""", (teacher_id, session))


def own_mentee(db, student_id):
    sess = _ctx(db)[0]
    row = fetch_one(db, """SELECT s.*, u.name, u.email FROM mentor_assignments a JOIN students s ON s.id=a.student_id
        JOIN users u ON u.id=s.user_id WHERE a.student_id=? AND a.teacher_id=? AND a.session=? AND a.active=1""",
                    (student_id, g.user["teacher_id"], sess))
    if not row:
        raise ApiError("This student is not one of your mentees", 403)
    return row


def _summarise(db, mentees, session, due, stale):
    reqs = reqsvc.requirements_for(db, [m["id"] for m in mentees], session, today(), due)
    out = []
    for m in mentees:
        r = reqs[m["id"]]
        worst = min((x["urgency"] for x in r), key=lambda u: reqsvc.URGENCY_RANK[u], default="no_deadline")
        out.append({
            "student_id": m["id"], "roll_no": m["roll_no"], "name": m["name"],
            "class": f"{m['programme']}-{m['semester']}-{m['section']}",
            "requirements": [{k: x[k] for k in ("id", "type", "status", "deadline", "urgency", "remarks", "amount_due", "claimed_at")} for x in r],
            "worst_urgency": worst,
            "pending_claims": sum(1 for x in r if x["claimed_at"] and not x["is_done"]),
            "open_queries": fetch_one(db, "SELECT COUNT(*) c FROM queries WHERE student_id=? AND status!='resolved'", (m["id"],))["c"],
            "risk": compute_risk(db, m["id"], r, today(), stale),
        })
    return out


@bp.get("/dashboard")
@mentor_only
def dashboard():
    db = get_db()
    sess, due, stale = _ctx(db)
    items = _summarise(db, mentee_rows(db, g.user["teacher_id"], sess), sess, due, stale)
    t = today().isoformat()
    fu = fetch_all(db, "SELECT * FROM followups WHERE teacher_id=? AND done=0 AND next_followup_date IS NOT NULL", (g.user["teacher_id"],))
    return jsonify(
        session=sess, mentees=len(items),
        students_with_pending_items=sum(1 for i in items if any(r["urgency"] != "done" for r in i["requirements"])),
        overdue_items=sum(1 for i in items for r in i["requirements"] if r["urgency"] == "overdue"),
        open_queries=sum(i["open_queries"] for i in items),
        awaiting_verification=sum(i["pending_claims"] for i in items),
        high_risk=sum(1 for i in items if i["risk"]["level"] == "high"),
        followups_due=sum(1 for f in fu if f["next_followup_date"] >= t),
        followups_missed=sum(1 for f in fu if f["next_followup_date"] < t),
        needs_attention=sorted(items, key=lambda i: (reqsvc.URGENCY_RANK[i["worst_urgency"]], -i["risk"]["score"]))[:10])


@bp.get("/mentees")
@mentor_only
def mentees():
    db = get_db()
    sess, due, stale = _ctx(db)
    items = _summarise(db, mentee_rows(db, g.user["teacher_id"], sess), sess, due, stale)
    a = request.args
    if a.get("risk"):
        items = [i for i in items if i["risk"]["level"] == a["risk"]]
    if a.get("urgency"):
        items = [i for i in items if any(r["urgency"] == a["urgency"] for r in i["requirements"])]
    if a.get("type") and a.get("status"):
        items = [i for i in items if any(r["type"] == a["type"] and r["status"] == a["status"] for r in i["requirements"])]
    if a.get("q"):
        q = a["q"].lower()
        items = [i for i in items if q in i["name"].lower() or q in i["roll_no"].lower()]
    items.sort(key=lambda i: (reqsvc.URGENCY_RANK[i["worst_urgency"]], -i["risk"]["score"], i["roll_no"]))
    return jsonify(items)


@bp.get("/mentees/<int:sid>")
@mentor_only
def mentee_detail(sid):
    db = get_db()
    own_mentee(db, sid)
    sess, due, stale = _ctx(db)
    m = [x for x in mentee_rows(db, g.user["teacher_id"], sess) if x["id"] == sid]
    summary = _summarise(db, m, sess, due, stale)[0]
    summary["queries"] = fetch_all(db, "SELECT * FROM queries WHERE student_id=? ORDER BY id DESC", (sid,))
    summary["followups"] = fetch_all(db, "SELECT * FROM followups WHERE student_id=? AND teacher_id=? ORDER BY id DESC",
                                     (sid, g.user["teacher_id"]))
    return jsonify(summary)


@bp.patch("/requirements/<int:rid>")
@mentor_only
def update_requirement(rid):
    d, db = json_body(), get_db()
    r = fetch_one(db, "SELECT * FROM requirements WHERE id=?", (rid,))
    if not r:
        raise ApiError("Requirement not found", 404)
    own_mentee(db, r["student_id"])
    sets, args = [], []
    if "status" in d:
        reqsvc.validate_status(r["type"], d["status"])
        sets.append("status=?"); args.append(d["status"])
        if d["status"] == "partly_paid" and d.get("amount_due") is None:
            raise ApiError("amount_due is required when status is partly_paid")
        if d["status"] == "paid":
            sets.append("amount_due=?"); args.append(0)
    if "amount_due" in d and d["amount_due"] is not None:
        if not isinstance(d["amount_due"], (int, float)) or d["amount_due"] < 0:
            raise ApiError("amount_due must be a non-negative number")
        if d.get("status") != "paid":
            sets.append("amount_due=?"); args.append(d["amount_due"])
    if "deadline" in d:
        sets.append("deadline=?"); args.append(parse_date(d["deadline"], "deadline"))
    if "remarks" in d:
        sets.append("remarks=?"); args.append(d["remarks"])
    if not sets:
        raise ApiError("Nothing to update")
    if "status" in d and d["status"] == reqsvc.DONE[r["type"]]:
        sets.append("claimed_at=NULL")   # claim resolved
    sets += ["updated_by=?", "updated_at=?"]; args += [g.user["id"], now_iso(), rid]
    db.execute(f"UPDATE requirements SET {', '.join(sets)} WHERE id=?", args)
    audit(db, g.user["id"], "requirement_update", "requirements", rid, str(d))
    db.commit()
    return jsonify(ok=True)


@bp.post("/reminders")
@mentor_only
def send_reminders():
    """Single or bulk reminder. If requirement_type is given, only students who have that item pending are reminded."""
    d, db = json_body(), get_db()
    need(d, "student_ids")
    if not isinstance(d["student_ids"], list) or not d["student_ids"]:
        raise ApiError("student_ids must be a non-empty list")
    channels = d.get("channels") or ["in_app"]
    if not set(channels) <= {"in_app", "email", "sms"}:
        raise ApiError("channels must be a subset of in_app, email, sms")
    rtype = d.get("requirement_type")
    if rtype and rtype not in reqsvc.STATUSES:
        raise ApiError("Unknown requirement_type")
    sess, due, _ = _ctx(db)
    sent, skipped = [], []
    for sid in d["student_ids"]:
        s = own_mentee(db, sid)
        reqs = reqsvc.requirements_for(db, [sid], sess, today(), due)[sid]
        target = [r for r in reqs if not r["is_done"] and (not rtype or r["type"] == rtype)]
        if not target:
            skipped.append(sid); continue
        labels = ", ".join(f"{r['type']} ({r['urgency'].replace('_', ' ')}{', due ' + r['deadline'] if r['deadline'] else ''})" for r in target)
        msg = d.get("message") or f"Reminder from your mentor: please complete {labels}."
        notify(db, s["user_id"], "Reminder from your mentor", msg, channels)
        db.execute("""INSERT INTO followups(student_id, teacher_id, requirement_id, action, note, next_followup_date, created_at)
                      VALUES (?,?,?,?,?,?,?)""",
                   (sid, g.user["teacher_id"], target[0]["id"] if len(target) == 1 else None, "reminder", msg,
                    parse_date(d.get("next_followup_date"), "next_followup_date"), now_iso()))
        sent.append(sid)
    db.commit()
    return jsonify(sent=sent, skipped_nothing_pending=skipped)


@bp.post("/followups")
@mentor_only
def add_followup():
    d, db = json_body(), get_db()
    need(d, "student_id", "action")
    if d["action"] not in ACTIONS:
        raise ApiError("action must be one of: " + ", ".join(sorted(ACTIONS)))
    own_mentee(db, d["student_id"])
    cur = db.execute("""INSERT INTO followups(student_id, teacher_id, query_id, requirement_id, action, note, outcome, next_followup_date, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (d["student_id"], g.user["teacher_id"], d.get("query_id"), d.get("requirement_id"), d["action"],
                      d.get("note"), d.get("outcome"), parse_date(d.get("next_followup_date"), "next_followup_date"), now_iso()))
    db.commit()
    return jsonify(id=cur.lastrowid), 201


@bp.patch("/followups/<int:fid>")
@mentor_only
def update_followup(fid):
    d, db = json_body(), get_db()
    f = fetch_one(db, "SELECT * FROM followups WHERE id=? AND teacher_id=?", (fid, g.user["teacher_id"]))
    if not f:
        raise ApiError("Follow-up not found", 404)
    if "outcome" in d:
        db.execute("UPDATE followups SET outcome=? WHERE id=?", (d["outcome"], fid))
    if "next_followup_date" in d:
        db.execute("UPDATE followups SET next_followup_date=? WHERE id=?", (parse_date(d["next_followup_date"]), fid))
    if "done" in d:
        db.execute("UPDATE followups SET done=?, completed_at=? WHERE id=?",
                   (1 if d["done"] else 0, now_iso() if d["done"] else None, fid))
    db.commit()
    return jsonify(ok=True)


@bp.get("/calendar")
@mentor_only
def calendar():
    db = get_db()
    t = today().isoformat()
    rows = fetch_all(db, """SELECT f.*, u.name AS student_name, s.roll_no FROM followups f JOIN students s ON s.id=f.student_id
        JOIN users u ON u.id=s.user_id WHERE f.teacher_id=? AND f.done=0 AND f.next_followup_date IS NOT NULL
        ORDER BY f.next_followup_date""", (g.user["teacher_id"],))
    return jsonify(missed=[r for r in rows if r["next_followup_date"] < t],
                   upcoming=[r for r in rows if r["next_followup_date"] >= t])


# ---------- queries ----------
@bp.get("/queries")
@mentor_only
def queries():
    db = get_db()
    sess = _ctx(db)[0]
    status = request.args.get("status")
    sql = """SELECT q.*, u.name AS student_name, s.roll_no FROM queries q JOIN students s ON s.id=q.student_id
        JOIN users u ON u.id=s.user_id JOIN mentor_assignments a ON a.student_id=s.id AND a.active=1 AND a.session=?
        WHERE a.teacher_id=?"""
    args = [sess, g.user["teacher_id"]]
    if status:
        sql += " AND q.status=?"; args.append(status)
    return jsonify(fetch_all(db, sql + " ORDER BY q.id DESC", args))


def _own_query(db, qid):
    q = fetch_one(db, "SELECT * FROM queries WHERE id=?", (qid,))
    if not q:
        raise ApiError("Query not found", 404)
    own_mentee(db, q["student_id"])
    return q


@bp.get("/queries/<int:qid>")
@mentor_only
def query_detail(qid):
    db = get_db()
    q = _own_query(db, qid)
    q["messages"] = fetch_all(db, """SELECT m.id, u.name AS author, u.role, m.body, m.created_at FROM query_messages m
                                     JOIN users u ON u.id=m.author_user_id WHERE m.query_id=? ORDER BY m.id""", (qid,))
    return jsonify(q)


@bp.post("/queries/<int:qid>/messages")
@mentor_only
def reply(qid):
    d, db = json_body(), get_db()
    need(d, "body")
    q = _own_query(db, qid)
    ts = now_iso()
    db.execute("INSERT INTO query_messages(query_id, author_user_id, body, created_at) VALUES (?,?,?,?)", (qid, g.user["id"], d["body"], ts))
    if q["status"] == "open":
        db.execute("UPDATE queries SET status='in_progress', updated_at=? WHERE id=?", (ts, qid))
    stu = fetch_one(db, "SELECT user_id FROM students WHERE id=?", (q["student_id"],))
    notify(db, stu["user_id"], f"Your mentor replied on '{q['subject']}'", d["body"][:200])
    db.commit()
    return jsonify(ok=True), 201


@bp.patch("/queries/<int:qid>")
@mentor_only
def update_query(qid):
    d, db = json_body(), get_db()
    q = _own_query(db, qid)
    status = d.get("status", q["status"])
    if status not in QUERY_STATUS:
        raise ApiError("status must be one of: " + ", ".join(sorted(QUERY_STATUS)))
    if status == "escalated" and not d.get("escalated_to"):
        raise ApiError("escalated_to (e.g. 'department' or 'admin') is required when escalating")
    ts = now_iso()
    db.execute("UPDATE queries SET status=?, escalated_to=?, updated_at=?, resolved_at=? WHERE id=?",
               (status, d.get("escalated_to") if status == "escalated" else q["escalated_to"], ts,
                ts if status == "resolved" else None, qid))
    if status != q["status"]:
        stu = fetch_one(db, "SELECT user_id FROM students WHERE id=?", (q["student_id"],))
        notify(db, stu["user_id"], f"Query '{q['subject']}' is now {status.replace('_', ' ')}", d.get("note") or "")
    db.commit()
    return jsonify(ok=True)
