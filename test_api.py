import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app          # noqa: E402
import db as database               # noqa: E402
from security import create_user    # noqa: E402

TODAY = "2026-10-16"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.app = create_app({"DATABASE": cls.tmp.name, "SECRET_KEY": "test-secret", "TODAY": TODAY, "TESTING": True})
        cls.c = cls.app.test_client()
        with cls.app.app_context():
            conn = database.get_db()
            create_user(conn, "admin@college.edu", "Admin", "admin", "adminpass1")
            conn.commit()
        cls.admin = cls.login("admin@college.edu", "adminpass1")

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.tmp.name)

    @classmethod
    def login(cls, email, pw):
        r = cls.c.post("/api/auth/login", json={"email": email, "password": pw})
        assert r.status_code == 200, r.get_json()
        return {"Authorization": "Bearer " + r.get_json()["token"]}

    def call(self, method, url, headers, **kw):
        return getattr(self.c, method)(url, headers=headers, **kw)


class TestWholeFlow(Base):
    """Tests run in alphabetical order, each step builds on the previous one."""

    def test_01_setup_people(self):
        self.call("put", "/api/admin/settings", self.admin, json={"default_capacity": 8})
        T = {}
        for code, name, cap in (("T1", "Teacher One", None), ("T2", "Teacher Two", None), ("T3", "Teacher Three", 2)):
            r = self.call("post", "/api/admin/teachers", self.admin, json={
                "name": name, "email": f"{code.lower()}@college.edu", "employee_code": code,
                "department": "CSE", "capacity_override": cap, "password": "teacherpass1"})
            self.assertEqual(r.status_code, 201)
            T[code] = r.get_json()["id"]
        self.__class__.T = T
        rows = ["roll_no,name,email,programme,semester,section"]
        n = 0
        for sec, count in (("A", 10), ("B", 6), ("C", 5)):
            for i in range(count):
                n += 1
                rows.append(f"R{n:03d},Student {n},s{n}@college.edu,btech,3,{sec.lower()}")
        r = self.call("post", "/api/admin/students/import", self.admin,
                      data={"file": (io.BytesIO("\n".join(rows).encode()), "students.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.get_json()["created"], 21, r.get_json())
        self.__class__.creds = {c["roll_no"]: c["temp_password"] for c in r.get_json()["credentials"]}

    def test_02_upload_validation_errors(self):
        bad = ("teacher,subject,programme,semester,section\n"
               "T1,Maths,BTECH,3,A\nT1,Maths,btech,3,a\nGhost,Physics,BTECH,3,A\nT2,Chem,BTECH,99,A\n")
        r = self.call("post", "/api/admin/allocations/preview", self.admin,
                      data={"file": (io.BytesIO(bad.encode()), "alloc.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)
        body = r.get_json()
        self.assertFalse(body["can_confirm"])
        msgs = " | ".join(e["message"] for e in body["errors"])
        self.assertIn("Duplicate of row 2", msgs)
        self.assertIn("not found", msgs)
        self.assertIn("Semester", msgs)
        self.assertEqual(self.call("post", "/api/admin/allocations/confirm", self.admin, json={"token": body["token"]}).status_code, 422)
        r = self.call("post", "/api/admin/allocations/preview", self.admin,
                      data={"file": (io.BytesIO(b"teacher,subject\nT1,Maths\n"), "a.csv")}, content_type="multipart/form-data")
        self.assertIn("Missing required column", r.get_json()["error"])

    def test_03_upload_and_allocate(self):
        good = ("teacher,subject,programme,semester,section\n"
                "T1,Maths,BTECH,3,A\nT1,Physics,BTECH,3,B\nT2,Chem,BTECH,3,A\nT3,English,BTECH,3,C\n")
        r = self.call("post", "/api/admin/allocations/preview", self.admin,
                      data={"file": (io.BytesIO(good.encode()), "alloc.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.call("post", "/api/admin/allocations/confirm", self.admin, json={"token": r.get_json()["token"]}).status_code, 200)

        # dry run changes nothing
        dry = self.call("post", "/api/admin/mentors/allocate", self.admin, json={}).get_json()
        self.assertTrue(dry["dry_run"])
        self.assertEqual(len(self.call("get", "/api/admin/mentors/summary", self.admin).get_json()["unassigned_students"]), 21)
        self.assertEqual(dry["summary"]["to_assign"], 18)       # C has only 2 slots (T3 capacity 2): 3 unassigned
        self.assertEqual(dry["summary"]["unassigned"], 3)
        self.assertTrue(all(u["reason"] == "capacity_exhausted" for u in dry["unassigned"]))

        self.call("post", "/api/admin/mentors/allocate", self.admin, json={"dry_run": False})
        students = {s["roll_no"]: s for s in self.call("get", "/api/admin/students", self.admin).get_json()}
        T = self.T
        # HARD RULE: mentor must teach the student's class
        allowed = {"A": {T["T1"], T["T2"]}, "B": {T["T1"]}, "C": {T["T3"]}}
        sec_of = {f"R{i:03d}": ("A" if i <= 10 else "B" if i <= 16 else "C") for i in range(1, 22)}
        for roll, s in students.items():
            if s["mentor_id"] is not None:
                self.assertIn(s["mentor_id"], allowed[sec_of[roll]], roll)
        summ = {t["teacher_id"]: t for t in self.call("get", "/api/admin/mentors/summary", self.admin).get_json()["teachers"]}
        for t in summ.values():
            self.assertLessEqual(t["mentees"], t["capacity"])
        self.assertEqual(summ[T["T3"]]["mentees"], 2)
        self.assertEqual(summ[T["T1"]]["mentees"] + summ[T["T2"]]["mentees"], 16)
        self.assertLessEqual(abs(summ[T["T1"]]["mentees"] - summ[T["T2"]]["mentees"]), 4)

        # re-run keeps existing assignments (no reshuffle)
        again = self.call("post", "/api/admin/mentors/allocate", self.admin, json={"dry_run": False}).get_json()
        self.assertEqual(again["summary"]["to_assign"], 0)
        self.assertEqual(again["summary"]["to_release"], 0)

    def test_04_manual_reassign_rules(self):
        students = {s["roll_no"]: s for s in self.call("get", "/api/admin/students", self.admin).get_json()}
        b_student = students["R012"]            # section B: only T1 teaches it
        r = self.call("post", "/api/admin/mentors/reassign", self.admin, json={"student_id": b_student["id"], "teacher_id": self.T["T2"]})
        self.assertEqual(r.status_code, 422)    # ineligible, even for admin
        self.assertIn("does not teach", r.get_json()["error"])

    def test_05_requirements_student_and_mentor_flow(self):
        r = self.call("post", "/api/admin/requirements/initialize", self.admin, json={"deadlines": {
            "exam_form": "2026-10-10", "informed": "2026-10-12", "sessional": "2026-10-20", "fees": "2026-11-30"}})
        self.assertEqual(r.get_json()["created"], 84)

        students = self.call("get", "/api/admin/students", self.admin).get_json()
        stu = next(s for s in students if s["mentor_id"] == self.T["T1"])
        mentor = self.login("t1@college.edu", "teacherpass1")
        student = self.login(stu["email"], self.creds[stu["roll_no"]])

        dash = self.call("get", "/api/student/dashboard", student).get_json()
        by = {x["type"]: x for x in dash["requirements"]}
        self.assertEqual(by["exam_form"]["urgency"], "overdue")
        self.assertEqual(by["sessional"]["urgency"], "due_soon")
        self.assertEqual(by["fees"]["urgency"], "on_track")
        self.assertEqual(dash["mentor"]["name"], "Teacher One")

        # student claims exam form -> not done until mentor verifies
        self.assertEqual(self.call("post", f"/api/student/requirements/{by['exam_form']['id']}/claim", student, json={"note": "done"}).status_code, 200)
        m = self.call("get", f"/api/mentor/mentees/{stu['id']}", mentor).get_json()
        ef = next(x for x in m["requirements"] if x["type"] == "exam_form")
        self.assertEqual((ef["status"], ef["urgency"]), ("not_submitted", "overdue"))
        self.assertIsNotNone(ef["claimed_at"])

        # mentor verifies; invalid statuses rejected; partly_paid needs amount
        self.assertEqual(self.call("patch", f"/api/mentor/requirements/{ef['id']}", mentor, json={"status": "submitted"}).status_code, 200)
        fee = next(x for x in m["requirements"] if x["type"] == "fees")
        self.assertEqual(self.call("patch", f"/api/mentor/requirements/{fee['id']}", mentor, json={"status": "bogus"}).status_code, 400)
        self.assertEqual(self.call("patch", f"/api/mentor/requirements/{fee['id']}", mentor, json={"status": "partly_paid"}).status_code, 400)
        self.assertEqual(self.call("patch", f"/api/mentor/requirements/{fee['id']}", mentor,
                                   json={"status": "partly_paid", "amount_due": 5000, "remarks": "Instalment agreed"}).status_code, 200)
        m = self.call("get", f"/api/mentor/mentees/{stu['id']}", mentor).get_json()
        fee = next(x for x in m["requirements"] if x["type"] == "fees")
        self.assertEqual((fee["status"], fee["amount_due"]), ("partly_paid", 5000))
        self.assertEqual(next(x for x in m["requirements"] if x["type"] == "exam_form")["urgency"], "done")

        # reminders + follow-ups + calendar
        r = self.call("post", "/api/mentor/reminders", mentor, json={
            "student_ids": [stu["id"]], "channels": ["in_app", "sms"], "next_followup_date": "2026-10-14"}).get_json()
        self.assertEqual(r["sent"], [stu["id"]])
        notes = self.call("get", "/api/student/notifications", student).get_json()
        self.assertTrue(any("Reminder" in n["title"] for n in notes))
        cal = self.call("get", "/api/mentor/calendar", mentor).get_json()
        self.assertEqual(len(cal["missed"]), 1)          # 14 Oct < today
        fid = cal["missed"][0]["id"]
        self.call("patch", f"/api/mentor/followups/{fid}", mentor, json={"done": True, "outcome": "Student will pay Friday"})
        self.assertEqual(len(self.call("get", "/api/mentor/calendar", mentor).get_json()["missed"]), 0)

        # queries: raise -> FAQ suggestions -> reply -> escalate -> resolve
        q = self.call("post", "/api/student/queries", student, json={
            "category": "fees", "subject": "Scholarship delay", "description": "My scholarship has not arrived yet."})
        self.assertEqual(q.status_code, 201)
        self.assertTrue(q.get_json()["suggested_answers"])
        qid = q.get_json()["id"]
        self.assertEqual(len(self.call("get", "/api/mentor/queries?status=open", mentor).get_json()), 1)
        self.call("post", f"/api/mentor/queries/{qid}/messages", mentor, json={"body": "Checking with accounts."})
        self.assertEqual(self.call("get", f"/api/mentor/queries/{qid}", mentor).get_json()["status"], "in_progress")
        self.assertEqual(self.call("patch", f"/api/mentor/queries/{qid}", mentor, json={"status": "escalated"}).status_code, 400)
        self.assertEqual(self.call("patch", f"/api/mentor/queries/{qid}", mentor, json={"status": "escalated", "escalated_to": "accounts"}).status_code, 200)
        self.call("patch", f"/api/mentor/queries/{qid}", mentor, json={"status": "resolved"})
        self.assertEqual(self.call("get", f"/api/student/queries/{qid}", student).get_json()["status"], "resolved")

        dash = self.call("get", "/api/mentor/dashboard", mentor).get_json()
        self.assertGreater(dash["mentees"], 0)
        self.assertGreater(dash["overdue_items"], 0)

    def test_06_risk_flag_and_filters(self):
        mentor = self.login("t1@college.edu", "teacherpass1")
        items = self.call("get", "/api/mentor/mentees", mentor).get_json()
        risky = [i for i in items if i["risk"]["level"] != "low"]
        self.assertTrue(risky)
        self.assertTrue(all(i["risk"]["reasons"] for i in risky))     # reasons, not just a score
        self.assertEqual(items, sorted(items, key=lambda i: (
            {"overdue": 0, "due_soon": 1, "on_track": 2, "no_deadline": 3, "done": 4}[i["worst_urgency"]], -i["risk"]["score"], i["roll_no"])))
        f = self.call("get", "/api/mentor/mentees?type=fees&status=partly_paid", mentor).get_json()
        self.assertEqual(len(f), 1)

    def test_07_access_control(self):
        mentor1 = self.login("t1@college.edu", "teacherpass1")
        mentor3 = self.login("t3@college.edu", "teacherpass1")
        students = self.call("get", "/api/admin/students", self.admin).get_json()
        t1_student = next(s for s in students if s["mentor_id"] == self.T["T1"])
        self.assertEqual(self.call("get", f"/api/mentor/mentees/{t1_student['id']}", mentor3).status_code, 403)
        student = self.login(t1_student["email"], self.creds[t1_student["roll_no"]])
        self.assertEqual(self.call("get", "/api/mentor/mentees", student).status_code, 403)
        self.assertEqual(self.call("get", "/api/admin/teachers", mentor1).status_code, 403)
        self.assertEqual(self.c.get("/api/student/dashboard").status_code, 401)
        self.assertEqual(self.c.post("/api/auth/login", json={"email": "t1@college.edu", "password": "wrong"}).status_code, 401)

    def test_08_teacher_leaves_and_capacity_change(self):
        T = self.T
        before = {t["teacher_id"]: t["mentees"] for t in self.call("get", "/api/admin/mentors/summary", self.admin).get_json()["teachers"]}
        # lower T2's capacity -> only newest auto assignments beyond it are released/reassigned
        self.call("patch", f"/api/admin/teachers/{T['T2']}", self.admin, json={"capacity_override": 2})
        plan = self.call("post", "/api/admin/mentors/allocate", self.admin, json={"dry_run": False}).get_json()
        self.assertEqual(plan["summary"]["to_release"], before[T["T2"]] - 2)
        after = {t["teacher_id"]: t["mentees"] for t in self.call("get", "/api/admin/mentors/summary", self.admin).get_json()["teachers"]}
        self.assertEqual(after[T["T2"]], 2)
        self.assertLessEqual(after[T["T1"]], 8)

        # teacher deactivated -> mentees freed, never given to a non-teaching mentor
        r = self.call("patch", f"/api/admin/teachers/{T['T3']}", self.admin, json={"active": False}).get_json()
        self.assertEqual(r["mentees_released"], 2)
        self.call("post", "/api/admin/mentors/allocate", self.admin, json={"dry_run": False})
        summary = self.call("get", "/api/admin/mentors/summary", self.admin).get_json()
        c_students = [s for s in self.call("get", "/api/admin/students", self.admin).get_json() if s["roll_no"] >= "R017"]
        self.assertTrue(all(s["mentor_id"] is None for s in c_students))        # class C has no eligible teacher now
        unassigned = {u["roll_no"] for u in summary["unassigned_students"]}
        self.assertTrue({"R017", "R018", "R019", "R020", "R021"} <= unassigned)

    def test_09_student_class_change_releases_mentor(self):
        students = self.call("get", "/api/admin/students", self.admin).get_json()
        a_student = next(s for s in students if s["section"] == "B" and s["mentor_id"])
        r = self.call("patch", f"/api/admin/students/{a_student['id']}", self.admin, json={"section": "C"}).get_json()
        self.assertTrue(r["mentor_released"])

    def test_10_audit_log(self):
        log = self.call("get", "/api/admin/audit", self.admin).get_json()
        actions = {e["action"] for e in log}
        self.assertTrue({"allocation_upload", "mentor_allocate", "teacher_update"} <= actions)

    def test_11_frontend_is_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Mentoring Portal", r.data)
        self.assertEqual(self.c.get("/static/app.js").status_code, 200)
        self.assertEqual(self.c.get("/static/style.css").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
