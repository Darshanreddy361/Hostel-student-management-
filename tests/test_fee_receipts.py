import io
import os
import tempfile
import unittest

import app as portal
from werkzeug.datastructures import FileStorage


class FeeReceiptWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.previous_database = portal.DATABASE
        self.previous_secret_key = portal.app.secret_key
        self.previous_testing = portal.app.testing
        self.previous_upload_directory = portal.app.config["RECEIPT_UPLOAD_DIR"]
        portal.DATABASE = os.path.join(self.temporary_directory.name, "fees.db")
        portal.app.config["RECEIPT_UPLOAD_DIR"] = os.path.join(
            self.temporary_directory.name,
            "private-receipts",
        )
        portal.app.secret_key = "fee-receipt-test-secret"
        portal.app.testing = True

        conn = portal.get_db()
        conn.execute("CREATE TABLE preserved_test_data (value TEXT)")
        conn.execute("INSERT INTO preserved_test_data VALUES ('preserved')")
        conn.commit()
        conn.close()
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
        self.south_user_id = conn.execute("""
            INSERT INTO users (username, password_hash, role, student_id)
            VALUES ('S12345', 'unused-test-hash', 'student', ?)
        """, (self.south_student_id,)).lastrowid
        self.north_user_id = conn.execute("""
            INSERT INTO users (username, password_hash, role, student_id)
            VALUES ('N12345', 'unused-test-hash', 'student', ?)
        """, (self.north_student_id,)).lastrowid
        self.warden_id = conn.execute("""
            INSERT INTO users (username, password_hash, role)
            VALUES ('warden', 'unused-test-hash', 'warden')
        """).lastrowid
        conn.commit()
        conn.close()

        self.student_client = portal.app.test_client()
        self.other_student_client = portal.app.test_client()
        self.warden_client = portal.app.test_client()
        self.authenticate(
            self.student_client, "student", self.south_user_id,
            self.south_student_id
        )
        self.authenticate(
            self.other_student_client, "student", self.north_user_id,
            self.north_student_id
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
            session["_csrf_token"] = "fee-test-csrf"
            if role == "student":
                session["student_id"] = student_id

    def upload_receipt(
        self,
        client=None,
        filename="receipt.png",
        content=b"\x89PNG\r\n\x1a\nreceipt-bytes",
        mimetype="image/png",
        correction_of_id="",
    ):
        client = client or self.student_client
        response = client.post(
            "/student/fees/upload",
            data={
                "csrf_token": "fee-test-csrf",
                "amount": "1250.50",
                "payment_date": "2026-11-18",
                "transaction_id": "UTR-123456",
                "academic_year": "2026-2027",
                "correction_of_id": correction_of_id,
                "receipt_file": (io.BytesIO(content), filename, mimetype),
            },
            content_type="multipart/form-data",
        )
        response.close()
        return response

    def get_receipt(self, receipt_id):
        conn = portal.get_db()
        try:
            return conn.execute(
                "SELECT * FROM fee_receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
        finally:
            conn.close()

    def test_migration_preserves_existing_tables_and_creates_receipt_schema(self):
        conn = portal.get_db()
        try:
            preserved = conn.execute(
                "SELECT value FROM preserved_test_data"
            ).fetchone()[0]
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(fee_receipts)"
                )
            }
        finally:
            conn.close()

        self.assertEqual(preserved, "preserved")
        self.assertTrue({
            "id", "student_id", "amount", "payment_date", "transaction_id",
            "academic_year", "receipt_filename", "status", "rejection_reason",
            "verified_by", "verified_at", "uploaded_at", "resubmission_of",
        }.issubset(columns))

    def test_student_upload_is_pending_and_receipt_is_private_to_owner(self):
        response = self.upload_receipt()
        self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        try:
            receipt_id = conn.execute(
                "SELECT id FROM fee_receipts"
            ).fetchone()["id"]
        finally:
            conn.close()
        receipt = self.get_receipt(receipt_id)

        self.assertEqual(receipt["student_id"], self.south_student_id)
        self.assertEqual(receipt["status"], "Pending")
        self.assertIsNone(receipt["verified_by"])
        self.assertIsNone(receipt["verified_at"])
        receipt_path = os.path.join(
            portal.app.config["RECEIPT_UPLOAD_DIR"],
            receipt["receipt_filename"],
        )
        self.assertEqual(os.stat(receipt_path).st_mode & 0o077, 0)
        self.assertFalse(
            os.path.commonpath((
                os.path.realpath(portal.app.static_folder),
                os.path.realpath(portal.app.config["RECEIPT_UPLOAD_DIR"]),
            )) == os.path.realpath(portal.app.static_folder)
        )

        response = self.student_client.get(
            f"/fee-receipts/{receipt_id}/file"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        response.close()
        response = self.other_student_client.get(
            f"/fee-receipts/{receipt_id}/file"
        )
        self.assertEqual(response.status_code, 404)
        response.close()
        response = self.warden_client.get(
            f"/fee-receipts/{receipt_id}/file?download=1"
        )
        self.assertEqual(response.status_code, 200)
        response.close()

        dashboard = self.student_client.get("/student")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Hostel Fee Payment", dashboard.data)
        self.assertIn(b"South Student", dashboard.data)
        self.assertIn(b"S12345", dashboard.data)

    def test_upload_rejects_mismatched_mime_and_invalid_academic_year(self):
        response = self.upload_receipt(mimetype="application/pdf")
        self.assertEqual(response.status_code, 302)
        self.upload_receipt(content=b"not a PNG file")

        response = self.student_client.post(
            "/student/fees/upload",
            data={
                "csrf_token": "fee-test-csrf",
                "amount": "1250.50",
                "payment_date": "2026-11-18",
                "transaction_id": "UTR-123456",
                "academic_year": "2026-2028",
                "receipt_file": (
                    io.BytesIO(b"\x89PNG\r\n\x1a\nreceipt"),
                    "receipt.png",
                    "image/png",
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        response.close()

        conn = portal.get_db()
        try:
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM fee_receipts"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(receipt_count, 0)

    def test_receipt_download_rejects_path_traversal_filename(self):
        self.upload_receipt()
        receipt_id = self.get_receipt(1)["id"]
        conn = portal.get_db()
        try:
            conn.execute(
                "UPDATE fee_receipts SET receipt_filename = ? WHERE id = ?",
                ("../../outside.png", receipt_id),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.student_client.get(
            f"/fee-receipts/{receipt_id}/file"
        )
        self.assertEqual(response.status_code, 404)

    def test_accepts_all_supported_receipt_types(self):
        test_files = (
            ("receipt.jpg", "image/jpeg", b"\xff\xd8\xffjpeg"),
            ("receipt.jpeg", "image/jpeg", b"\xff\xd8\xffjpeg"),
            ("receipt.png", "image/png", b"\x89PNG\r\n\x1a\npng"),
            ("receipt.pdf", "application/pdf", b"%PDF-1.7\npdf"),
        )
        for filename, mimetype, content in test_files:
            with self.subTest(filename=filename):
                response = self.upload_receipt(
                    filename=filename,
                    mimetype=mimetype,
                    content=content,
                )
                self.assertEqual(response.status_code, 302)

        conn = portal.get_db()
        try:
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM fee_receipts"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(receipt_count, len(test_files))

    def test_rejects_receipts_larger_than_five_megabytes(self):
        upload = FileStorage(
            stream=io.BytesIO(
                b"\x89PNG\r\n\x1a\n" + b"x" * (portal.MAX_RECEIPT_SIZE + 1)
            ),
            filename="receipt.png",
            content_type="image/png",
        )
        with self.assertRaisesRegex(ValueError, "5 MB or smaller"):
            portal.read_receipt_upload(upload)

    def test_warden_rejection_requires_reason_and_student_can_resubmit(self):
        self.upload_receipt()
        receipt_id = self.get_receipt(1)["id"]
        response = self.warden_client.post(
            f"/warden/fees/{receipt_id}/reject",
            data={"csrf_token": "fee-test-csrf"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.get_receipt(receipt_id)["status"], "Pending")

        response = self.warden_client.post(
            f"/warden/fees/{receipt_id}/reject",
            data={
                "csrf_token": "fee-test-csrf",
                "rejection_reason": "Receipt amount is not legible.",
            },
        )
        self.assertEqual(response.status_code, 302)
        rejected = self.get_receipt(receipt_id)
        self.assertEqual(rejected["status"], "Rejected")
        self.assertEqual(
            rejected["rejection_reason"], "Receipt amount is not legible."
        )
        self.assertEqual(rejected["verified_by"], self.warden_id)
        self.assertIsNotNone(rejected["verified_at"])

        response = self.upload_receipt(correction_of_id=str(receipt_id))
        self.assertEqual(response.status_code, 302)
        conn = portal.get_db()
        try:
            corrected = conn.execute("""
                SELECT * FROM fee_receipts
                WHERE resubmission_of = ?
            """, (receipt_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(corrected)
        self.assertEqual(corrected["status"], "Pending")
        self.assertEqual(self.get_receipt(receipt_id)["status"], "Rejected")

        dashboard = self.student_client.get("/student")
        self.assertIn(b"Receipt amount is not legible.", dashboard.data)
        self.assertIn(b"Upload corrected receipt", dashboard.data)

    def test_warden_approval_records_verifier_and_filters_dashboard(self):
        self.upload_receipt()
        receipt_id = self.get_receipt(1)["id"]

        response = self.student_client.post(
            f"/warden/fees/{receipt_id}/approve",
            data={"csrf_token": "fee-test-csrf"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.get_receipt(receipt_id)["status"], "Pending")

        response = self.warden_client.post(
            f"/warden/fees/{receipt_id}/approve",
            data={"csrf_token": "fee-test-csrf"},
        )
        self.assertEqual(response.status_code, 302)
        approved = self.get_receipt(receipt_id)
        self.assertEqual(approved["status"], "Approved")
        self.assertEqual(approved["verified_by"], self.warden_id)
        self.assertIsNotNone(approved["verified_at"])

        response = self.warden_client.get(
            "/warden?fee_status=Approved&fee_q=S12345"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Fee Verification", response.data)
        self.assertIn(b"UTR-123456", response.data)
        self.assertIn(b"Approved", response.data)
        self.assertNotIn(b"No fee receipts match these filters.", response.data)

    def test_rejected_receipt_cannot_be_corrected_by_another_student(self):
        self.upload_receipt()
        receipt_id = self.get_receipt(1)["id"]
        self.warden_client.post(
            f"/warden/fees/{receipt_id}/reject",
            data={
                "csrf_token": "fee-test-csrf",
                "rejection_reason": "Upload a clearer receipt.",
            },
        )

        response = self.upload_receipt(
            client=self.other_student_client,
            correction_of_id=str(receipt_id),
        )
        self.assertEqual(response.status_code, 302)
        conn = portal.get_db()
        try:
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM fee_receipts"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(receipt_count, 1)

    def test_approved_receipt_cannot_be_used_for_a_correction(self):
        self.upload_receipt()
        receipt_id = self.get_receipt(1)["id"]
        self.warden_client.post(
            f"/warden/fees/{receipt_id}/approve",
            data={"csrf_token": "fee-test-csrf"},
        )

        self.upload_receipt(correction_of_id=str(receipt_id))

        self.assertEqual(self.get_receipt(receipt_id)["status"], "Approved")
        conn = portal.get_db()
        try:
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM fee_receipts"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(receipt_count, 1)

    def test_receipt_storage_rejects_public_static_directory(self):
        portal.app.config["RECEIPT_UPLOAD_DIR"] = os.path.join(
            portal.app.static_folder,
            "receipt-test-directory",
        )
        with self.assertRaisesRegex(RuntimeError, "outside the static directory"):
            portal.receipt_storage_directory()


if __name__ == "__main__":
    unittest.main()
