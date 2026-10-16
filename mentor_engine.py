"""Fair mentor allocation. HARD RULE: a teacher is eligible for a class only if the
active subject-allocation version lists them for that programme + semester + section."""
from common import ApiError, fetch_all, fetch_one, now_iso
from db import audit, get_setting


def class_key(s):
    return (s["programme"], s["semester"], s["section"])


def eligibility_map(db):
    v = fetch_one(db, "SELECT id FROM allocation_versions WHERE is_active=1")
    if not v:
        raise ApiError("No subject allocation has been uploaded yet", 409)
    m = {}
    for r in db.execute(
        """SELECT DISTINCT sa.programme, sa.semester, sa.section, sa.teacher_id
           FROM subject_allocations sa JOIN teachers t ON t.id=sa.teacher_id
           WHERE sa.version_id=? AND t.active=1""", (v["id"],)):
        m.setdefault((r["programme"], r["semester"], r["section"]), set()).add(r["teacher_id"])
    return m


def capacity_map(db):
    default = int(get_setting(db, "default_capacity"))
    return {t["id"]: (t["capacity_override"] if t["capacity_override"] is not None else default)
            for t in fetch_all(db, "SELECT id, capacity_override FROM teachers WHERE active=1")}


def plan_allocation(db, session, keep_existing=True):
    elig, cap = eligibility_map(db), capacity_map(db)
    students = fetch_all(db, "SELECT id, roll_no, programme, semester, section FROM students WHERE active=1 ORDER BY roll_no")
    by_id = {s["id"]: s for s in students}
    current = {a["student_id"]: a for a in fetch_all(
        db, "SELECT * FROM mentor_assignments WHERE active=1 AND session=?", (session,))}

    kept, releases = {}, []          # kept: teacher_id -> [assignment]; releases: (assignment_id, student_id, why)
    for sid, a in current.items():
        s = by_id.get(sid)
        if s is None:
            releases.append((a["id"], sid, "student_inactive"))
        elif a["teacher_id"] not in elig.get(class_key(s), ()):
            releases.append((a["id"], sid, "teacher_not_eligible"))
        elif a["reason"] == "auto" and not keep_existing:
            releases.append((a["id"], sid, "rebalance"))
        else:
            kept.setdefault(a["teacher_id"], []).append(a)

    for tid, lst in kept.items():     # capacity lowered -> trim newest auto assignments only
        excess = len(lst) - cap.get(tid, 0)
        if excess > 0:
            autos = sorted((a for a in lst if a["reason"] == "auto"), key=lambda a: (a["assigned_at"], a["id"]), reverse=True)
            for a in autos[:excess]:
                lst.remove(a)
                releases.append((a["id"], a["student_id"], "over_capacity"))

    load = {t: len(kept.get(t, [])) for t in cap}
    load_before = {t: sum(1 for a in current.values() if a["teacher_id"] == t) for t in cap}
    done = {a["student_id"] for lst in kept.values() for a in lst}
    classes = {}
    for s in students:
        if s["id"] not in done:
            classes.setdefault(class_key(s), []).append(s)

    def slack(k):  # remaining room among eligible teachers minus students needing a mentor
        return sum(max(0, cap[t] - load[t]) for t in elig.get(k, ())) - len(classes[k])

    creates, unassigned = [], []
    for k in sorted(classes, key=lambda k: (len(elig.get(k, ())), slack(k), k)):   # most constrained first
        for s in classes[k]:
            cands = [t for t in elig.get(k, ()) if load[t] < cap[t]]
            if not cands:
                unassigned.append({"student_id": s["id"], "roll_no": s["roll_no"], "class": list(k),
                                   "reason": "no_eligible_teacher" if not elig.get(k) else "capacity_exhausted"})
                continue
            t = min(cands, key=lambda t: (load[t] / cap[t], load[t], t))
            load[t] += 1
            creates.append((s["id"], t))

    names = {r["id"]: r["name"] for r in db.execute("SELECT t.id, u.name FROM teachers t JOIN users u ON u.id=t.user_id")}
    return {
        "creates": creates, "releases": releases, "unassigned": unassigned,
        "summary": {"students": len(students), "kept": sum(len(v) for v in kept.values()),
                    "to_assign": len(creates), "to_release": len(releases), "unassigned": len(unassigned)},
        "distribution": sorted(
            ({"teacher_id": t, "name": names[t], "capacity": cap[t], "load_before": load_before[t], "load_after": load[t]}
             for t in cap), key=lambda d: d["name"]),
    }


def apply_plan(db, plan, session, actor_user_id):
    ts = now_iso()
    for aid, _sid, why in plan["releases"]:
        db.execute("UPDATE mentor_assignments SET active=0, ended_at=?, end_reason=? WHERE id=?", (ts, why, aid))
    for sid, tid in plan["creates"]:
        db.execute(
            "INSERT INTO mentor_assignments(student_id, teacher_id, session, reason, assigned_by, assigned_at) VALUES (?,?,?,?,?,?)",
            (sid, tid, session, "auto", actor_user_id, ts))
    audit(db, actor_user_id, "mentor_allocate", "mentor_assignments", None,
          f"assigned={len(plan['creates'])} released={len(plan['releases'])} unassigned={len(plan['unassigned'])}")


def reassign(db, session, student_id, teacher_id, actor_user_id, note=None, force_capacity=False):
    s = fetch_one(db, "SELECT * FROM students WHERE id=? AND active=1", (student_id,))
    if not s:
        raise ApiError("Student not found", 404)
    if teacher_id not in eligibility_map(db).get(class_key(s), ()):
        raise ApiError("That teacher does not teach this student's class and cannot be their mentor", 422)
    cap = capacity_map(db)[teacher_id]
    load = fetch_one(db, "SELECT COUNT(*) c FROM mentor_assignments WHERE teacher_id=? AND session=? AND active=1",
                     (teacher_id, session))["c"]
    if load >= cap and not force_capacity:
        raise ApiError(f"Teacher is at capacity ({load}/{cap}); pass force_capacity=true to override", 409)
    ts = now_iso()
    db.execute("UPDATE mentor_assignments SET active=0, ended_at=?, end_reason='manual_reassign' WHERE student_id=? AND session=? AND active=1",
               (ts, student_id, session))
    db.execute("INSERT INTO mentor_assignments(student_id, teacher_id, session, reason, note, assigned_by, assigned_at) VALUES (?,?,?,?,?,?,?)",
               (student_id, teacher_id, session, "manual", note, actor_user_id, ts))
    audit(db, actor_user_id, "mentor_reassign", "students", student_id, f"to teacher {teacher_id}; {note or ''}")


def release_if_ineligible(db, student_id, session, why="class_changed"):
    """Call after a student's programme/semester/section changes (or a teacher is deactivated)."""
    s = fetch_one(db, "SELECT * FROM students WHERE id=?", (student_id,))
    a = fetch_one(db, "SELECT * FROM mentor_assignments WHERE student_id=? AND session=? AND active=1", (student_id, session))
    if not a or not s:
        return False
    try:
        ok = a["teacher_id"] in eligibility_map(db).get(class_key(s), ())
    except ApiError:
        ok = True   # no allocation uploaded: nothing to compare with
    if not ok:
        db.execute("UPDATE mentor_assignments SET active=0, ended_at=?, end_reason=? WHERE id=?", (now_iso(), why, a["id"]))
    return not ok
