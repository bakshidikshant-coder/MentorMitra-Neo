import sqlite3

from flask import current_app, g

from common import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','hod','mentor','student')),
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teachers (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
  employee_code TEXT NOT NULL UNIQUE COLLATE NOCASE,
  department TEXT,
  capacity_override INTEGER CHECK (capacity_override IS NULL OR capacity_override >= 0),
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS students (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
  roll_no TEXT NOT NULL UNIQUE COLLATE NOCASE,
  programme TEXT NOT NULL,
  semester INTEGER NOT NULL,
  section TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_students_class ON students(programme, semester, section);

CREATE TABLE IF NOT EXISTS allocation_versions (
  id INTEGER PRIMARY KEY,
  session TEXT NOT NULL,
  filename TEXT,
  uploaded_by INTEGER REFERENCES users(id),
  uploaded_at TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_version ON allocation_versions(is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS subject_allocations (
  id INTEGER PRIMARY KEY,
  version_id INTEGER NOT NULL REFERENCES allocation_versions(id),
  teacher_id INTEGER NOT NULL REFERENCES teachers(id),
  subject TEXT NOT NULL,
  programme TEXT NOT NULL,
  semester INTEGER NOT NULL,
  section TEXT NOT NULL,
  UNIQUE (version_id, teacher_id, subject, programme, semester, section)
);
CREATE INDEX IF NOT EXISTS idx_alloc_class ON subject_allocations(version_id, programme, semester, section);

CREATE TABLE IF NOT EXISTS allocation_staging (
  token TEXT PRIMARY KEY,
  session TEXT NOT NULL,
  filename TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  rows_json TEXT NOT NULL,
  errors_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mentor_assignments (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  teacher_id INTEGER NOT NULL REFERENCES teachers(id),
  session TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('auto','manual')),
  note TEXT,
  assigned_by INTEGER REFERENCES users(id),
  assigned_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  ended_at TEXT,
  end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_mentor
  ON mentor_assignments(student_id, session) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_assign_teacher ON mentor_assignments(teacher_id, session, active);

CREATE TABLE IF NOT EXISTS requirements (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  session TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('exam_form','informed','sessional','fees')),
  status TEXT NOT NULL,
  deadline TEXT,
  remarks TEXT,
  amount_due REAL,
  claimed_at TEXT,
  claim_note TEXT,
  updated_by INTEGER REFERENCES users(id),
  updated_at TEXT NOT NULL,
  UNIQUE (student_id, session, type)
);

CREATE TABLE IF NOT EXISTS queries (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  requirement_id INTEGER REFERENCES requirements(id),
  category TEXT NOT NULL,
  subject TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','escalated','resolved')),
  escalated_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS query_messages (
  id INTEGER PRIMARY KEY,
  query_id INTEGER NOT NULL REFERENCES queries(id),
  author_user_id INTEGER NOT NULL REFERENCES users(id),
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  teacher_id INTEGER NOT NULL REFERENCES teachers(id),
  query_id INTEGER REFERENCES queries(id),
  requirement_id INTEGER REFERENCES requirements(id),
  action TEXT NOT NULL CHECK (action IN ('reminder','call','meeting','email','note')),
  note TEXT,
  outcome TEXT,
  next_followup_date TEXT,
  done INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_followups_teacher ON followups(teacher_id, done, next_followup_date);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  channel TEXT NOT NULL CHECK (channel IN ('in_app','email','sms')),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'delivered',   -- delivered | queued | failed
  created_at TEXT NOT NULL,
  read_at TEXT
);

CREATE TABLE IF NOT EXISTS faq (
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  actor_user_id INTEGER,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id TEXT,
  detail TEXT,
  created_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "current_session": "2026-27",
    "default_capacity": "25",      # max mentees per teacher unless overridden
    "due_soon_days": "7",          # "due soon" window for deadlines
    "stale_query_days": "7",       # open query older than this feeds the risk flag
}

DEFAULT_FAQ = [
    ("exam_form", "How do I submit my examination form?",
     "Fill the form on the examination portal before the deadline and tell your mentor once submitted."),
    ("fees", "I have paid only part of my fees.",
     "Partial payment is recorded as 'partly paid'. Clear the balance before the deadline or ask your mentor about instalments."),
    ("sessional", "I missed a sessional examination.",
     "Raise a query with the subject, date and reason. Your mentor will route it to the department for a decision."),
    ("other", "Who is my mentor?",
     "Your mentor is shown on your dashboard. If it is empty, the allocation has not been run yet."),
]


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    if not conn.execute("SELECT 1 FROM faq LIMIT 1").fetchone():
        conn.executemany("INSERT INTO faq(category, question, answer) VALUES (?,?,?)", DEFAULT_FAQ)
    conn.commit()
    conn.close()


def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS[key]


def audit(db, actor_user_id, action, entity=None, entity_id=None, detail=None):
    db.execute(
        "INSERT INTO audit_log(actor_user_id, action, entity, entity_id, detail, created_at) VALUES (?,?,?,?,?,?)",
        (actor_user_id, action, entity, str(entity_id) if entity_id is not None else None, detail, now_iso()),
    )
