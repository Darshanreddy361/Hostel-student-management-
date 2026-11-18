import os
import tempfile
import unittest
from datetime import date

import app as portal


class AttendanceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.previous_database = portal.DATABASE
        self.previous_secret_key = portal.app.secret_key
        self.previous_testing = portal.app.testing
        portal.DATABASE = os.path.join(
            self.temporary_directory.name,
            "attendance-test.db"
        )
        portal.app.secret_key = "attendance-test-secret"
        portal.app.testing = True
        portal.init_db(provision_warden_from_env=False)

        conn = portal.get_db()
        self.south_student_id = conn.execute("""
            INSERT INTO students (name, usn, department, year, campus)
            VALUES ('South Student', 'S12345', 'CSE', '2nd Year', 'South Campus')
        """).lastrowid
        self.north_student_id = conn.execute("""
            INSERT INTO students (name, usn, department, year, campus)
            VALUES ('North Student', 'N12345', 'ISE', '3rd Year', 'North Campus')
        """).lastrowid
        self.warden_id = conn.execute("""
            INSERT INTO users (username, password_hash, role)
            VALUES ('warden', 'unused-test-hash', 'warden')
        """).lastrowid
        conn.commit()
        conn.close()

        self.client = portal.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.warden_id
            session["username"] = "warden"
            session["role"] = "warden"
            session["_csrf_token"] = "attendance-test-csrf"
        self.attendance_date = date.today().isoformat()

    def tearDown(self):
        portal.DATABASE = self.previous_database
        portal.app.secret_key = self.previous_secret_key
        portal.app.testing = self.previous_testing
        self.temporary_directory.cleanup()

    def post_attendance(self, statuses, campus=""):
        form_data = {
            "csrf_token": "attendance-test-csrf",
            "attendance_date": self.attendance_date,
            "campus": campus,
        }
        form_data.update({
            f"attendance_{student_id}": status
            for student_id, status in statuses.items()
        })
        return self.client.post("/warden/attendance", data=form_data)

    def test_warden_dashboard_links_to_daily_attendance(self):
        response = self.client.get("/warden")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Take attendance", response.data)
        self.assertIn(b"/warden/attendance", response.data)

        response = portal.app.test_client().get("/warden/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Daily hostel attendance", response.data)

    def test_student_directory_shows_saved_attendance_status(self):
        self.post_attendance({
            self.south_student_id: "Present",
            self.north_student_id: "Absent",
        })

        response = self.client.get("/warden")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Today's attendance", response.data)
        self.assertIn(b"attendance-status-present", response.data)
        self.assertIn(b"attendance-status-absent", response.data)

    def test_saves_and_updates_one_record_per_student_and_day(self):
        response = self.client.get(
            f"/warden/attendance?date={self.attendance_date}&campus=South+Campus"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"S12345", response.data)
        self.assertNotIn(b"N12345", response.data)
        self.assertIn(
            f'name="attendance_{self.south_student_id}"'.encode(),
            response.data
        )
        self.assertIn(b'value="Present"', response.data)
        self.assertIn(b'value="Absent"', response.data)

        response = self.post_attendance({
            self.south_student_id: "Present",
        }, campus="South Campus")
        self.assertEqual(response.status_code, 302)

        response = self.post_attendance({
            self.south_student_id: "On Leave",
        }, campus="South Campus")
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        try:
            records = conn.execute("""
                SELECT student_id, status, marked_by_user_id
                FROM attendance_records
                WHERE attendance_date = ?
            """, (self.attendance_date,)).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["student_id"], self.south_student_id)
        self.assertEqual(records[0]["status"], "On Leave")
        self.assertEqual(records[0]["marked_by_user_id"], self.warden_id)

    def test_invalid_status_does_not_save_partial_attendance(self):
        response = self.post_attendance({
            self.south_student_id: "Present",
            self.north_student_id: "Excused",
        })
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        try:
            record_count = conn.execute(
                "SELECT COUNT(*) FROM attendance_records"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(record_count, 0)


if __name__ == "__main__":
    unittest.main()
