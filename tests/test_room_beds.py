import os
import tempfile
import unittest

import app as portal


class RoomBedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.previous_database = portal.DATABASE
        self.previous_secret_key = portal.app.secret_key
        self.previous_testing = portal.app.testing
        portal.DATABASE = os.path.join(self.temporary_directory.name, "rooms.db")
        portal.app.secret_key = "room-bed-test-secret"
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
        conn.commit()
        conn.close()

        self.client = portal.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["username"] = "warden"
            session["role"] = "warden"
            session["_csrf_token"] = "room-bed-test-csrf"

    def tearDown(self):
        portal.DATABASE = self.previous_database
        portal.app.secret_key = self.previous_secret_key
        portal.app.testing = self.previous_testing
        self.temporary_directory.cleanup()

    def test_existing_room_assignments_are_backfilled_to_unique_beds(self):
        conn = portal.get_db()
        room_id = conn.execute(
            "SELECT id FROM rooms WHERE room_number = 'S-101'"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE students SET room_id = ? WHERE id = ?",
            (room_id, self.south_student_id)
        )
        conn.commit()
        conn.close()

        portal.init_db(provision_warden_from_env=False)

        conn = portal.get_db()
        try:
            assignment = conn.execute("""
                SELECT beds.bed_label, beds.student_id, students.room_id
                FROM beds
                JOIN students ON students.id = beds.student_id
                WHERE students.id = ?
            """, (self.south_student_id,)).fetchone()
            room_beds = conn.execute(
                "SELECT COUNT(*) FROM beds WHERE room_id = ?", (room_id,)
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertIsNotNone(assignment)
        self.assertEqual(assignment["student_id"], self.south_student_id)
        self.assertEqual(assignment["room_id"], room_id)
        self.assertEqual(room_beds, 3)

    def test_warden_can_choose_a_bed_and_vacating_releases_it(self):
        conn = portal.get_db()
        bed_id = conn.execute("""
            SELECT beds.id FROM beds
            JOIN rooms ON rooms.id = beds.room_id
            WHERE rooms.room_number = 'S-101' AND beds.status = 'Available'
                AND beds.student_id IS NULL
            ORDER BY beds.bed_label LIMIT 1
        """).fetchone()["id"]
        conn.close()

        page = self.client.get(
            f"/warden/students/{self.south_student_id}/allocation"
        )
        self.assertEqual(page.status_code, 200)
        response = self.client.post(
            f"/warden/students/{self.south_student_id}/allocation",
            data={"csrf_token": "room-bed-test-csrf", "bed_id": str(bed_id)}
        )
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        try:
            assigned_bed = conn.execute(
                "SELECT student_id FROM beds WHERE id = ?", (bed_id,)
            ).fetchone()["student_id"]
            assigned_room = conn.execute(
                "SELECT room_id FROM students WHERE id = ?",
                (self.south_student_id,)
            ).fetchone()["room_id"]
        finally:
            conn.close()
        self.assertEqual(assigned_bed, self.south_student_id)
        self.assertEqual(assigned_room, self._room_for_bed(bed_id))

        response = self.client.post(
            f"/vacate/{self.south_student_id}",
            data={"csrf_token": "room-bed-test-csrf"}
        )
        self.assertEqual(response.status_code, 302)
        conn = portal.get_db()
        try:
            released_bed = conn.execute(
                "SELECT student_id FROM beds WHERE id = ?", (bed_id,)
            ).fetchone()["student_id"]
            released_room = conn.execute(
                "SELECT room_id FROM students WHERE id = ?",
                (self.south_student_id,)
            ).fetchone()["room_id"]
        finally:
            conn.close()
        self.assertIsNone(released_bed)
        self.assertIsNone(released_room)

    def test_room_creation_builds_bed_slots_and_warden_can_block_a_bed(self):
        response = self.client.post(
            "/warden/rooms",
            data={
                "csrf_token": "room-bed-test-csrf",
                "campus": "South Campus",
                "building_name": "Block Z",
                "floor_number": "6",
                "room_number": "Z-601",
                "capacity": "2",
            },
        )
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        room = conn.execute("""
            SELECT rooms.id, buildings.name, floors.floor_number
            FROM rooms
            JOIN buildings ON buildings.id = rooms.building_id
            JOIN floors ON floors.id = rooms.floor_id
            WHERE rooms.room_number = 'Z-601'
        """).fetchone()
        bed_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM beds WHERE room_id = ?", (room["id"],)
            ).fetchall()
        ]
        conn.close()
        self.assertEqual(room["name"], "Block Z")
        self.assertEqual(room["floor_number"], 6)
        self.assertEqual(len(bed_ids), 2)

        response = self.client.post(
            f"/warden/beds/{bed_ids[0]}/status",
            data={"csrf_token": "room-bed-test-csrf", "status": "Blocked"},
        )
        self.assertEqual(response.status_code, 302)
        conn = portal.get_db()
        try:
            status = conn.execute(
                "SELECT status FROM beds WHERE id = ?", (bed_ids[0],)
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(status, "Blocked")

    def test_bed_from_another_campus_cannot_be_assigned(self):
        conn = portal.get_db()
        south_bed_id = conn.execute("""
            SELECT beds.id FROM beds
            JOIN rooms ON rooms.id = beds.room_id
            WHERE rooms.campus = 'South Campus' AND beds.student_id IS NULL
                AND beds.status = 'Available'
            LIMIT 1
        """).fetchone()["id"]
        conn.close()

        response = self.client.post(
            f"/warden/students/{self.north_student_id}/allocation",
            data={
                "csrf_token": "room-bed-test-csrf",
                "bed_id": str(south_bed_id),
            },
        )
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        try:
            student_room = conn.execute(
                "SELECT room_id FROM students WHERE id = ?",
                (self.north_student_id,)
            ).fetchone()["room_id"]
            bed_student = conn.execute(
                "SELECT student_id FROM beds WHERE id = ?", (south_bed_id,)
            ).fetchone()["student_id"]
        finally:
            conn.close()
        self.assertIsNone(student_room)
        self.assertIsNone(bed_student)

    def test_auto_allocation_skips_blocked_beds(self):
        conn = portal.get_db()
        room_id = conn.execute(
            "SELECT id FROM rooms WHERE room_number = 'S-101'"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE beds SET status = 'Blocked' WHERE room_id = ?", (room_id,)
        )
        conn.commit()
        conn.close()

        response = self.client.post(
            f"/allocate/{self.south_student_id}",
            data={"csrf_token": "room-bed-test-csrf"},
        )
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        try:
            student_room = conn.execute(
                "SELECT room_id FROM students WHERE id = ?",
                (self.south_student_id,)
            ).fetchone()["room_id"]
            student_bed = conn.execute(
                "SELECT status FROM beds WHERE student_id = ?",
                (self.south_student_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertNotEqual(student_room, room_id)
        self.assertEqual(student_bed["status"], "Available")

    def _room_for_bed(self, bed_id):
        conn = portal.get_db()
        try:
            return conn.execute(
                "SELECT room_id FROM beds WHERE id = ?", (bed_id,)
            ).fetchone()["room_id"]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
