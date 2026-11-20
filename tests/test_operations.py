import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import app as portal


class HostelOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.previous_database = portal.DATABASE
        self.previous_secret_key = portal.app.secret_key
        self.previous_testing = portal.app.testing
        self.previous_upload_directory = portal.app.config["RECEIPT_UPLOAD_DIR"]
        portal.DATABASE = os.path.join(self.temporary_directory.name, "ops.db")
        portal.app.config["RECEIPT_UPLOAD_DIR"] = os.path.join(
            self.temporary_directory.name, "private-receipts"
        )
        portal.app.secret_key = "operations-test-secret"
        portal.app.testing = True
        portal.init_db(provision_warden_from_env=False)

        conn = portal.get_db()
        self.student_id = conn.execute("""
            INSERT INTO students (name, usn, department, year, campus)
            VALUES ('Demo Student', 'S12345', 'CSE', '2nd Year', 'South Campus')
        """).lastrowid
        self.other_student_id = conn.execute("""
            INSERT INTO students (name, usn, department, year, campus)
            VALUES ('Other Student', 'N12345', 'ISE', '3rd Year', 'North Campus')
        """).lastrowid
        self.student_user_id = conn.execute("""
            INSERT INTO users (username, password_hash, role, student_id)
            VALUES ('S12345', 'unused-test-hash', 'student', ?)
        """, (self.student_id,)).lastrowid
        self.other_student_user_id = conn.execute("""
            INSERT INTO users (username, password_hash, role, student_id)
            VALUES ('N12345', 'unused-test-hash', 'student', ?)
        """, (self.other_student_id,)).lastrowid
        self.warden_id = conn.execute("""
            INSERT INTO users (username, password_hash, role)
            VALUES ('warden', 'unused-test-hash', 'warden')
        """).lastrowid
        self.staff_id = conn.execute("""
            INSERT INTO hostel_staff (name, role, campus)
            VALUES ('Demo Staff', 'Maintenance', 'South Campus')
        """).lastrowid
        self.facility_id = conn.execute("""
            INSERT INTO facilities (campus, name, category, location)
            VALUES ('South Campus', 'Laundry', 'Laundry', 'Block A')
        """).lastrowid
        conn.commit()
        conn.close()

        self.student_client = portal.app.test_client()
        self.other_student_client = portal.app.test_client()
        self.warden_client = portal.app.test_client()
        self.authenticate(
            self.student_client, "student", self.student_user_id, self.student_id
        )
        self.authenticate(
            self.other_student_client, "student",
            self.other_student_user_id, self.other_student_id
        )
        self.authenticate(self.warden_client, "warden", self.warden_id)

    def tearDown(self):
        portal.DATABASE = self.previous_database
        portal.app.secret_key = self.previous_secret_key
        portal.app.testing = self.previous_testing
        portal.app.config["RECEIPT_UPLOAD_DIR"] = self.previous_upload_directory
        self.temporary_directory.cleanup()

    @staticmethod
    def authenticate(client, role, user_id, student_id=None):
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["username"] = role
            session["role"] = role
            session["_csrf_token"] = "operations-test-csrf"
            if student_id is not None:
                session["student_id"] = student_id

    def test_student_dashboard_and_warden_operations_hub_render(self):
        student_page = self.student_client.get("/student")
        self.assertEqual(student_page.status_code, 200)
        self.assertIn(b"Request an outpass", student_page.data)
        self.assertIn(b"emergency contacts", student_page.data)

        warden_page = self.warden_client.get("/warden/operations")
        self.assertEqual(warden_page.status_code, 200)
        self.assertIn(b"Operations hub", warden_page.data)
        self.assertIn(b"Reports &amp; exports", warden_page.data)

    def test_student_dashboard_marks_meals_unpublished_when_no_menus_exist(self):
        response = self.student_client.get("/student")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"Today's mess menu has not been published yet.",
            response.data,
        )
        self.assertNotIn(b"Menu not published yet.", response.data)

    def test_hostel_today_uses_mysuru_timezone(self):
        utc_instant = datetime(2026, 11, 19, 19, 30, tzinfo=timezone.utc)
        with patch("app.datetime") as datetime_mock:
            datetime_mock.now.side_effect = lambda timezone: utc_instant.astimezone(timezone)
            self.assertEqual(portal.hostel_today(), date(2026, 11, 20))

    def test_public_homepage_has_no_sample_meal_menu_preview(self):
        response = portal.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"SAMPLE PREVIEW", response.data)
        self.assertNotIn(b"Illustrative sample content", response.data)
        self.assertNotIn(b"Idli", response.data)
        self.assertIn(b"A menu for every meal", response.data)
        self.assertIn(b"Attendance and complaints", response.data)
        self.assertIn(b"Visitors and outpasses", response.data)
        self.assertIn(b"Vendors and finance records", response.data)

    def test_student_cannot_read_or_edit_another_student_records(self):
        conn = portal.get_db()
        conn.execute("""
            INSERT INTO visitor_requests (
                student_id, visitor_name, relationship, visitor_phone,
                visit_date, visit_time, purpose
            ) VALUES (?, 'Private Visitor', 'Parent', '55512345',
                      '2026-12-01', '10:00', 'Visit')
        """, (self.student_id,))
        conn.commit()
        conn.close()

        other_page = self.other_student_client.get("/student")
        self.assertEqual(other_page.status_code, 200)
        self.assertNotIn(b"Private Visitor", other_page.data)
        response = self.other_student_client.post(
            "/student/profile",
            data={
                "csrf_token": "operations-test-csrf",
                "guardian_name": "Unauthorized edit",
                "guardian_phone": "",
                "emergency_phone": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = portal.get_db()
        try:
            guardian = conn.execute(
                "SELECT guardian_name FROM students WHERE id = ?",
                (self.student_id,)
            ).fetchone()["guardian_name"]
        finally:
            conn.close()
        self.assertIsNone(guardian)

    def test_student_home_address_is_private_and_visible_to_warden(self):
        address = "12 Example Road, Mysuru, Karnataka 570001"
        conn = portal.get_db()
        conn.execute(
            "UPDATE students SET home_address = ? WHERE id = ?",
            (address, self.student_id),
        )
        conn.commit()
        conn.close()

        own_page = self.student_client.get("/student")
        other_page = self.other_student_client.get("/student")
        warden_page = self.warden_client.get("/warden")

        self.assertIn(address.encode(), own_page.data)
        self.assertNotIn(address.encode(), other_page.data)
        self.assertIn(address.encode(), warden_page.data)
        emergency_report = self.warden_client.get(
            "/warden/reports/emergency-contacts.csv"
        )
        self.assertEqual(emergency_report.status_code, 200)
        self.assertIn(address.encode(), emergency_report.data)

    def test_registration_requires_and_saves_emergency_details(self):
        registration_client = portal.app.test_client()
        with registration_client.session_transaction() as session:
            session["_csrf_token"] = "operations-test-csrf"
        address = "24 Sample Lane, Mysuru, Karnataka 570002"

        incomplete_response = registration_client.post(
            "/register",
            data={
                "csrf_token": "operations-test-csrf",
                "name": "New Student",
                "usn": "X12345",
                "department": "CSE",
                "year": "1st Year",
                "campus": "North Campus",
                "password": "secure-demo-password",
                "password_confirmation": "secure-demo-password",
            },
        )
        self.assertEqual(incomplete_response.status_code, 302)

        response = registration_client.post(
            "/register",
            data={
                "csrf_token": "operations-test-csrf",
                "name": "New Student",
                "usn": "X12345",
                "department": "CSE",
                "year": "1st Year",
                "campus": "North Campus",
                "guardian_name": "Parent Name",
                "guardian_phone": "+91 98765 43210",
                "emergency_phone": "+91 87654 32109",
                "home_address": address,
                "password": "secure-demo-password",
                "password_confirmation": "secure-demo-password",
            },
        )

        self.assertEqual(response.status_code, 302)
        conn = portal.get_db()
        try:
            student = conn.execute(
                """SELECT guardian_name, guardian_phone, emergency_phone,
                          home_address
                   FROM students WHERE usn = 'X12345'"""
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(student["guardian_name"], "Parent Name")
        self.assertEqual(student["guardian_phone"], "+91 98765 43210")
        self.assertEqual(student["emergency_phone"], "+91 87654 32109")
        self.assertEqual(student["home_address"], address)

    def test_legacy_migration_refuses_to_guess_campus_for_existing_records(self):
        portal.DATABASE = os.path.join(self.temporary_directory.name, "legacy.db")
        conn = sqlite3.connect(portal.DATABASE)
        conn.executescript("""
            CREATE TABLE rooms (
                id INTEGER PRIMARY KEY,
                room_number TEXT NOT NULL,
                capacity INTEGER NOT NULL
            );
            CREATE TABLE students (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                usn TEXT NOT NULL,
                department TEXT NOT NULL,
                year TEXT NOT NULL,
                room_id INTEGER
            );
            INSERT INTO rooms VALUES (1, '101', 3);
            INSERT INTO students VALUES (1, 'Legacy Student', 'L12345',
                                         'CSE', '2nd Year', 1);
        """)
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "Cannot infer campus"):
            portal.init_db(provision_warden_from_env=False)

        conn = sqlite3.connect(portal.DATABASE)
        room_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(rooms)")
        }
        student_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(students)")
        }
        self.assertNotIn("campus", room_columns)
        self.assertNotIn("campus", student_columns)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT name FROM students").fetchone()[0],
            "Legacy Student"
        )
        conn.close()

    def test_notice_and_rule_publication_controls_student_visibility(self):
        notice_response = self.warden_client.post(
            "/warden/operations/notices",
            data={
                "csrf_token": "operations-test-csrf",
                "title": "Quiet hours",
                "body": "Please keep shared areas quiet after 10 PM.",
                "audience": "Students",
                "expires_on": "",
            },
        )
        self.assertEqual(notice_response.status_code, 302)
        rule_response = self.warden_client.post(
            "/warden/operations/rules",
            data={
                "csrf_token": "operations-test-csrf",
                "title": "Quiet hours",
                "body": "Quiet hours apply from 10 PM to 7 AM.",
                "category": "Community",
            },
        )
        self.assertEqual(rule_response.status_code, 302)

        conn = portal.get_db()
        notice_id = conn.execute(
            "SELECT id FROM notices"
        ).fetchone()["id"]
        rule_id = conn.execute(
            "SELECT id FROM hostel_rules"
        ).fetchone()["id"]
        conn.close()
        student_page = self.student_client.get("/student")
        self.assertIn(b"Please keep shared areas quiet", student_page.data)
        self.assertIn(b"Quiet hours apply", student_page.data)

        self.warden_client.post(
            f"/warden/operations/notices/{notice_id}/status",
            data={
                "csrf_token": "operations-test-csrf",
                "published": "0",
            },
        )
        self.warden_client.post(
            f"/warden/operations/rules/{rule_id}/status",
            data={"csrf_token": "operations-test-csrf", "active": "0"},
        )
        student_page = self.student_client.get("/student")
        self.assertNotIn(b"Please keep shared areas quiet", student_page.data)
        self.assertNotIn(b"Quiet hours apply", student_page.data)

    def test_visitor_and_outpass_approval_and_gate_events(self):
        future_day = (datetime.now() + timedelta(days=2)).date().isoformat()
        visitor_response = self.student_client.post(
            "/student/visitors",
            data={
                "csrf_token": "operations-test-csrf",
                "visitor_name": "Demo Parent",
                "relationship": "Parent",
                "visitor_phone": "+1 555 123 4567",
                "visit_date": future_day,
                "visit_time": "10:30",
                "purpose": "Student visit",
            },
        )
        self.assertEqual(visitor_response.status_code, 302)

        departure = (datetime.now() + timedelta(days=1)).replace(
            second=0, microsecond=0
        )
        expected_return = departure + timedelta(hours=4)
        outpass_response = self.student_client.post(
            "/student/outpasses",
            data={
                "csrf_token": "operations-test-csrf",
                "destination": "City center",
                "reason": "Personal errand",
                "departure_at": departure.strftime("%Y-%m-%dT%H:%M"),
                "expected_return_at": expected_return.strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertEqual(outpass_response.status_code, 302)

        conn = portal.get_db()
        visitor_id = conn.execute(
            "SELECT id FROM visitor_requests"
        ).fetchone()["id"]
        outpass_id = conn.execute(
            "SELECT id FROM outpasses"
        ).fetchone()["id"]
        conn.close()

        reject_without_reason = self.warden_client.post(
            f"/warden/operations/visitors/{visitor_id}/decision",
            data={
                "csrf_token": "operations-test-csrf",
                "decision": "Rejected",
            },
        )
        self.assertEqual(reject_without_reason.status_code, 302)
        conn = portal.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT status FROM visitor_requests WHERE id = ?", (visitor_id,)
            ).fetchone()["status"],
            "Pending",
        )
        conn.close()

        visitor_decision = self.warden_client.post(
            f"/warden/operations/visitors/{visitor_id}/decision",
            data={
                "csrf_token": "operations-test-csrf",
                "decision": "Approved",
                "decision_note": "",
            },
        )
        self.assertEqual(visitor_decision.status_code, 302)
        outpass_decision = self.warden_client.post(
            f"/warden/operations/outpasses/{outpass_id}/decision",
            data={
                "csrf_token": "operations-test-csrf",
                "decision": "Approved",
                "decision_note": "Approved for the requested period",
            },
        )
        self.assertEqual(outpass_decision.status_code, 302)
        self.warden_client.post(
            f"/warden/operations/outpasses/{outpass_id}/gate",
            data={
                "csrf_token": "operations-test-csrf",
                "action": "check-out",
            },
        )
        self.warden_client.post(
            f"/warden/operations/outpasses/{outpass_id}/gate",
            data={
                "csrf_token": "operations-test-csrf",
                "action": "check-in",
            },
        )
        conn = portal.get_db()
        try:
            outpass = conn.execute(
                "SELECT status, checked_out_at, checked_in_at FROM outpasses WHERE id = ?",
                (outpass_id,)
            ).fetchone()
            visitor_status = conn.execute(
                "SELECT status FROM visitor_requests WHERE id = ?", (visitor_id,)
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(visitor_status, "Approved")
        self.assertEqual(outpass["status"], "Returned")
        self.assertIsNotNone(outpass["checked_out_at"])
        self.assertIsNotNone(outpass["checked_in_at"])

    def test_meal_reservation_and_maintenance_ticket_lifecycle(self):
        future_day = (portal.hostel_today() + timedelta(days=1)).isoformat()
        response = self.warden_client.post(
            "/warden/operations/meals",
            data={
                "csrf_token": "operations-test-csrf",
                "campus": "South Campus",
                "meal_date": future_day,
                "meal_type": "Lunch",
                "menu_text": "Vegetable rice and fruit",
            },
        )
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        menu_id = conn.execute(
            "SELECT id FROM meal_menus"
        ).fetchone()["id"]
        conn.close()
        self.student_client.post(
            f"/student/meals/{menu_id}/reservation",
            data={
                "csrf_token": "operations-test-csrf",
                "action": "reserve",
            },
        )
        conn = portal.get_db()
        reservation_status = conn.execute(
            "SELECT status FROM meal_reservations WHERE student_id = ?",
            (self.student_id,)
        ).fetchone()["status"]
        conn.close()
        self.assertEqual(reservation_status, "Reserved")

        self.student_client.post(
            "/student/tickets",
            data={
                "csrf_token": "operations-test-csrf",
                "facility_id": str(self.facility_id),
                "subject": "Washer leaking",
                "description": "Water is leaking from the washer.",
                "priority": "High",
            },
        )
        conn = portal.get_db()
        ticket_id = conn.execute(
            "SELECT id FROM maintenance_tickets"
        ).fetchone()["id"]
        conn.close()

        missing_resolution = self.warden_client.post(
            f"/warden/operations/tickets/{ticket_id}",
            data={
                "csrf_token": "operations-test-csrf",
                "status": "Resolved",
                "assigned_staff_id": str(self.staff_id),
                "resolution_note": "",
            },
        )
        self.assertEqual(missing_resolution.status_code, 302)
        conn = portal.get_db()
        self.assertEqual(
            conn.execute(
                "SELECT status FROM maintenance_tickets WHERE id = ?", (ticket_id,)
            ).fetchone()["status"],
            "Open",
        )
        conn.close()

        resolved = self.warden_client.post(
            f"/warden/operations/tickets/{ticket_id}",
            data={
                "csrf_token": "operations-test-csrf",
                "status": "Resolved",
                "assigned_staff_id": str(self.staff_id),
                "resolution_note": "Seal replaced and test run completed.",
            },
        )
        self.assertEqual(resolved.status_code, 302)
        conn = portal.get_db()
        try:
            ticket = conn.execute(
                "SELECT status, assigned_staff_id, resolved_at FROM maintenance_tickets WHERE id = ?",
                (ticket_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(ticket["status"], "Resolved")
        self.assertEqual(ticket["assigned_staff_id"], self.staff_id)
        self.assertIsNotNone(ticket["resolved_at"])

    def test_daily_menus_cover_four_meals_and_are_campus_scoped(self):
        today = portal.hostel_today().isoformat()
        menus_by_campus = {
            "South Campus": {
                "breakfast": "South breakfast",
                "lunch": "South lunch",
                "snacks": "South snacks",
                "dinner": "South dinner",
            },
            "North Campus": {
                "breakfast": "North breakfast",
                "lunch": "North lunch",
                "snacks": "North snacks",
                "dinner": "North dinner",
            },
        }
        for campus, menus in menus_by_campus.items():
            response = self.warden_client.post(
                "/warden/operations/meals/daily",
                data={
                    "csrf_token": "operations-test-csrf",
                    "campus": campus,
                    "meal_date": today,
                    **{f"menu_{meal}": text for meal, text in menus.items()},
                },
            )
            self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        count = conn.execute(
            "SELECT COUNT(*) FROM meal_menus WHERE meal_date = ?", (today,)
        ).fetchone()[0]
        self.assertEqual(count, 8)
        conn.close()

        south_page = self.student_client.get("/student")
        north_page = self.other_student_client.get("/student")
        for menu in menus_by_campus["South Campus"].values():
            self.assertIn(menu.encode(), south_page.data)
            self.assertNotIn(menu.encode(), north_page.data)
        for menu in menus_by_campus["North Campus"].values():
            self.assertIn(menu.encode(), north_page.data)
            self.assertNotIn(menu.encode(), south_page.data)
        self.assertIn(b"Snacks", south_page.data)
        self.assertIn(b"Today's mess menu", south_page.data)

    def test_meal_menu_migration_preserves_reservations_and_adds_snacks(self):
        conn = portal.get_db()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            CREATE TABLE meal_menus_legacy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campus TEXT NOT NULL,
                meal_date TEXT NOT NULL,
                meal_type TEXT NOT NULL
                    CHECK (meal_type IN ('Breakfast', 'Lunch', 'Dinner')),
                menu_text TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (campus, meal_date, meal_type),
                FOREIGN KEY (created_by) REFERENCES users(id)
            )
        """)
        conn.execute("DROP TABLE meal_menus")
        conn.execute(
            "ALTER TABLE meal_menus_legacy RENAME TO meal_menus"
        )
        conn.commit()
        conn.close()

        conn = portal.get_db()
        menu_id = conn.execute("""
            INSERT INTO meal_menus (
                campus, meal_date, meal_type, menu_text, created_by
            ) VALUES ('South Campus', '2026-11-20', 'Breakfast',
                      'Legacy breakfast', ?)
        """, (self.warden_id,)).lastrowid
        conn.execute("""
            INSERT INTO meal_reservations (student_id, menu_id)
            VALUES (?, ?)
        """, (self.student_id, menu_id))
        conn.commit()
        conn.close()

        portal.init_db(provision_warden_from_env=False)

        conn = portal.get_db()
        try:
            preserved = conn.execute("""
                SELECT meal_menus.menu_text, meal_reservations.status
                FROM meal_menus
                JOIN meal_reservations
                    ON meal_reservations.menu_id = meal_menus.id
                WHERE meal_menus.id = ?
            """, (menu_id,)).fetchone()
            conn.execute("""
                INSERT INTO meal_menus (
                    campus, meal_date, meal_type, menu_text, created_by
                ) VALUES ('South Campus', '2026-11-20', 'Snacks',
                          'Tea and fruit', ?)
            """, (self.warden_id,))
            conn.commit()
            foreign_key_errors = conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(preserved["menu_text"], "Legacy breakfast")
        self.assertEqual(preserved["status"], "Reserved")
        self.assertEqual(foreign_key_errors, [])

    def test_warden_csv_export_is_protected_and_formula_safe(self):
        conn = portal.get_db()
        conn.execute(
            "UPDATE students SET name = '=HYPERLINK(\"https://example.test\")' WHERE id = ?",
            (self.student_id,)
        )
        conn.commit()
        conn.close()

        forbidden = portal.app.test_client().get("/warden/reports/students.csv")
        self.assertEqual(forbidden.status_code, 302)
        response = self.warden_client.get("/warden/reports/students.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b"'=HYPERLINK",
            response.data,
        )
        self.assertIn(b"text/csv", response.content_type.encode())

    def test_staff_duty_and_finance_records_are_saved(self):
        duty_date = datetime.now().date().isoformat()
        duty_response = self.warden_client.post(
            "/warden/operations/duties",
            data={
                "csrf_token": "operations-test-csrf",
                "staff_id": str(self.staff_id),
                "duty_date": duty_date,
                "shift": "Night",
                "location": "South Campus main gate",
                "notes": "Night coverage",
            },
        )
        self.assertEqual(duty_response.status_code, 302)

        vendor_response = self.warden_client.post(
            "/warden/operations/vendors",
            data={
                "csrf_token": "operations-test-csrf",
                "name": "Demo Supplies",
                "category": "Supplies",
                "campus": "South Campus",
                "contact_name": "Demo Contact",
                "phone": "",
            },
        )
        self.assertEqual(vendor_response.status_code, 302)
        conn = portal.get_db()
        vendor_id = conn.execute(
            "SELECT id FROM vendors"
        ).fetchone()["id"]
        conn.close()

        finance_response = self.warden_client.post(
            "/warden/operations/finance",
            data={
                "csrf_token": "operations-test-csrf",
                "entry_type": "Expense",
                "category": "Maintenance",
                "amount": "1250.00",
                "entry_date": duty_date,
                "vendor_id": str(vendor_id),
                "invoice_reference": "INV-001",
                "payment_status": "Due",
                "description": "Pump replacement parts",
            },
        )
        self.assertEqual(finance_response.status_code, 302)
        conn = portal.get_db()
        try:
            duty = conn.execute(
                "SELECT shift, location FROM duty_rosters"
            ).fetchone()
            finance = conn.execute("""
                SELECT entry_type, amount, vendor_id, payment_status
                FROM finance_entries
            """).fetchone()
        finally:
            conn.close()
        self.assertEqual(duty["shift"], "Night")
        self.assertEqual(duty["location"], "South Campus main gate")
        self.assertEqual(finance["entry_type"], "Expense")
        self.assertEqual(finance["amount"], 1250)
        self.assertEqual(finance["vendor_id"], vendor_id)
        self.assertEqual(finance["payment_status"], "Due")

    def test_profile_update_and_expired_or_invalid_inputs(self):
        response = self.student_client.post(
            "/student/profile",
            data={
                "csrf_token": "operations-test-csrf",
                "guardian_name": "Demo Guardian",
                "guardian_phone": "+1 555 111 2222",
                "emergency_phone": "invalid",
            },
        )
        self.assertEqual(response.status_code, 302)
        conn = portal.get_db()
        try:
            student = conn.execute(
                "SELECT guardian_name, guardian_phone, emergency_phone FROM students WHERE id = ?",
                (self.student_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(student["guardian_name"])

        self.student_client.post(
            "/student/profile",
            data={
                "csrf_token": "operations-test-csrf",
                "guardian_name": "Demo Guardian",
                "guardian_phone": "+1 555 111 2222",
                "emergency_phone": "+1 555 222 3333",
                "home_address": "12 Example Road, Mysuru, Karnataka 570001",
            },
        )
        conn = portal.get_db()
        try:
            student = conn.execute(
                """SELECT guardian_name, guardian_phone, emergency_phone,
                          home_address
                   FROM students WHERE id = ?""",
                (self.student_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(student["guardian_name"], "Demo Guardian")
        self.assertEqual(student["guardian_phone"], "+1 555 111 2222")
        self.assertEqual(student["emergency_phone"], "+1 555 222 3333")
        self.assertEqual(
            student["home_address"],
            "12 Example Road, Mysuru, Karnataka 570001",
        )

        self.student_client.post(
            "/student/profile",
            data={
                "csrf_token": "operations-test-csrf",
                "guardian_name": "Changed Guardian",
                "guardian_phone": "+1 555 111 2222",
                "emergency_phone": "+1 555 222 3333",
                "home_address": "A" * 501,
            },
        )
        conn = portal.get_db()
        try:
            student = conn.execute(
                "SELECT guardian_name, home_address FROM students WHERE id = ?",
                (self.student_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(student["guardian_name"], "Demo Guardian")
        self.assertEqual(
            student["home_address"],
            "12 Example Road, Mysuru, Karnataka 570001",
        )


if __name__ == "__main__":
    unittest.main()
