from flask import Blueprint, g, jsonify, request

from common import ApiError, fetch_all, fetch_one, json_body, need, now_iso, today
from db import get_db, get_setting
from security import auth_required
from services import requirements as reqsvc
from services.notify import notify

bp = Blueprint("student", __name__, url_prefix="/api/student")
student_only = auth_required("student")
CATEGORIES = {"exam_form", "sessional", "fees", "academic", "other"}


def _ctx(db):
    return get_setting(db, "current_session"), int(get_setting(db, "due_soon_days"))


def current_mentor(db, student_id, session):
    return fetch_one(db, """SELECT t.id AS teacher_id, t.user_id, u.name, u.email FROM mentor_assignments a
        JOIN teachers t ON t.id=a.teacher_id JOIN users u ON u.id=t.user_id
        WHERE a.student_id=? AND a.session=? AND a.active=1""", (student_id, session))


@bp.get("/dashboard")
@student_only
def dashboard():
    db = get_db()
    sess, due = _ctx(db)
    sid = g.user["student_id"]
    reqs = reqsvc.requirements_for(db, [sid], sess, today(), due)[sid]
    profile = fetch_one(db, "SELECT roll_no, programme, semester, section FROM students WHERE id=?", (sid,))
    return jsonify(
        profile={**profile, "name": g.user["name"]}, session=sess,
        mentor=current_mentor(db, sid, sess) and {k: v for k, v in current_mentor(db, sid, sess).items() if k != "user_id"},
        requirements=reqs,
        pending=sum(1 for r in reqs if not r["is_done"]), overdue=sum(1 for r in reqs if r["urgency"] == "overdue"),
        open_queries=fetch_one(db, "SELECT COUNT(*) c FROM queries WHERE student_id=? AND status!='resolved'", (sid,))["c"],
        unread_notifications=fetch_one(db, "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND read_at IS NULL AND channel='in_app'",
                                       (g.user["id"],))["c"])


@bp.post("/requirements/<int:rid>/claim")
@student_only
def claim(rid):
    """Student reports an item as completed. It stays pending until the mentor verifies it
    (except 'informed', which the student can acknowledge directly)."""
    d, db = request.get_json(silent=True) or {}, get_db()
    sess, _ = _ctx(db)
    r = fetch_one(db, "SELECT * FROM requirements WHERE id=? AND student_id=?", (rid, g.user["student_id"]))
    if not r:
        raise ApiError("Requirement not found", 404)
    ts = now_iso()
    if r["type"] == "informed":
        db.execute("UPDATE requirements SET status='informed', claimed_at=?, claim_note=?, updated_by=?, updated_at=? WHERE id=?",
                   (ts, d.get("note"), g.user["id"], ts, rid))
        verified = True
    else:
        db.execute("UPDATE requirements SET claimed_at=?, claim_note=?, updated_at=? WHERE id=?", (ts, d.get("note"), ts, rid))
        verified = False
    m = current_mentor(db, g.user["student_id"], sess)
    if m:
        notify(db, m["user_id"], f"{g.user['name']} reported '{r['type']}' as completed",
               d.get("note") or "Please verify and update the status.")
    db.commit()
    return jsonify(ok=True, verified=verified)


@bp.post("/queries")
@student_only
def raise_query():
    d, db = json_body(), get_db()
    need(d, "category", "subject", "description")
    if d["category"] not in CATEGORIES:
        raise ApiError("category must be one of: " + ", ".join(sorted(CATEGORIES)))
    if len(d["description"].strip()) < 5:
        raise ApiError("Please describe the issue in a few words")
    sid, sess = g.user["student_id"], _ctx(db)[0]
    rid = d.get("requirement_id")
    if rid and not fetch_one(db, "SELECT 1 x FROM requirements WHERE id=? AND student_id=?", (rid, sid)):
        raise ApiError("requirement_id does not belong to you", 403)
    ts = now_iso()
    cur = db.execute("""INSERT INTO queries(student_id, requirement_id, category, subject, description, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?)""", (sid, rid, d["category"], d["subject"].strip(), d["description"].strip(), ts, ts))
    m = current_mentor(db, sid, sess)
    if m:
        notify(db, m["user_id"], f"New query from {g.user['name']}: {d['subject']}", d["description"][:200])
    faqs = fetch_all(db, "SELECT question, answer FROM faq WHERE category=?", (d["category"],))
    db.commit()
    return jsonify(id=cur.lastrowid, mentor_assigned=bool(m), suggested_answers=faqs), 201


@bp.get("/queries")
@student_only
def my_queries():
    return jsonify(fetch_all(get_db(), "SELECT * FROM queries WHERE student_id=? ORDER BY id DESC", (g.user["student_id"],)))


@bp.get("/queries/<int:qid>")
@student_only
def query_detail(qid):
    db = get_db()
    q = fetch_one(db, "SELECT * FROM queries WHERE id=? AND student_id=?", (qid, g.user["student_id"]))
    if not q:
        raise ApiError("Query not found", 404)
    q["messages"] = fetch_all(db, """SELECT m.id, u.name AS author, u.role, m.body, m.created_at FROM query_messages m
                                     JOIN users u ON u.id=m.author_user_id WHERE m.query_id=? ORDER BY m.id""", (qid,))
    return jsonify(q)


@bp.post("/queries/<int:qid>/messages")
@student_only
def query_reply(qid):
    d, db = json_body(), get_db()
    need(d, "body")
    q = fetch_one(db, "SELECT * FROM queries WHERE id=? AND student_id=?", (qid, g.user["student_id"]))
    if not q:
        raise ApiError("Query not found", 404)
    ts = now_iso()
    db.execute("INSERT INTO query_messages(query_id, author_user_id, body, created_at) VALUES (?,?,?,?)", (qid, g.user["id"], d["body"], ts))
    if q["status"] == "resolved":   # student replying reopens the query
        db.execute("UPDATE queries SET status='open', resolved_at=NULL, updated_at=? WHERE id=?", (ts, qid))
    else:
        db.execute("UPDATE queries SET updated_at=? WHERE id=?", (ts, qid))
    m = current_mentor(db, g.user["student_id"], _ctx(db)[0])
    if m:
        notify(db, m["user_id"], f"Reply on query #{qid} from {g.user['name']}", d["body"][:200])
    db.commit()
    return jsonify(ok=True), 201


@bp.get("/notifications")
@auth_required()
def notifications():
    db = get_db()
    rows = fetch_all(db, "SELECT id, title, body, created_at, read_at FROM notifications WHERE user_id=? AND channel='in_app' ORDER BY id DESC LIMIT 100",
                     (g.user["id"],))
    return jsonify(rows)


@bp.post("/notifications/read")
@auth_required()
def mark_read():
    db = get_db()
    db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (now_iso(), g.user["id"]))
    db.commit()
    return jsonify(ok=True)


@bp.get("/faq")
@auth_required()
def faq():
    cat = request.args.get("category")
    sql, args = ("SELECT * FROM faq WHERE category=?", (cat,)) if cat else ("SELECT * FROM faq", ())
    return jsonify(fetch_all(get_db(), sql, args))
