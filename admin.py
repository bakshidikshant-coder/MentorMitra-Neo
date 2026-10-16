import csv
import io

from flask import Blueprint, g, jsonify, request

from common import ApiError, fetch_all, fetch_one, json_body, need, norm_code, now_iso, parse_date
from db import DEFAULT_SETTINGS, audit, get_db, get_setting
from security import auth_required, create_user
from services import allocation_upload, mentor_engine
from services import requirements as reqsvc

bp = Blueprint("admin", __name__, url_prefix="/api/admin")
admin_only = auth_required("admin")
admin_or_hod = auth_required("admin", "hod")


def session_name(db):
    return get_setting(db, "current_session")


# ---------- settings ----------
@bp.get("/settings")
@admin_or_hod
def get_settings():
    db = get_db()
    return jsonify({k: get_setting(db, k) for k in DEFAULT_SETTINGS})


@bp.put("/settings")
@admin_only
def put_settings():
    d, db = json_body(), get_db()
    for k, v in d.items():
        if k not in DEFAULT_SETTINGS:
            raise ApiError(f"Unknown setting '{k}'")
        if k != "current_session" and (not str(v).isdigit() or int(v) < 0):
            raise ApiError(f"{k} must be a non-negative integer")
        db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    audit(db, g.user["id"], "settings_update", "settings", None, str(d))
    db.commit()
    return get_settings()


# ---------- teachers ----------
@bp.post("/teachers")
@admin_only
def add_teacher():
    d, db = json_body(), get_db()
    need(d, "name", "email", "employee_code")
    cap = d.get("capacity_override")
    if cap is not None and (not isinstance(cap, int) or cap < 0):
        raise ApiError("capacity_override must be a non-negative integer")
    uid, temp = create_user(db, d["email"], d["name"], "mentor", d.get("password"))
    cur = db.execute("INSERT INTO teachers(user_id, employee_code, department, capacity_override) VALUES (?,?,?,?)",
                     (uid, d["employee_code"].strip(), d.get("department"), cap))
    audit(db, g.user["id"], "teacher_create", "teachers", cur.lastrowid)
    db.commit()
    return jsonify(id=cur.lastrowid, temp_password=temp), 201


@bp.get("/teachers")
@admin_or_hod
def list_teachers():
    db = get_db()
    sess = session_name(db)
    return jsonify(fetch_all(db, """
        SELECT t.id, t.employee_code, u.name, u.email, t.department, t.capacity_override, t.active,
          (SELECT COUNT(*) FROM mentor_assignments a WHERE a.teacher_id=t.id AND a.session=? AND a.active=1) AS mentees
        FROM teachers t JOIN users u ON u.id=t.user_id ORDER BY u.name""", (sess,)))


@bp.patch("/teachers/<int:tid>")
@admin_only
def patch_teacher(tid):
    d, db = json_body(), get_db()
    t = fetch_one(db, "SELECT * FROM teachers WHERE id=?", (tid,))
    if not t:
        raise ApiError("Teacher not found", 404)
    released = 0
    if "capacity_override" in d:
        cap = d["capacity_override"]
        if cap is not None and (not isinstance(cap, int) or cap < 0):
            raise ApiError("capacity_override must be a non-negative integer or null")
        db.execute("UPDATE teachers SET capacity_override=? WHERE id=?", (cap, tid))
    if "department" in d:
        db.execute("UPDATE teachers SET department=? WHERE id=?", (d["department"], tid))
    if "active" in d:
        db.execute("UPDATE teachers SET active=? WHERE id=?", (1 if d["active"] else 0, tid))
        if not d["active"]:   # teacher leaves: free their mentees; re-run allocation to reassign only them
            cur = db.execute("""UPDATE mentor_assignments SET active=0, ended_at=?, end_reason='teacher_inactive'
                                WHERE teacher_id=? AND active=1""", (now_iso(), tid))
            released = cur.rowcount
    audit(db, g.user["id"], "teacher_update", "teachers", tid, str(d))
    db.commit()
    return jsonify(ok=True, mentees_released=released)


# ---------- students ----------
def _insert_student(db, rec):
    for f in ("name", "email", "roll_no", "programme", "semester", "section"):
        if rec.get(f) in (None, ""):
            raise ApiError(f"Missing {f}")
    try:
        sem = int(float(str(rec["semester"])))
    except ValueError:
        raise ApiError(f"Semester '{rec['semester']}' is not a number")
    if db.execute("SELECT 1 FROM students WHERE roll_no=?", (str(rec["roll_no"]).strip(),)).fetchone():
        raise ApiError(f"Roll number {rec['roll_no']} already exists", 409)
    uid, temp = create_user(db, rec["email"], rec["name"], "student", rec.get("password"))
    cur = db.execute("INSERT INTO students(user_id, roll_no, programme, semester, section) VALUES (?,?,?,?,?)",
                     (uid, str(rec["roll_no"]).strip(), norm_code(rec["programme"]), sem, norm_code(rec["section"])))
    return cur.lastrowid, temp


@bp.post("/students")
@admin_only
def add_student():
    db = get_db()
    sid, temp = _insert_student(db, json_body())
    audit(db, g.user["id"], "student_create", "students", sid)
    db.commit()
    return jsonify(id=sid, temp_password=temp), 201


@bp.post("/students/import")
@admin_only
def import_students():
    """CSV/XLSX with: roll_no, name, email, programme, semester, section."""
    f = request.files.get("file")
    if not f:
        raise ApiError("Attach a file in the 'file' form field")
    db = get_db()
    content = f.read()
    if f.filename.lower().endswith(".csv"):
        grid = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    else:
        raise ApiError("Student import currently accepts .csv files")
    created, errors, creds = 0, [], []
    for i, rec in enumerate(grid, start=2):
        rec = {str(k).strip().lower(): (v or "").strip() for k, v in rec.items() if k}
        try:
            db.execute("SAVEPOINT row")
            sid, temp = _insert_student(db, rec)
            db.execute("RELEASE row")
            created += 1
            creds.append({"roll_no": rec["roll_no"], "email": rec["email"], "temp_password": temp})
        except ApiError as e:
            db.execute("ROLLBACK TO row")
            errors.append({"row": i, "message": e.message})
    audit(db, g.user["id"], "student_import", "students", None, f"created={created} errors={len(errors)}")
    db.commit()
    return jsonify(created=created, errors=errors, credentials=creds)


@bp.get("/students")
@admin_or_hod
def list_students():
    db = get_db()
    sess = session_name(db)
    return jsonify(fetch_all(db, """
        SELECT s.id, s.roll_no, u.name, u.email, s.programme, s.semester, s.section, s.active,
               a.teacher_id AS mentor_id, mu.name AS mentor_name
        FROM students s JOIN users u ON u.id=s.user_id
        LEFT JOIN mentor_assignments a ON a.student_id=s.id AND a.session=? AND a.active=1
        LEFT JOIN teachers t ON t.id=a.teacher_id LEFT JOIN users mu ON mu.id=t.user_id
        ORDER BY s.roll_no""", (sess,)))


@bp.patch("/students/<int:sid>")
@admin_only
def patch_student(sid):
    d, db = json_body(), get_db()
    if not fetch_one(db, "SELECT 1 FROM students WHERE id=?", (sid,)):
        raise ApiError("Student not found", 404)
    for f, conv in (("programme", norm_code), ("section", norm_code), ("semester", int)):
        if f in d:
            db.execute(f"UPDATE students SET {f}=? WHERE id=?", (conv(d[f]), sid))
    if "active" in d:
        db.execute("UPDATE students SET active=? WHERE id=?", (1 if d["active"] else 0, sid))
    lost = mentor_engine.release_if_ineligible(db, sid, session_name(db))
    audit(db, g.user["id"], "student_update", "students", sid, str(d))
    db.commit()
    return jsonify(ok=True, mentor_released=lost,
                   hint="Run /api/admin/mentors/allocate to assign a new eligible mentor" if lost else None)


# ---------- subject allocation upload ----------
@bp.post("/allocations/preview")
@admin_only
def allocation_preview():
    f = request.files.get("file")
    if not f:
        raise ApiError("Attach a file in the 'file' form field")
    db = get_db()
    res = allocation_upload.stage_upload(db, g.user["id"], request.form.get("session") or session_name(db),
                                         f.filename, f.read())
    db.commit()
    return jsonify(res), (200 if res["can_confirm"] else 422)


@bp.post("/allocations/confirm")
@admin_only
def allocation_confirm():
    d, db = json_body(), get_db()
    need(d, "token")
    vid, n = allocation_upload.confirm_upload(db, g.user["id"], d["token"])
    audit(db, g.user["id"], "allocation_upload", "allocation_versions", vid, f"{n} rows")
    db.commit()
    return jsonify(version_id=vid, rows=n,
                   hint="Run mentor allocation to apply the new eligibility rules; ineligible mentors will be released.")


@bp.get("/allocations")
@admin_or_hod
def active_allocation():
    db = get_db()
    versions = fetch_all(db, "SELECT * FROM allocation_versions ORDER BY id DESC")
    active = next((v for v in versions if v["is_active"]), None)
    rows = fetch_all(db, """SELECT sa.id, u.name AS teacher, t.employee_code, sa.subject, sa.programme, sa.semester, sa.section
                            FROM subject_allocations sa JOIN teachers t ON t.id=sa.teacher_id JOIN users u ON u.id=t.user_id
                            WHERE sa.version_id=? ORDER BY sa.programme, sa.semester, sa.section, u.name""",
                     (active["id"],)) if active else []
    return jsonify(versions=versions, active_rows=rows)


# ---------- mentor allocation ----------
@bp.post("/mentors/allocate")
@admin_only
def allocate():
    d, db = request.get_json(silent=True) or {}, get_db()
    sess = session_name(db)
    plan = mentor_engine.plan_allocation(db, sess, keep_existing=bool(d.get("keep_existing", True)))
    dry = bool(d.get("dry_run", True))   # safe default: preview only
    if not dry:
        mentor_engine.apply_plan(db, plan, sess, g.user["id"])
        db.commit()
    plan["creates"] = [{"student_id": s, "teacher_id": t} for s, t in plan["creates"]]
    plan["releases"] = [{"student_id": s, "reason": why} for _a, s, why in plan["releases"]]
    plan["dry_run"], plan["session"] = dry, sess
    return jsonify(plan)


@bp.post("/mentors/reassign")
@admin_only
def reassign():
    d, db = json_body(), get_db()
    need(d, "student_id", "teacher_id")
    mentor_engine.reassign(db, session_name(db), d["student_id"], d["teacher_id"], g.user["id"],
                           d.get("note"), bool(d.get("force_capacity")))
    db.commit()
    return jsonify(ok=True)


@bp.get("/mentors/summary")
@admin_or_hod
def mentor_summary():
    db = get_db()
    sess = session_name(db)
    cap = mentor_engine.capacity_map(db)
    rows = fetch_all(db, """SELECT t.id AS teacher_id, u.name, COUNT(a.id) AS mentees FROM teachers t
        JOIN users u ON u.id=t.user_id
        LEFT JOIN mentor_assignments a ON a.teacher_id=t.id AND a.session=? AND a.active=1
        WHERE t.active=1 GROUP BY t.id ORDER BY u.name""", (sess,))
    for r in rows:
        r["capacity"] = cap[r["teacher_id"]]
    unassigned = fetch_all(db, """SELECT s.id, s.roll_no, s.programme, s.semester, s.section FROM students s
        WHERE s.active=1 AND NOT EXISTS (SELECT 1 FROM mentor_assignments a WHERE a.student_id=s.id AND a.session=? AND a.active=1)
        ORDER BY s.roll_no""", (sess,))
    return jsonify(session=sess, teachers=rows, unassigned_students=unassigned)


# ---------- requirements / faq / audit ----------
@bp.post("/requirements/initialize")
@admin_only
def init_requirements():
    d, db = json_body(), get_db()
    deadlines = {t: parse_date((d.get("deadlines") or {}).get(t), f"deadlines.{t}") for t in reqsvc.STATUSES}
    n = reqsvc.initialize(db, session_name(db), deadlines, g.user["id"])
    audit(db, g.user["id"], "requirements_init", "requirements", None, f"created={n}")
    db.commit()
    return jsonify(created=n), 201


@bp.post("/faq")
@admin_only
def add_faq():
    d, db = json_body(), get_db()
    need(d, "category", "question", "answer")
    cur = db.execute("INSERT INTO faq(category, question, answer) VALUES (?,?,?)", (d["category"], d["question"], d["answer"]))
    db.commit()
    return jsonify(id=cur.lastrowid), 201


@bp.get("/audit")
@admin_only
def audit_log():
    limit = min(int(request.args.get("limit", 100)), 500)
    return jsonify(fetch_all(get_db(), "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)))
