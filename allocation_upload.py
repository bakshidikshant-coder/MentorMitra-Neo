"""Parse + validate the approved subject-allocation file (CSV or XLSX)."""
import csv
import io
import json
import uuid

from common import ApiError, fetch_all, norm_code, now_iso

REQUIRED = ["teacher", "subject", "programme", "semester", "section"]
ALIASES = {
    "faculty": "teacher", "teacher_name": "teacher", "teacher_code": "teacher",
    "employee_code": "teacher", "faculty_name": "teacher", "teacher_email": "teacher",
    "course": "subject", "subject_name": "subject",
    "program": "programme", "sem": "semester",
    "class": "section", "division": "section", "sec": "section",
}


def _header(h):
    h = str(h or "").strip().lower().replace(" ", "_")
    return ALIASES.get(h, h)


def read_table(filename: str, content: bytes):
    """Returns (rows, row_numbers) where rows are dicts keyed by normalised header."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        grid = list(csv.reader(io.StringIO(text)))
    elif name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:
            raise ApiError("Could not read the Excel file; make sure it is a valid .xlsx")
        grid = [["" if c is None else c for c in row] for row in wb.worksheets[0].iter_rows(values_only=True)]
    else:
        raise ApiError("Unsupported file type; upload a .csv or .xlsx file")
    if not grid:
        raise ApiError("The file is empty")
    headers = [_header(h) for h in grid[0]]
    missing = [c for c in REQUIRED if c not in headers]
    if missing:
        raise ApiError("Missing required column(s): " + ", ".join(missing), columns_found=headers)
    rows = []
    for i, raw in enumerate(grid[1:], start=2):
        if not any(str(c).strip() for c in raw):
            continue
        rec = {headers[j]: raw[j] if j < len(raw) else "" for j in range(len(headers))}
        rec["_row"] = i
        rows.append(rec)
    if not rows:
        raise ApiError("The file has a header but no data rows")
    return rows


def _teacher_lookup(db):
    by_key, names = {}, {}
    for t in fetch_all(db, """SELECT t.id, t.employee_code, u.email, u.name FROM teachers t
                              JOIN users u ON u.id=t.user_id WHERE t.active=1"""):
        by_key[t["employee_code"].lower()] = t["id"]
        by_key[t["email"].lower()] = t["id"]
        names.setdefault(t["name"].strip().lower(), []).append(t["id"])
    return by_key, names


def validate(db, rows):
    by_key, names = _teacher_lookup(db)
    errors, warnings, clean, seen = [], [], [], {}
    class_has_students = {
        (r["programme"], r["semester"], r["section"])
        for r in fetch_all(db, "SELECT DISTINCT programme, semester, section FROM students WHERE active=1")
    }
    warned_classes = set()
    for r in rows:
        n = r["_row"]
        def err(field, msg):
            errors.append({"row": n, "field": field, "message": msg})
        ref = str(r["teacher"]).strip()
        tid = by_key.get(ref.lower())
        if tid is None:
            matches = names.get(ref.lower(), [])
            if len(matches) == 1:
                tid = matches[0]
            elif len(matches) > 1:
                err("teacher", f"'{ref}' matches several teachers; use the employee code or email")
            else:
                err("teacher", f"Teacher '{ref}' not found among active teachers")
        subject = str(r["subject"]).strip()
        if not subject:
            err("subject", "Subject is empty")
        programme, section = norm_code(r["programme"]), norm_code(r["section"])
        if not programme:
            err("programme", "Programme is empty")
        if not section:
            err("section", "Section is empty")
        try:
            sem = int(float(str(r["semester"]).strip()))
            if not 1 <= sem <= 12:
                raise ValueError
        except ValueError:
            sem = None
            err("semester", f"Semester '{r['semester']}' must be a whole number from 1 to 12")
        if tid is None or sem is None or not subject or not programme or not section:
            continue
        key = (tid, subject.lower(), programme, sem, section)
        if key in seen:
            err("row", f"Duplicate of row {seen[key]}")
            continue
        seen[key] = n
        cls = (programme, sem, section)
        if cls not in class_has_students and cls not in warned_classes:
            warned_classes.add(cls)
            warnings.append({"row": n, "message": f"No active students found for class {programme} sem {sem} sec {section}"})
        clean.append({"teacher_id": tid, "subject": subject, "programme": programme, "semester": sem, "section": section})
    return clean, errors, warnings


def stage_upload(db, user_id, session, filename, content):
    rows = read_table(filename, content)
    clean, errors, warnings = validate(db, rows)
    token = uuid.uuid4().hex
    db.execute(
        "INSERT INTO allocation_staging(token, session, filename, created_by, created_at, rows_json, errors_json) VALUES (?,?,?,?,?,?,?)",
        (token, session, filename, user_id, now_iso(), json.dumps(clean), json.dumps(errors)),
    )
    classes = {(c["programme"], c["semester"], c["section"]) for c in clean}
    return {
        "token": token, "valid_rows": len(clean), "error_count": len(errors), "errors": errors,
        "warnings": warnings, "classes_covered": len(classes), "can_confirm": not errors and bool(clean),
        "preview": clean[:20],
    }


def confirm_upload(db, user_id, token):
    st = db.execute("SELECT * FROM allocation_staging WHERE token=?", (token,)).fetchone()
    if not st:
        raise ApiError("Unknown or already used upload token", 404)
    if json.loads(st["errors_json"]):
        raise ApiError("This upload has validation errors; fix the file and upload again", 422)
    rows = json.loads(st["rows_json"])
    db.execute("UPDATE allocation_versions SET is_active=0 WHERE is_active=1")
    cur = db.execute(
        "INSERT INTO allocation_versions(session, filename, uploaded_by, uploaded_at, row_count, is_active) VALUES (?,?,?,?,?,1)",
        (st["session"], st["filename"], user_id, now_iso(), len(rows)),
    )
    vid = cur.lastrowid
    db.executemany(
        "INSERT INTO subject_allocations(version_id, teacher_id, subject, programme, semester, section) VALUES (?,?,?,?,?,?)",
        [(vid, r["teacher_id"], r["subject"], r["programme"], r["semester"], r["section"]) for r in rows],
    )
    db.execute("DELETE FROM allocation_staging WHERE token=?", (token,))
    return vid, len(rows)
