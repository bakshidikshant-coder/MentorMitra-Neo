# Student Mentoring System – Backend

Flask + SQLite (stdlib `sqlite3`) + JWT. Roles: `admin`, `hod` (read-only reports), `mentor`, `student`.

## Run (backend + web portal together)
The web portal lives in `frontend/` (plain HTML/CSS/JS, no build step) and is served by Flask at `/`.
After starting the server open http://localhost:5000 and sign in. Tabs depend on role:
admin (Allocation, Setup), mentor (Dashboard, Mentees, Queries, Calendar, Notifications), student (Overview, Queries, Notifications).

    pip install -r requirements.txt
    export MENTOR_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')"
    python manage.py create-admin admin@college.edu "Admin Name"
    python app.py                      # dev server on :5000
    # production: gunicorn -w 2 "app:create_app()"
    python -m unittest tests.test_api -v

Env vars: `MENTOR_DB` (path), `MENTOR_SECRET`, `MENTOR_TOKEN_TTL_HOURS`, `MENTOR_MAX_UPLOAD_MB`, `MENTOR_CORS_ORIGIN`.

## Typical admin setup order
1. `POST /api/admin/teachers`, `POST /api/admin/students` or `/students/import` (CSV: roll_no,name,email,programme,semester,section)
2. `PUT /api/admin/settings` – `default_capacity`, `due_soon_days`, `current_session`
3. `POST /api/admin/allocations/preview` (multipart `file`: CSV/XLSX with teacher,subject,programme,semester,section) → fix errors → `POST /allocations/confirm {token}`
4. `POST /api/admin/mentors/allocate {"dry_run": true}` → review → `{"dry_run": false}`
5. `POST /api/admin/requirements/initialize {"deadlines": {"exam_form":"2026-11-01","informed":"...","sessional":"...","fees":"..."}}`

## Endpoints
**Auth**: `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/change-password`

**Admin** (`/api/admin`): `settings` GET/PUT · `teachers` POST/GET, `teachers/<id>` PATCH (capacity_override, department, active=false frees mentees) · `students` POST/GET, `students/import`, `students/<id>` PATCH (class change re-checks mentor eligibility) · `allocations/preview|confirm`, `allocations` GET · `mentors/allocate|reassign|summary` · `requirements/initialize` · `faq` POST · `audit` GET

**Student** (`/api/student`): `dashboard` · `requirements/<id>/claim` · `queries` POST/GET, `queries/<id>` GET, `queries/<id>/messages` POST · `notifications` GET, `notifications/read` POST · `faq`

**Mentor** (`/api/mentor`): `dashboard` · `mentees` (filters: `risk`, `urgency`, `type`+`status`, `q`) · `mentees/<student_id>` · `requirements/<id>` PATCH · `reminders` POST (bulk, channels in_app/email/sms) · `followups` POST, `followups/<id>` PATCH · `calendar` · `queries` GET, `queries/<id>` GET/PATCH (open→in_progress→escalated→resolved), `queries/<id>/messages` POST

## Key rules enforced
- **Eligibility is a hard filter**: a mentor must appear in the active allocation upload for the student's programme + semester + section. Enforced in auto-allocation *and* manual reassignment (admin cannot override it).
- **Fair balancing**: most-constrained classes first; each student goes to the eligible teacher with the lowest load/capacity ratio; capacity = per-teacher override or global default; overflow is reported as `unassigned` (`no_eligible_teacher` / `capacity_exhausted`), never force-assigned.
- **Stable re-runs**: `keep_existing` (default true) only touches unassigned students plus those released by a capacity cut, teacher leaving, class change or a new upload that makes the mentor ineligible. Manual assignments are never auto-moved (except when no longer eligible).
- Allocation defaults to `dry_run: true`. Every admin mutation goes to `audit_log`.
- Requirement statuses: exam_form `not_submitted|submitted`, informed `not_informed|informed`, sessional `pending|completed`, fees `pending|partly_paid|paid` (+`amount_due`). Urgency computed live: `overdue | due_soon | on_track | no_deadline | done`.
- Student "claim" does not mark an item done (except `informed`); the mentor verifies.
- Risk flag = score + reasons (overdue items, unpaid fees past deadline, ≥2 open queries, stale queries, reminders ignored).

## Known limits / next steps
- Email/SMS are written to `notifications` with `status='queued'`; attach a worker/gateway to deliver them.
- Fees are entered by the mentor/admin; add an import from the accounts office if available.
- SQLite is fine for one college; for multi-instance deployment move to PostgreSQL (SQL is plain, no SQLite-only features except the partial unique indexes, which Postgres also supports).
- Rate limiting and login lockout are not implemented; put the API behind a reverse proxy with HTTPS.
