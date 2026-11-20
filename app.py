
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from hmac import compare_digest
import io
import os
import re
import secrets
import sqlite3
from zoneinfo import ZoneInfo

import click
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, redirect, render_template, request, send_file,
    session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

# Database configuration for local and Docker execution
DATABASE = os.environ.get("DATABASE_PATH", "hostel.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true",
    RECEIPT_UPLOAD_DIR=os.environ.get(
        "RECEIPT_UPLOAD_DIR",
        os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "receipts")
    ),
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
)
if not os.environ.get("SECRET_KEY"):
    app.logger.warning("SECRET_KEY is not set; active sessions will reset on restart.")

CAMPUSES = ("South Campus", "North Campus")
ROOM_CAPACITY = 3
ROOM_NUMBERS = range(101, 351)
MIN_PASSWORD_LENGTH = 8
ATTENDANCE_STATUSES = ("Present", "Absent", "On Leave")
FEE_RECEIPT_STATUSES = ("Pending", "Approved", "Rejected")
MEAL_TYPES = ("Breakfast", "Lunch", "Snacks", "Dinner")
REQUEST_STATUSES = ("Pending", "Approved", "Rejected")
TICKET_STATUSES = ("Open", "In Progress", "Resolved")
TICKET_PRIORITIES = ("Low", "Normal", "High", "Urgent")
MAX_RECEIPT_SIZE = 5 * 1024 * 1024
MAX_REJECTION_REASON_LENGTH = 1000
MAX_TRANSACTION_ID_LENGTH = 128
HOSTEL_TIMEZONE = ZoneInfo("Asia/Kolkata")
RECEIPT_TYPES = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".pdf": ("application/pdf", b"%PDF-"),
}
ACADEMIC_YEAR_PATTERN = re.compile(r"^(\d{4})-(\d{4})$")
SHORT_ACADEMIC_YEAR_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")


# Database connection
def get_db():
    db_folder = os.path.dirname(DATABASE)

    if db_folder:
        os.makedirs(db_folder, exist_ok=True)

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hostel_today():
    return datetime.now(HOSTEL_TIMEZONE).date()


def csrf_token():
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


def csrf_protect(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == "POST":
            expected = session.get("_csrf_token", "")
            submitted = request.form.get("csrf_token", "")
            if not expected or not compare_digest(expected, submitted):
                abort(400)
        return view(*args, **kwargs)

    return wrapped


def migrate_meal_menus_for_snacks(conn):
    table = conn.execute("""
        SELECT sql FROM sqlite_master
        WHERE type = 'table' AND name = 'meal_menus'
    """).fetchone()
    if table is None or re.search(r"'Snacks'", table["sql"], re.IGNORECASE):
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE meal_menus_with_snacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campus TEXT NOT NULL,
                meal_date TEXT NOT NULL,
                meal_type TEXT NOT NULL
                    CHECK (meal_type IN ('Breakfast', 'Lunch', 'Snacks', 'Dinner')),
                menu_text TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (campus, meal_date, meal_type),
                FOREIGN KEY (created_by) REFERENCES users(id)
            )
        """)
        conn.execute("""
            INSERT INTO meal_menus_with_snacks (
                id, campus, meal_date, meal_type, menu_text, created_by,
                created_at
            )
            SELECT id, campus, meal_date, meal_type, menu_text, created_by,
                   created_at
            FROM meal_menus
        """)
        conn.execute("DROP TABLE meal_menus")
        conn.execute(
            "ALTER TABLE meal_menus_with_snacks RENAME TO meal_menus"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("user_id") is None:
                return redirect(url_for("login"))
            if session.get("role") != role:
                return redirect(url_for("index"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


# Create database and tables
def init_db(provision_warden_from_env=True):
    warden_username = (
        os.environ.get("WARDEN_USERNAME", "").strip()
        if provision_warden_from_env else ""
    )
    warden_password = (
        os.environ.get("WARDEN_PASSWORD", "")
        if provision_warden_from_env else ""
    )
    if bool(warden_username) != bool(warden_password):
        raise RuntimeError("Set both WARDEN_USERNAME and WARDEN_PASSWORD.")
    if warden_password and len(warden_password) < 12:
        raise RuntimeError("WARDEN_PASSWORD must be at least 12 characters.")

    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS buildings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            campus TEXT NOT NULL,
            UNIQUE (name, campus)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS floors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            building_id INTEGER NOT NULL,
            floor_number INTEGER NOT NULL CHECK (floor_number > 0),
            name TEXT NOT NULL,
            UNIQUE (building_id, floor_number),
            FOREIGN KEY (building_id) REFERENCES buildings(id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_number TEXT UNIQUE NOT NULL,
            capacity INTEGER NOT NULL,
            campus TEXT NOT NULL,
            building_id INTEGER,
            floor_id INTEGER,
            FOREIGN KEY (building_id) REFERENCES buildings(id),
            FOREIGN KEY (floor_id) REFERENCES floors(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            usn TEXT UNIQUE NOT NULL,
            department TEXT NOT NULL,
            year TEXT NOT NULL,
            room_id INTEGER,
            campus TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        )
    """)

    room_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(rooms)")
    }
    student_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(students)")
    }
    legacy_campus_records = []
    if "campus" not in room_columns and conn.execute(
        "SELECT EXISTS(SELECT 1 FROM rooms)"
    ).fetchone()[0]:
        legacy_campus_records.append("rooms")
    if "campus" not in student_columns and conn.execute(
        "SELECT EXISTS(SELECT 1 FROM students)"
    ).fetchone()[0]:
        legacy_campus_records.append("students")
    if legacy_campus_records:
        conn.close()
        tables = " and ".join(legacy_campus_records)
        raise RuntimeError(
            f"Cannot infer campus for existing {tables} records. Back up the "
            "database, assign each record its correct campus, and add/populate "
            "the missing campus column before restarting. No campus values "
            "were guessed."
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS beds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            bed_label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Available'
                CHECK (status IN ('Available', 'Blocked', 'Maintenance')),
            student_id INTEGER UNIQUE,
            UNIQUE (room_id, bed_label),
            FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE SET NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_beds_room_status
        ON beds(room_id, status, student_id)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('warden', 'student')),
            student_id INTEGER UNIQUE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending'
                CHECK (status IN ('Pending', 'Resolved')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('Present', 'Absent', 'On Leave')),
            marked_by_user_id INTEGER NOT NULL,
            marked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (student_id, attendance_date),
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (marked_by_user_id) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_attendance_records_date
        ON attendance_records(attendance_date)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fee_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            amount NUMERIC NOT NULL CHECK (amount > 0),
            payment_date TEXT NOT NULL,
            transaction_id TEXT NOT NULL,
            academic_year TEXT NOT NULL,
            receipt_filename TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'Pending'
                CHECK (status IN ('Pending', 'Approved', 'Rejected')),
            rejection_reason TEXT,
            verified_by INTEGER,
            verified_at TEXT,
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resubmission_of INTEGER,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (verified_by) REFERENCES users(id),
            FOREIGN KEY (resubmission_of) REFERENCES fee_receipts(id)
                ON DELETE SET NULL
        )
    """)
    fee_receipt_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(fee_receipts)")
    }
    if "resubmission_of" not in fee_receipt_columns:
        conn.execute("""
            ALTER TABLE fee_receipts
            ADD COLUMN resubmission_of INTEGER
            REFERENCES fee_receipts(id) ON DELETE SET NULL
        """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fee_receipts_student_uploaded
        ON fee_receipts(student_id, uploaded_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fee_receipts_status_uploaded
        ON fee_receipts(status, uploaded_at DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            audience TEXT NOT NULL DEFAULT 'All'
                CHECK (audience IN ('All', 'Students')),
            published INTEGER NOT NULL DEFAULT 1
                CHECK (published IN (0, 1)),
            expires_on TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notices_published
        ON notices(published, created_at DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hostel_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            category TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
                CHECK (active IN (0, 1)),
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hostel_rules_active
        ON hostel_rules(active, created_at DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meal_menus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campus TEXT NOT NULL,
            meal_date TEXT NOT NULL,
            meal_type TEXT NOT NULL
                CHECK (meal_type IN ('Breakfast', 'Lunch', 'Snacks', 'Dinner')),
            menu_text TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (campus, meal_date, meal_type),
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)
    migrate_meal_menus_for_snacks(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meal_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            menu_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Reserved'
                CHECK (status IN ('Reserved', 'Cancelled')),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (student_id, menu_id),
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (menu_id) REFERENCES meal_menus(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visitor_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            visitor_name TEXT NOT NULL,
            relationship TEXT NOT NULL,
            visitor_phone TEXT NOT NULL,
            visit_date TEXT NOT NULL,
            visit_time TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending'
                CHECK (status IN ('Pending', 'Approved', 'Rejected')),
            decision_note TEXT,
            decided_by INTEGER,
            decided_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (decided_by) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_visitor_requests_status_date
        ON visitor_requests(status, visit_date)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outpasses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            destination TEXT NOT NULL,
            reason TEXT NOT NULL,
            departure_at TEXT NOT NULL,
            expected_return_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending'
                CHECK (status IN ('Pending', 'Approved', 'Rejected', 'Returned')),
            decision_note TEXT,
            approved_by INTEGER,
            approved_at TEXT,
            checked_out_at TEXT,
            checked_in_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (approved_by) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outpasses_status_departure
        ON outpasses(status, departure_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS facilities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campus TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            location TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Operational'
                CHECK (status IN ('Operational', 'Limited', 'Closed')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (campus, name, location)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hostel_staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            campus TEXT NOT NULL,
            phone TEXT,
            active INTEGER NOT NULL DEFAULT 1
                CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            facility_id INTEGER,
            subject TEXT NOT NULL,
            description TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'Normal'
                CHECK (priority IN ('Low', 'Normal', 'High', 'Urgent')),
            status TEXT NOT NULL DEFAULT 'Open'
                CHECK (status IN ('Open', 'In Progress', 'Resolved')),
            assigned_staff_id INTEGER,
            resolution_note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (facility_id) REFERENCES facilities(id) ON DELETE SET NULL,
            FOREIGN KEY (assigned_staff_id) REFERENCES hostel_staff(id)
                ON DELETE SET NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_status_created
        ON maintenance_tickets(status, created_at DESC)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS duty_rosters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            duty_date TEXT NOT NULL,
            shift TEXT NOT NULL,
            location TEXT NOT NULL,
            notes TEXT,
            assigned_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (staff_id, duty_date, shift),
            FOREIGN KEY (staff_id) REFERENCES hostel_staff(id) ON DELETE CASCADE,
            FOREIGN KEY (assigned_by) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_duty_rosters_date
        ON duty_rosters(duty_date, shift)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            campus TEXT NOT NULL,
            contact_name TEXT,
            phone TEXT,
            active INTEGER NOT NULL DEFAULT 1
                CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (name, campus)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS finance_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL
                CHECK (entry_type IN ('Income', 'Expense')),
            category TEXT NOT NULL,
            amount NUMERIC NOT NULL CHECK (amount > 0),
            entry_date TEXT NOT NULL,
            vendor_id INTEGER,
            invoice_reference TEXT,
            payment_status TEXT NOT NULL DEFAULT 'Recorded'
                CHECK (payment_status IN ('Recorded', 'Due', 'Paid')),
            description TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE SET NULL,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_finance_entries_date
        ON finance_entries(entry_date DESC)
    """)

    if "campus" not in room_columns:
        conn.execute("ALTER TABLE rooms ADD COLUMN campus TEXT")
    if "building_id" not in room_columns:
        conn.execute("""
            ALTER TABLE rooms ADD COLUMN building_id INTEGER
                REFERENCES buildings(id)
        """)
    if "floor_id" not in room_columns:
        conn.execute("""
            ALTER TABLE rooms ADD COLUMN floor_id INTEGER REFERENCES floors(id)
        """)

    if "campus" not in student_columns:
        conn.execute("ALTER TABLE students ADD COLUMN campus TEXT")
    for column in (
        "guardian_name", "guardian_phone", "emergency_phone", "home_address"
    ):
        if column not in student_columns:
            conn.execute(f"ALTER TABLE students ADD COLUMN {column} TEXT")

    existing_rooms = conn.execute(
        "SELECT id, room_number, capacity FROM rooms"
    ).fetchall()
    existing_students = conn.execute(
        "SELECT COUNT(*) FROM students"
    ).fetchone()[0]
    sample_room_numbers = {str(number) for number in range(101, 107)}

    if (
        not existing_students
        and len(existing_rooms) == len(sample_room_numbers)
        and {room["room_number"] for room in existing_rooms} == sample_room_numbers
        and all(room["capacity"] == ROOM_CAPACITY for room in existing_rooms)
    ):
        for room in existing_rooms:
            conn.execute(
                """
                UPDATE rooms
                SET room_number = ?, campus = ?
                WHERE id = ?
                """,
                (f"S-{room['room_number']}", "South Campus", room["id"])
            )

    for campus, prefix in (("South Campus", "S"), ("North Campus", "N")):
        rooms = [
            (f"{prefix}-{number}", ROOM_CAPACITY, campus)
            for number in ROOM_NUMBERS
        ]
        conn.executemany("""
            INSERT INTO rooms (room_number, capacity, campus)
            VALUES (?, ?, ?)
            ON CONFLICT(room_number) DO NOTHING
        """, rooms)

    all_rooms = conn.execute("""
        SELECT id, room_number, capacity, campus, building_id, floor_id
        FROM rooms
        ORDER BY campus, room_number
    """).fetchall()
    for room in all_rooms:
        building_name = "Block A"
        conn.execute("""
            INSERT INTO buildings (name, campus)
            VALUES (?, ?)
            ON CONFLICT(name, campus) DO NOTHING
        """, (building_name, room["campus"]))
        building_id = conn.execute("""
            SELECT id FROM buildings WHERE name = ? AND campus = ?
        """, (building_name, room["campus"])).fetchone()["id"]

        room_digits = re.search(r"\d+", room["room_number"])
        floor_number = max(1, int(room_digits.group(0)) // 100) if room_digits else 1
        floor_name = f"Floor {floor_number}"
        conn.execute("""
            INSERT INTO floors (building_id, floor_number, name)
            VALUES (?, ?, ?)
            ON CONFLICT(building_id, floor_number) DO NOTHING
        """, (building_id, floor_number, floor_name))
        floor_id = conn.execute("""
            SELECT id FROM floors
            WHERE building_id = ? AND floor_number = ?
        """, (building_id, floor_number)).fetchone()["id"]
        conn.execute("""
            UPDATE rooms
            SET building_id = COALESCE(building_id, ?),
                floor_id = COALESCE(floor_id, ?)
            WHERE id = ?
        """, (building_id, floor_id, room["id"]))
        conn.executemany("""
            INSERT INTO beds (room_id, bed_label)
            VALUES (?, ?)
            ON CONFLICT(room_id, bed_label) DO NOTHING
        """, [
            (room["id"], f"Bed {bed_number}")
            for bed_number in range(1, room["capacity"] + 1)
        ])

    assigned_students = conn.execute("""
        SELECT students.id, students.room_id, rooms.room_number
        FROM students
        JOIN rooms ON rooms.id = students.room_id
        WHERE NOT EXISTS (
            SELECT 1 FROM beds WHERE beds.student_id = students.id
        )
        ORDER BY students.id
    """).fetchall()
    for student in assigned_students:
        bed = conn.execute("""
            SELECT id FROM beds
            WHERE room_id = ? AND status = 'Available' AND student_id IS NULL
            ORDER BY bed_label
            LIMIT 1
        """, (student["room_id"],)).fetchone()
        if bed is None:
            conn.rollback()
            conn.close()
            raise RuntimeError(
                f"Room {student['room_number']} has more assigned students "
                "than available beds. Resolve the over-capacity room before "
                "starting the app."
            )
        conn.execute(
            "UPDATE beds SET student_id = ? WHERE id = ?",
            (student["id"], bed["id"])
        )

    if warden_username:
        warden = conn.execute(
            "SELECT id, role FROM users WHERE username = ?",
            (warden_username,)
        ).fetchone()
        if warden and warden["role"] != "warden":
            conn.close()
            raise RuntimeError(
                "WARDEN_USERNAME is already assigned to a student account."
            )

        password_hash = generate_password_hash(warden_password)
        if warden:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_hash, warden["id"])
            )
        else:
            conn.execute("""
                INSERT INTO users (username, password_hash, role)
                VALUES (?, ?, 'warden')
            """, (warden_username, password_hash))

    conn.commit()
    conn.close()


def create_warden_account(username, password):
    username = username.strip()
    if not username:
        raise ValueError("Warden username cannot be empty.")
    if len(password) < 12:
        raise ValueError("Warden password must be at least 12 characters.")

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO users (username, password_hash, role)
            VALUES (?, ?, 'warden')
        """, (username, generate_password_hash(password)))
        conn.commit()
    except sqlite3.IntegrityError as error:
        conn.rollback()
        raise ValueError("That username already belongs to an account.") from error
    finally:
        conn.close()


@app.cli.command("create-warden")
def create_warden_command():
    """Create a warden login with a password entered in the terminal."""
    init_db(provision_warden_from_env=False)
    username = click.prompt("Warden username").strip()
    password = click.prompt(
        "Warden password",
        hide_input=True,
        confirmation_prompt=True
    )
    try:
        create_warden_account(username, password)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Warden account '{username}' created.")


# Home page
@app.route("/")
def index():
    if session.get("role") == "warden":
        return redirect(url_for("warden_dashboard"))
    if session.get("role") == "student":
        return redirect(url_for("student_dashboard"))
    return render_template("home.html")


@app.route("/login", methods=["GET", "POST"])
@csrf_protect
def login():
    return login_for_role("student")


@app.route("/warden/login", methods=["GET", "POST"])
@csrf_protect
def warden_login():
    return login_for_role("warden")


def login_for_role(expected_role):
    if session.get("user_id") is not None:
        return redirect(url_for("index"))

    conn = get_db()
    warden_configured = False
    if expected_role == "warden":
        warden_configured = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM users WHERE role = 'warden')"
        ).fetchone()[0]

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if expected_role == "student":
            user = conn.execute("""
                SELECT id, username, password_hash, role, student_id
                FROM users
                WHERE username = ? AND role = 'student'
            """, (username,)).fetchone()
        else:
            user = conn.execute("""
                SELECT id, username, password_hash, role, student_id
                FROM users
                WHERE username = ? AND role = 'warden'
            """, (username,)).fetchone()
        conn.close()

        if (
            not user
            or user["role"] != expected_role
            or not check_password_hash(user["password_hash"], password)
        ):
            flash("Invalid username or password.", "error")
            login_endpoint = "warden_login" if expected_role == "warden" else "login"
            return redirect(url_for(login_endpoint))

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        if user["student_id"] is not None:
            session["student_id"] = user["student_id"]
        return redirect(url_for("index"))

    conn.close()
    return render_template(
        "login.html",
        login_role=expected_role,
        warden_configured=bool(warden_configured),
    )


@app.route("/register", methods=["GET", "POST"])
@csrf_protect
def register_student():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        usn = request.form.get("usn", "").strip().upper()
        department = request.form.get("department", "").strip()
        year = request.form.get("year", "").strip()
        campus = request.form.get("campus", "").strip()
        guardian_name = request.form.get("guardian_name", "").strip()
        guardian_phone = request.form.get("guardian_phone", "").strip()
        emergency_phone = request.form.get("emergency_phone", "").strip()
        home_address = request.form.get("home_address", "").strip()
        password = request.form.get("password", "")
        password_confirmation = request.form.get("password_confirmation", "")

        if not all([name, usn, department, year]) or campus not in CAMPUSES:
            flash("Please fill all student details.", "error")
            return redirect(url_for("register_student"))
        if (
            not guardian_name or len(guardian_name) > 100
            or not valid_contact_phone(guardian_phone, required=True)
            or not valid_contact_phone(emergency_phone, required=True)
            or not home_address or len(home_address) > 500
        ):
            flash(
                "Enter a valid guardian name, both phone numbers, and home address.",
                "error",
            )
            return redirect(url_for("register_student"))
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                "error"
            )
            return redirect(url_for("register_student"))
        if password != password_confirmation:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register_student"))

        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT id FROM users WHERE username = ?", (usn,)
            ).fetchone():
                conn.rollback()
                flash("That USN already belongs to an account.", "error")
                return redirect(url_for("register_student"))

            student = conn.execute("""
                INSERT INTO students (
                    name, usn, department, year, campus,
                    guardian_name, guardian_phone, emergency_phone, home_address
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name, usn, department, year, campus,
                guardian_name or None, guardian_phone or None,
                emergency_phone or None, home_address or None,
            ))
            conn.execute("""
                INSERT INTO users (username, password_hash, role, student_id)
                VALUES (?, ?, 'student', ?)
            """, (
                usn, generate_password_hash(password), student.lastrowid
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            flash("That USN already belongs to an account.", "error")
            return redirect(url_for("register_student"))
        finally:
            conn.close()

        flash("Student account created. Sign in with your USN.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", campuses=CAMPUSES)


def get_room_inventory():
    conn = get_db()
    try:
        return conn.execute("""
            SELECT rooms.*, COUNT(beds.id) AS bed_count,
                   SUM(CASE WHEN beds.student_id IS NOT NULL THEN 1 ELSE 0 END)
                       AS occupied,
                   SUM(CASE WHEN beds.status = 'Available'
                                 AND beds.student_id IS NULL THEN 1 ELSE 0 END)
                       AS available_beds
            FROM rooms
            LEFT JOIN beds ON rooms.id = beds.room_id
            GROUP BY rooms.id
            ORDER BY rooms.campus, rooms.room_number
        """).fetchall()
    finally:
        conn.close()


def valid_contact_phone(value, required=False):
    if not value:
        return not required
    digits = re.sub(r"\D", "", value)
    return bool(
        re.fullmatch(r"[+()\d .-]{7,24}", value)
        and 7 <= len(digits) <= 15
    )


def parse_local_datetime(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None


def parse_payment_amount(value):
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0:
            return None
        rounded_amount = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return rounded_amount if amount == rounded_amount else None


def valid_academic_year(value):
    match = ACADEMIC_YEAR_PATTERN.fullmatch(value)
    if match:
        return int(match.group(2)) == int(match.group(1)) + 1
    short_match = SHORT_ACADEMIC_YEAR_PATTERN.fullmatch(value)
    return bool(
        short_match
        and int(short_match.group(2))
        == (int(short_match.group(1)) + 1) % 100
    )


def read_receipt_upload(upload):
    if upload is None or not upload.filename:
        raise ValueError("Choose a receipt file to upload.")

    extension = os.path.splitext(upload.filename)[1].lower()
    receipt_type = RECEIPT_TYPES.get(extension)
    if receipt_type is None:
        raise ValueError("Upload a JPG, JPEG, PNG, or PDF receipt.")

    expected_mimetype, signature = receipt_type
    if upload.mimetype != expected_mimetype:
        raise ValueError("The receipt file type does not match its extension.")

    content = upload.stream.read(MAX_RECEIPT_SIZE + 1)
    if not content:
        raise ValueError("The receipt file is empty.")
    if len(content) > MAX_RECEIPT_SIZE:
        raise ValueError("Receipt files must be 5 MB or smaller.")
    if not content.startswith(signature):
        raise ValueError("The uploaded file does not appear to be a valid receipt.")

    return content, extension, expected_mimetype


def receipt_storage_directory():
    storage_directory = os.path.realpath(app.config["RECEIPT_UPLOAD_DIR"])
    static_directory = os.path.realpath(app.static_folder)
    if os.path.commonpath((static_directory, storage_directory)) == static_directory:
        raise RuntimeError("Receipt uploads must be stored outside the static directory.")
    os.makedirs(storage_directory, mode=0o700, exist_ok=True)
    return storage_directory


def receipt_file_path(filename):
    if (
        not filename
        or os.path.basename(filename) != filename
        or not re.fullmatch(r"[a-f0-9]{32}\.(jpg|jpeg|png|pdf)", filename)
    ):
        abort(404)

    storage_directory = receipt_storage_directory()
    file_path = os.path.realpath(os.path.join(storage_directory, filename))
    if os.path.commonpath((storage_directory, file_path)) != storage_directory:
        abort(404)
    return file_path


def student_fee_receipts(student_id):
    conn = get_db()
    try:
        receipts = conn.execute("""
            SELECT fee_receipts.*
            FROM fee_receipts
            WHERE fee_receipts.student_id = ?
            ORDER BY fee_receipts.uploaded_at DESC, fee_receipts.id DESC
        """, (student_id,)).fetchall()
        summary = conn.execute("""
            SELECT
                COUNT(*) AS receipt_count,
                SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'Approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected,
                COALESCE(SUM(
                    CASE WHEN status = 'Approved' THEN amount ELSE 0 END
                ), 0) AS approved_total
            FROM fee_receipts
            WHERE student_id = ?
        """, (student_id,)).fetchone()
        return receipts, summary
    finally:
        conn.close()


@app.route("/fee-receipts/<int:receipt_id>/file")
def fee_receipt_file(receipt_id):
    if session.get("user_id") is None:
        return redirect(url_for("login"))
    role = session.get("role")
    if role not in ("student", "warden"):
        abort(404)

    conn = get_db()
    try:
        receipt = conn.execute("""
            SELECT fee_receipts.id, fee_receipts.student_id,
                   fee_receipts.receipt_filename
            FROM fee_receipts
            WHERE fee_receipts.id = ?
        """, (receipt_id,)).fetchone()
    finally:
        conn.close()

    if receipt is None:
        abort(404)
    if (
        role == "student"
        and receipt["student_id"] != session.get("student_id")
    ):
        abort(404)

    extension = os.path.splitext(receipt["receipt_filename"])[1].lower()
    mimetype = RECEIPT_TYPES.get(extension, (None,))[0]
    if mimetype is None:
        abort(404)

    file_path = receipt_file_path(receipt["receipt_filename"])
    if not os.path.isfile(file_path):
        abort(404)

    response = send_file(
        file_path,
        mimetype=mimetype,
        as_attachment=request.args.get("download") == "1",
        download_name=f"fee-receipt-{receipt_id}{extension}",
        conditional=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/warden")
@role_required("warden")
def warden_dashboard():
    attendance_today = date.today().isoformat()
    conn = get_db()

    students = conn.execute("""
        SELECT students.*, rooms.room_number, beds.bed_label,
               attendance_records.status AS attendance_status
        FROM students
        LEFT JOIN rooms ON students.room_id = rooms.id
        LEFT JOIN beds ON beds.student_id = students.id
        LEFT JOIN attendance_records
            ON attendance_records.student_id = students.id
            AND attendance_records.attendance_date = ?
        ORDER BY students.id DESC
    """, (attendance_today,)).fetchall()

    rooms = conn.execute("""
        SELECT rooms.*, buildings.name AS building_name,
               floors.name AS floor_name, floors.floor_number,
               COUNT(beds.id) AS bed_count,
               COALESCE(SUM(
                   CASE WHEN beds.student_id IS NOT NULL THEN 1 ELSE 0 END
               ), 0) AS occupied,
               COALESCE(SUM(
                   CASE WHEN beds.status = 'Available'
                             AND beds.student_id IS NULL THEN 1 ELSE 0 END
               ), 0) AS available_beds,
               COALESCE(SUM(
                   CASE WHEN beds.status = 'Blocked' THEN 1 ELSE 0 END
               ), 0) AS blocked_beds,
               COALESCE(SUM(
                   CASE WHEN beds.status = 'Maintenance' THEN 1 ELSE 0 END
               ), 0) AS maintenance_beds
        FROM rooms
        LEFT JOIN buildings ON buildings.id = rooms.building_id
        LEFT JOIN floors ON floors.id = rooms.floor_id
        LEFT JOIN beds ON beds.room_id = rooms.id
        GROUP BY rooms.id
        ORDER BY rooms.campus, buildings.name, floors.floor_number,
                 rooms.room_number
    """).fetchall()

    total_students = conn.execute(
        "SELECT COUNT(*) FROM students"
    ).fetchone()[0]

    allocated = conn.execute(
        "SELECT COUNT(*) FROM students WHERE room_id IS NOT NULL"
    ).fetchone()[0]
    total_rooms = len(rooms)
    total_beds = sum(room["bed_count"] for room in rooms)
    occupied_beds = sum(room["occupied"] for room in rooms)
    unavailable_beds = sum(
        room["blocked_beds"] + room["maintenance_beds"] for room in rooms
    )
    usable_beds = total_beds - unavailable_beds
    occupancy_percent = (
        round((occupied_beds / usable_beds * 100) if usable_beds else 0)
    )

    attendance_counts = conn.execute("""
        SELECT
            SUM(CASE WHEN status = 'Present' THEN 1 ELSE 0 END) AS present,
            SUM(CASE WHEN status = 'Absent' THEN 1 ELSE 0 END) AS absent,
            SUM(CASE WHEN status = 'On Leave' THEN 1 ELSE 0 END) AS on_leave
        FROM attendance_records
        WHERE attendance_date = ?
    """, (attendance_today,)).fetchone()
    attendance_present = attendance_counts["present"] or 0
    attendance_absent = attendance_counts["absent"] or 0
    attendance_leave = attendance_counts["on_leave"] or 0
    attendance_marked = attendance_present + attendance_absent + attendance_leave
    attendance_percent = (
        round((attendance_present / attendance_marked * 100)
              if attendance_marked else 0)
    )
    complaint_status_counts = conn.execute("""
        SELECT
            SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved
        FROM complaints
    """).fetchone()
    resolved_complaints = complaint_status_counts["resolved"] or 0

    available = sum(room["available_beds"] for room in rooms)
    pending_complaints = conn.execute(
        "SELECT COUNT(*) FROM complaints WHERE status = 'Pending'"
    ).fetchone()[0]
    complaint_count = conn.execute(
        "SELECT COUNT(*) FROM complaints"
    ).fetchone()[0]
    fee_status_filter = request.args.get("fee_status", "").strip()
    if fee_status_filter not in FEE_RECEIPT_STATUSES:
        fee_status_filter = ""
    fee_search = request.args.get("fee_q", "").strip()[:100]
    fee_query = """
        SELECT fee_receipts.*, students.name, students.usn, students.campus,
               users.username AS verifier_username
        FROM fee_receipts
        JOIN students ON students.id = fee_receipts.student_id
        LEFT JOIN users ON users.id = fee_receipts.verified_by
    """
    fee_conditions = []
    fee_parameters = []
    if fee_status_filter:
        fee_conditions.append("fee_receipts.status = ?")
        fee_parameters.append(fee_status_filter)
    if fee_search:
        fee_conditions.append("""
            (students.name LIKE ? OR students.usn LIKE ?
             OR students.campus LIKE ? OR fee_receipts.transaction_id LIKE ?)
        """)
        search_pattern = f"%{fee_search}%"
        fee_parameters.extend([search_pattern] * 4)
    if fee_conditions:
        fee_query += " WHERE " + " AND ".join(fee_conditions)
    fee_query += """
        ORDER BY
            CASE fee_receipts.status WHEN 'Pending' THEN 0 ELSE 1 END,
            fee_receipts.uploaded_at DESC, fee_receipts.id DESC
    """
    fee_receipts = conn.execute(fee_query, fee_parameters).fetchall()
    fee_summary = conn.execute("""
        SELECT
            COUNT(*) AS receipt_count,
            SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'Approved' THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN status = 'Rejected' THEN 1 ELSE 0 END) AS rejected,
            COALESCE(SUM(
                CASE WHEN status = 'Approved' THEN amount ELSE 0 END
            ), 0) AS approved_total
        FROM fee_receipts
    """).fetchone()
    complaints = conn.execute("""
        SELECT complaints.*, students.name, students.usn,
               students.department, students.campus, rooms.room_number
        FROM complaints
        JOIN students ON students.id = complaints.student_id
        LEFT JOIN rooms ON rooms.id = students.room_id
        ORDER BY
            CASE complaints.status WHEN 'Pending' THEN 0 ELSE 1 END,
            complaints.created_at DESC
    """).fetchall()

    room_query = request.args.get("q", "").strip()
    room_campus_filter = request.args.get("campus", "")
    room_status_filter = request.args.get("status", "")
    room_building_filter = request.args.get("building_id", type=int)
    room_floor_filter = request.args.get("floor_id", type=int)

    if room_campus_filter not in CAMPUSES:
        room_campus_filter = ""
    if room_status_filter not in ("available", "full"):
        room_status_filter = ""
    building_ids = {row["id"] for row in conn.execute(
        "SELECT id FROM buildings"
    ).fetchall()}
    if room_building_filter not in building_ids:
        room_building_filter = None
    floor_query = """
        SELECT floors.id, floors.building_id, floors.floor_number, floors.name
        FROM floors
        JOIN buildings ON buildings.id = floors.building_id
    """
    floor_parameters = []
    if room_building_filter:
        floor_query += " WHERE floors.building_id = ?"
        floor_parameters.append(room_building_filter)
    elif room_campus_filter:
        floor_query += " WHERE buildings.campus = ?"
        floor_parameters.append(room_campus_filter)
    room_floors = conn.execute(
        floor_query + " ORDER BY floors.building_id, floors.floor_number",
        floor_parameters
    ).fetchall()
    floor_ids = {row["id"] for row in room_floors}
    if room_floor_filter not in floor_ids:
        room_floor_filter = None
    room_buildings = conn.execute("""
        SELECT id, name, campus FROM buildings
        ORDER BY campus, name
    """).fetchall()
    campus_inventory = conn.execute("""
        SELECT rooms.campus, COUNT(DISTINCT rooms.id) AS room_count,
               COUNT(beds.id) AS bed_count
        FROM rooms
        LEFT JOIN beds ON beds.room_id = rooms.id
        GROUP BY rooms.campus
        ORDER BY rooms.campus
    """).fetchall()

    filtered_rooms = []
    for room in rooms:
        has_space = room["available_beds"] > 0
        searchable_text = f"{room['room_number']} {room['campus']}".casefold()

        if room_query.casefold() not in searchable_text:
            continue
        if room_campus_filter and room["campus"] != room_campus_filter:
            continue
        if room_building_filter and room["building_id"] != room_building_filter:
            continue
        if room_floor_filter and room["floor_id"] != room_floor_filter:
            continue
        if room_status_filter == "available" and not has_space:
            continue
        if room_status_filter == "full" and has_space:
            continue

        filtered_rooms.append(room)

    room_page_size = 24
    room_page = request.args.get("page", 1, type=int)
    if room_page < 1:
        room_page = 1
    room_results_count = len(filtered_rooms)
    room_page_count = max(
        1, (room_results_count + room_page_size - 1) // room_page_size
    )
    room_page = min(room_page, room_page_count)
    first_room_index = (room_page - 1) * room_page_size
    displayed_rooms = [
        dict(room) for room in filtered_rooms[
            first_room_index:first_room_index + room_page_size
        ]
    ]
    displayed_room_ids = [room["id"] for room in displayed_rooms]
    beds_by_room = {}
    if displayed_room_ids:
        room_placeholders = ", ".join("?" for _ in displayed_room_ids)
        bed_rows = conn.execute(f"""
            SELECT beds.id, beds.room_id, beds.bed_label, beds.status,
                   beds.student_id, students.name AS student_name
            FROM beds
            LEFT JOIN students ON students.id = beds.student_id
            WHERE beds.room_id IN ({room_placeholders})
            ORDER BY beds.room_id, beds.bed_label
        """, displayed_room_ids).fetchall()
        for bed in bed_rows:
            beds_by_room.setdefault(bed["room_id"], []).append(bed)
    for room in displayed_rooms:
        room["bed_slots"] = beds_by_room.get(room["id"], [])

    grouped_room_map = {}
    for room in displayed_rooms:
        group_key = (
            room["campus"],
            room["building_name"],
            room["floor_name"],
            room["floor_number"],
        )
        grouped_room_map.setdefault(group_key, []).append(room)
    room_groups = [
        {
            "campus": group_key[0],
            "building_name": group_key[1],
            "floor_name": group_key[2],
            "floor_number": group_key[3],
            "rooms": group_rooms,
        }
        for group_key, group_rooms in grouped_room_map.items()
    ]

    conn.close()

    return render_template(
        "index.html",
        students=students,
        rooms=displayed_rooms,
        pending_complaints=pending_complaints,
        complaint_count=complaint_count,
        complaints=complaints,
        room_results_count=room_results_count,
        room_page=room_page,
        room_page_count=room_page_count,
        first_room_index=first_room_index,
        room_query=room_query,
        room_campus_filter=room_campus_filter,
        room_status_filter=room_status_filter,
        room_building_filter=room_building_filter,
        room_floor_filter=room_floor_filter,
        room_buildings=room_buildings,
        room_floors=room_floors,
        campus_inventory=campus_inventory,
        room_groups=room_groups,
        total_students=total_students,
        allocated=allocated,
        available=available,
        total_rooms=total_rooms,
        total_beds=total_beds,
        occupied_beds=occupied_beds,
        unavailable_beds=unavailable_beds,
        usable_beds=usable_beds,
        occupancy_percent=occupancy_percent,
        attendance_present=attendance_present,
        attendance_absent=attendance_absent,
        attendance_leave=attendance_leave,
        attendance_marked=attendance_marked,
        attendance_percent=attendance_percent,
        resolved_complaints=resolved_complaints,
        fee_receipts=fee_receipts,
        fee_summary=fee_summary,
        fee_status_filter=fee_status_filter,
        fee_search=fee_search,
        campuses=CAMPUSES,
        warden_username=session.get("username")
    )


def operations_redirect(section):
    return redirect(
        url_for("warden_operations", tab=section) + f"#{section}"
    )


@app.route("/warden/operations")
@role_required("warden")
def warden_operations():
    conn = get_db()
    notices = conn.execute("""
        SELECT notices.*, users.username AS author
        FROM notices
        JOIN users ON users.id = notices.created_by
        ORDER BY notices.published DESC, notices.created_at DESC
        LIMIT 100
    """).fetchall()
    hostel_rules = conn.execute("""
        SELECT hostel_rules.*, users.username AS author
        FROM hostel_rules
        JOIN users ON users.id = hostel_rules.created_by
        ORDER BY hostel_rules.active DESC, hostel_rules.category, hostel_rules.title
        LIMIT 100
    """).fetchall()
    meal_menus = conn.execute("""
        SELECT meal_menus.*,
               COALESCE(SUM(
                   CASE WHEN meal_reservations.status = 'Reserved'
                        THEN 1 ELSE 0 END
               ), 0) AS reservation_count
        FROM meal_menus
        LEFT JOIN meal_reservations
            ON meal_reservations.menu_id = meal_menus.id
        GROUP BY meal_menus.id
        ORDER BY meal_menus.meal_date DESC,
                 CASE meal_menus.meal_type
                     WHEN 'Breakfast' THEN 1 WHEN 'Lunch' THEN 2
                     WHEN 'Snacks' THEN 3 ELSE 4 END
        LIMIT 100
    """).fetchall()
    visitor_requests = conn.execute("""
        SELECT visitor_requests.*, students.name AS student_name,
               students.usn, students.campus
        FROM visitor_requests
        JOIN students ON students.id = visitor_requests.student_id
        ORDER BY CASE visitor_requests.status
                     WHEN 'Pending' THEN 0 ELSE 1 END,
                 visitor_requests.visit_date, visitor_requests.visit_time
        LIMIT 100
    """).fetchall()
    outpasses = conn.execute("""
        SELECT outpasses.*, students.name AS student_name,
               students.usn, students.campus
        FROM outpasses
        JOIN students ON students.id = outpasses.student_id
        ORDER BY CASE outpasses.status
                     WHEN 'Pending' THEN 0 WHEN 'Approved' THEN 1 ELSE 2 END,
                 outpasses.departure_at DESC
        LIMIT 100
    """).fetchall()
    facilities = conn.execute("""
        SELECT * FROM facilities
        ORDER BY campus, category, name
    """).fetchall()
    tickets = conn.execute("""
        SELECT maintenance_tickets.*, students.name AS student_name,
               students.usn, students.campus, facilities.name AS facility_name,
               hostel_staff.name AS assigned_staff_name
        FROM maintenance_tickets
        JOIN students ON students.id = maintenance_tickets.student_id
        LEFT JOIN facilities ON facilities.id = maintenance_tickets.facility_id
        LEFT JOIN hostel_staff
            ON hostel_staff.id = maintenance_tickets.assigned_staff_id
        ORDER BY CASE maintenance_tickets.status
                     WHEN 'Open' THEN 0 WHEN 'In Progress' THEN 1 ELSE 2 END,
                 CASE maintenance_tickets.priority
                     WHEN 'Urgent' THEN 0 WHEN 'High' THEN 1 ELSE 2 END,
                 maintenance_tickets.created_at DESC
        LIMIT 100
    """).fetchall()
    staff = conn.execute("""
        SELECT * FROM hostel_staff
        ORDER BY active DESC, campus, role, name
    """).fetchall()
    duty_rosters = conn.execute("""
        SELECT duty_rosters.*, hostel_staff.name AS staff_name,
               hostel_staff.role AS staff_role, hostel_staff.campus
        FROM duty_rosters
        JOIN hostel_staff ON hostel_staff.id = duty_rosters.staff_id
        ORDER BY duty_rosters.duty_date DESC, duty_rosters.shift
        LIMIT 100
    """).fetchall()
    vendors = conn.execute("""
        SELECT * FROM vendors ORDER BY active DESC, name
    """).fetchall()
    finance_entries = conn.execute("""
        SELECT finance_entries.*, vendors.name AS vendor_name
        FROM finance_entries
        LEFT JOIN vendors ON vendors.id = finance_entries.vendor_id
        ORDER BY finance_entries.entry_date DESC, finance_entries.id DESC
        LIMIT 100
    """).fetchall()
    finance_summary = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN entry_type = 'Income' THEN amount ELSE 0 END), 0)
                AS income_total,
            COALESCE(SUM(CASE WHEN entry_type = 'Expense' THEN amount ELSE 0 END), 0)
                AS expense_total,
            SUM(CASE WHEN payment_status = 'Due' THEN 1 ELSE 0 END) AS due_count
        FROM finance_entries
    """).fetchone()
    operations_counts = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM visitor_requests
             WHERE status = 'Pending') AS pending_visitors,
            (SELECT COUNT(*) FROM outpasses
             WHERE status = 'Pending') AS pending_outpasses,
            (SELECT COUNT(*) FROM maintenance_tickets
             WHERE status != 'Resolved') AS open_tickets,
            (SELECT COUNT(*) FROM hostel_staff
             WHERE active = 1) AS active_staff
    """).fetchone()
    operations_summary = {
        "pending_visitors": operations_counts["pending_visitors"],
        "pending_outpasses": operations_counts["pending_outpasses"],
        "open_tickets": operations_counts["open_tickets"],
        "active_staff": operations_counts["active_staff"],
    }
    conn.close()
    active_tab = request.args.get("tab", "overview")
    if active_tab not in {
        "overview", "notices", "meals", "gate", "facilities",
        "staff", "finance", "reports",
    }:
        active_tab = "overview"
    return render_template(
        "warden_operations.html",
        notices=notices,
        hostel_rules=hostel_rules,
        meal_menus=meal_menus,
        visitor_requests=visitor_requests,
        outpasses=outpasses,
        facilities=facilities,
        tickets=tickets,
        staff=staff,
        duty_rosters=duty_rosters,
        vendors=vendors,
        finance_entries=finance_entries,
        finance_summary=finance_summary,
        operations_summary=operations_summary,
        campuses=CAMPUSES,
        meal_types=MEAL_TYPES,
        ticket_priorities=TICKET_PRIORITIES,
        ticket_statuses=TICKET_STATUSES,
        active_tab=active_tab,
        today=hostel_today().isoformat(),
        warden_username=session.get("username"),
    )


@app.route("/warden/operations/notices", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_notice():
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    audience = request.form.get("audience", "All")
    expires_on = request.form.get("expires_on", "").strip() or None
    if (
        not title or len(title) > 160
        or not body or len(body) > 3000
        or audience not in ("All", "Students")
    ):
        flash("Enter a title, message, and valid audience.", "error")
        return operations_redirect("notices")
    if expires_on:
        try:
            expiration_date = date.fromisoformat(expires_on)
        except ValueError:
            expiration_date = None
        if expiration_date is None or expiration_date < date.today():
            flash("Notice expiry must be today or a future date.", "error")
            return operations_redirect("notices")
    conn = get_db()
    conn.execute("""
        INSERT INTO notices (title, body, audience, expires_on, created_by)
        VALUES (?, ?, ?, ?, ?)
    """, (title, body, audience, expires_on, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Notice published to the student portal.", "success")
    return operations_redirect("notices")


@app.route(
    "/warden/operations/notices/<int:notice_id>/status",
    methods=["POST"]
)
@role_required("warden")
@csrf_protect
def update_notice_status(notice_id):
    published = request.form.get("published", "")
    if published not in ("0", "1"):
        flash("Choose a valid notice publication status.", "error")
        return operations_redirect("notices")
    conn = get_db()
    result = conn.execute(
        "UPDATE notices SET published = ? WHERE id = ?",
        (int(published), notice_id)
    )
    conn.commit()
    conn.close()
    flash(
        "Notice publication status updated." if result.rowcount
        else "Notice not found.",
        "success" if result.rowcount else "error"
    )
    return operations_redirect("notices")


@app.route("/warden/operations/rules", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_hostel_rule():
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    category = request.form.get("category", "").strip()
    if (
        not title or len(title) > 160 or not body or len(body) > 3000
        or not category or len(category) > 80
    ):
        flash("Enter a rule title, category, and explanation.", "error")
        return operations_redirect("notices")
    conn = get_db()
    conn.execute("""
        INSERT INTO hostel_rules (title, body, category, created_by)
        VALUES (?, ?, ?, ?)
    """, (title, body, category, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Hostel rule published to student profiles.", "success")
    return operations_redirect("notices")


@app.route(
    "/warden/operations/rules/<int:rule_id>/status",
    methods=["POST"]
)
@role_required("warden")
@csrf_protect
def update_hostel_rule_status(rule_id):
    active = request.form.get("active", "")
    if active not in ("0", "1"):
        flash("Choose a valid rule publication status.", "error")
        return operations_redirect("notices")
    conn = get_db()
    result = conn.execute(
        "UPDATE hostel_rules SET active = ? WHERE id = ?",
        (int(active), rule_id)
    )
    conn.commit()
    conn.close()
    flash(
        "Rule publication status updated." if result.rowcount
        else "Hostel rule not found.",
        "success" if result.rowcount else "error"
    )
    return operations_redirect("notices")


@app.route("/warden/operations/meals", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_meal_menu():
    campus = request.form.get("campus", "").strip()
    meal_date = request.form.get("meal_date", "").strip()
    meal_type = request.form.get("meal_type", "").strip()
    menu_text = request.form.get("menu_text", "").strip()
    today = hostel_today()
    try:
        parsed_date = date.fromisoformat(meal_date)
    except ValueError:
        parsed_date = None
    if (
        campus not in CAMPUSES or meal_type not in MEAL_TYPES
        or parsed_date is None or parsed_date < today
        or parsed_date > today + timedelta(days=90)
        or not menu_text or len(menu_text) > 1000
    ):
        flash("Enter a valid campus, meal, date, and menu.", "error")
        return operations_redirect("meals")
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO meal_menus (
                campus, meal_date, meal_type, menu_text, created_by
            ) VALUES (?, ?, ?, ?, ?)
        """, (campus, meal_date, meal_type, menu_text, session["user_id"]))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("A menu already exists for that campus, date, and meal.", "error")
        return operations_redirect("meals")
    finally:
        conn.close()
    flash("Meal menu published.", "success")
    return operations_redirect("meals")


@app.route("/warden/operations/meals/daily", methods=["POST"])
@role_required("warden")
@csrf_protect
def save_daily_meal_menu():
    campus = request.form.get("campus", "").strip()
    meal_date = request.form.get("meal_date", "").strip()
    menus = {
        meal_type: request.form.get(
            f"menu_{meal_type.lower()}", ""
        ).strip()
        for meal_type in MEAL_TYPES
    }
    today = hostel_today()
    try:
        parsed_date = date.fromisoformat(meal_date)
    except ValueError:
        parsed_date = None
    if (
        campus not in CAMPUSES or parsed_date is None
        or parsed_date < today
        or parsed_date > today + timedelta(days=90)
        or any(not menu_text or len(menu_text) > 1000
               for menu_text in menus.values())
    ):
        flash(
            "Choose a hostel and valid date, and enter all four meal menus.",
            "error",
        )
        return operations_redirect("meals")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("""
            INSERT INTO meal_menus (
                campus, meal_date, meal_type, menu_text, created_by
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(campus, meal_date, meal_type) DO UPDATE SET
                menu_text = excluded.menu_text,
                created_by = excluded.created_by,
                created_at = CURRENT_TIMESTAMP
        """, [
            (campus, meal_date, meal_type, menu_text, session["user_id"])
            for meal_type, menu_text in menus.items()
        ])
        conn.commit()
    finally:
        conn.close()
    flash(f"Daily mess menu saved for {campus}.", "success")
    return operations_redirect("meals")


@app.route(
    "/warden/operations/visitors/<int:request_id>/decision",
    methods=["POST"]
)
@role_required("warden")
@csrf_protect
def decide_visitor_request(request_id):
    decision = request.form.get("decision", "")
    note = request.form.get("decision_note", "").strip()
    if decision not in ("Approved", "Rejected") or len(note) > 500:
        flash("Enter a valid visitor decision and note.", "error")
        return operations_redirect("gate")
    if decision == "Rejected" and not note:
        flash("Provide a reason when rejecting a visitor request.", "error")
        return operations_redirect("gate")
    conn = get_db()
    result = conn.execute("""
        UPDATE visitor_requests
        SET status = ?, decision_note = ?, decided_by = ?,
            decided_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'Pending'
    """, (decision, note or None, session["user_id"], request_id))
    conn.commit()
    conn.close()
    flash(
        "Visitor request updated." if result.rowcount
        else "Pending visitor request not found.",
        "success" if result.rowcount else "error"
    )
    return operations_redirect("gate")


@app.route(
    "/warden/operations/outpasses/<int:outpass_id>/decision",
    methods=["POST"]
)
@role_required("warden")
@csrf_protect
def decide_outpass(outpass_id):
    decision = request.form.get("decision", "")
    note = request.form.get("decision_note", "").strip()
    if decision not in ("Approved", "Rejected") or len(note) > 500:
        flash("Enter a valid outpass decision and note.", "error")
        return operations_redirect("gate")
    if decision == "Rejected" and not note:
        flash("Provide a reason when rejecting an outpass.", "error")
        return operations_redirect("gate")
    conn = get_db()
    result = conn.execute("""
        UPDATE outpasses
        SET status = ?, decision_note = ?, approved_by = ?,
            approved_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'Pending'
    """, (decision, note or None, session["user_id"], outpass_id))
    conn.commit()
    conn.close()
    flash(
        "Outpass decision recorded." if result.rowcount
        else "Pending outpass not found.",
        "success" if result.rowcount else "error"
    )
    return operations_redirect("gate")


@app.route(
    "/warden/operations/outpasses/<int:outpass_id>/gate",
    methods=["POST"]
)
@role_required("warden")
@csrf_protect
def record_outpass_gate_event(outpass_id):
    action = request.form.get("action", "")
    if action not in ("check-out", "check-in"):
        flash("Choose a valid gate action.", "error")
        return operations_redirect("gate")
    conn = get_db()
    if action == "check-out":
        result = conn.execute("""
            UPDATE outpasses
            SET checked_out_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'Approved'
                AND checked_out_at IS NULL
        """, (outpass_id,))
    else:
        result = conn.execute("""
            UPDATE outpasses
            SET checked_in_at = CURRENT_TIMESTAMP, status = 'Returned'
            WHERE id = ? AND status = 'Approved'
                AND checked_out_at IS NOT NULL AND checked_in_at IS NULL
        """, (outpass_id,))
    conn.commit()
    conn.close()
    flash(
        "Gate movement recorded." if result.rowcount
        else "Outpass is not in a state that allows this gate action.",
        "success" if result.rowcount else "error"
    )
    return operations_redirect("gate")


@app.route("/warden/operations/facilities", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_facility():
    campus = request.form.get("campus", "").strip()
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    location = request.form.get("location", "").strip()
    if (
        campus not in CAMPUSES or not name or len(name) > 100
        or not category or len(category) > 60
        or not location or len(location) > 120
    ):
        flash("Enter a valid campus, facility name, category, and location.", "error")
        return operations_redirect("facilities")
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO facilities (campus, name, category, location)
            VALUES (?, ?, ?, ?)
        """, (campus, name, category, location))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That facility is already listed at this location.", "error")
        return operations_redirect("facilities")
    finally:
        conn.close()
    flash("Facility added to the campus directory.", "success")
    return operations_redirect("facilities")


@app.route("/warden/operations/facilities/<int:facility_id>/status", methods=["POST"])
@role_required("warden")
@csrf_protect
def update_facility_status(facility_id):
    status = request.form.get("status", "")
    if status not in ("Operational", "Limited", "Closed"):
        flash("Choose a valid facility status.", "error")
        return operations_redirect("facilities")
    conn = get_db()
    result = conn.execute(
        "UPDATE facilities SET status = ? WHERE id = ?",
        (status, facility_id)
    )
    conn.commit()
    conn.close()
    flash(
        "Facility status updated." if result.rowcount
        else "Facility not found.",
        "success" if result.rowcount else "error"
    )
    return operations_redirect("facilities")


@app.route("/warden/operations/tickets/<int:ticket_id>", methods=["POST"])
@role_required("warden")
@csrf_protect
def update_maintenance_ticket(ticket_id):
    status = request.form.get("status", "")
    resolution_note = request.form.get("resolution_note", "").strip()
    assigned_staff_id = request.form.get("assigned_staff_id", type=int)
    if status not in TICKET_STATUSES or len(resolution_note) > 1000:
        flash("Choose a valid ticket status and resolution note.", "error")
        return operations_redirect("facilities")
    conn = get_db()
    ticket = conn.execute("""
        SELECT maintenance_tickets.id, students.campus
        FROM maintenance_tickets
        JOIN students ON students.id = maintenance_tickets.student_id
        WHERE maintenance_tickets.id = ?
    """, (ticket_id,)).fetchone()
    if ticket is None:
        conn.close()
        flash("Maintenance ticket not found.", "error")
        return operations_redirect("facilities")
    if status == "Resolved" and not resolution_note:
        conn.close()
        flash("Add a resolution note before closing a ticket.", "error")
        return operations_redirect("facilities")
    if assigned_staff_id is not None:
        staff = conn.execute("""
            SELECT id FROM hostel_staff
            WHERE id = ? AND campus = ? AND active = 1
        """, (assigned_staff_id, ticket["campus"])).fetchone()
        if staff is None:
            conn.close()
            flash("Choose active staff from the student's campus.", "error")
            return operations_redirect("facilities")
    result = conn.execute("""
        UPDATE maintenance_tickets
        SET status = ?, resolution_note = ?, assigned_staff_id = ?,
            updated_at = CURRENT_TIMESTAMP,
            resolved_at = CASE WHEN ? = 'Resolved'
                               THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id = ?
    """, (
        status, resolution_note or None, assigned_staff_id, status, ticket_id
    ))
    conn.commit()
    conn.close()
    flash("Maintenance ticket updated.", "success" if result.rowcount else "error")
    return operations_redirect("facilities")


@app.route("/warden/operations/staff", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_hostel_staff():
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    campus = request.form.get("campus", "").strip()
    phone = request.form.get("phone", "").strip()
    if (
        not name or len(name) > 100 or not role or len(role) > 80
        or campus not in CAMPUSES or not valid_contact_phone(phone)
    ):
        flash("Enter valid staff details and an optional valid phone.", "error")
        return operations_redirect("staff")
    conn = get_db()
    conn.execute("""
        INSERT INTO hostel_staff (name, role, campus, phone)
        VALUES (?, ?, ?, ?)
    """, (name, role, campus, phone or None))
    conn.commit()
    conn.close()
    flash("Staff member added.", "success")
    return operations_redirect("staff")


@app.route("/warden/operations/duties", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_duty_assignment():
    staff_id = request.form.get("staff_id", type=int)
    duty_date = request.form.get("duty_date", "").strip()
    shift = request.form.get("shift", "").strip()
    location = request.form.get("location", "").strip()
    notes = request.form.get("notes", "").strip()
    try:
        parsed_date = date.fromisoformat(duty_date)
    except ValueError:
        parsed_date = None
    if (
        staff_id is None or parsed_date is None
        or parsed_date < date.today()
        or parsed_date > date.today() + timedelta(days=365)
        or shift not in ("Day", "Evening", "Night")
        or not location or len(location) > 120 or len(notes) > 500
    ):
        flash("Enter a valid staff member, date, shift, and location.", "error")
        return operations_redirect("staff")
    conn = get_db()
    staff = conn.execute(
        "SELECT id FROM hostel_staff WHERE id = ? AND active = 1",
        (staff_id,)
    ).fetchone()
    if staff is None:
        conn.close()
        flash("Choose an active staff member.", "error")
        return operations_redirect("staff")
    try:
        conn.execute("""
            INSERT INTO duty_rosters (
                staff_id, duty_date, shift, location, notes, assigned_by
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            staff_id, duty_date, shift, location, notes or None,
            session["user_id"],
        ))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That staff member already has a duty in this shift.", "error")
        return operations_redirect("staff")
    finally:
        conn.close()
    flash("Duty assignment saved.", "success")
    return operations_redirect("staff")


@app.route("/warden/operations/vendors", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_vendor():
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    campus = request.form.get("campus", "").strip()
    contact_name = request.form.get("contact_name", "").strip()
    phone = request.form.get("phone", "").strip()
    if (
        not name or len(name) > 120 or not category or len(category) > 60
        or campus not in CAMPUSES or len(contact_name) > 100
        or not valid_contact_phone(phone)
    ):
        flash("Enter valid vendor details and an optional valid phone.", "error")
        return operations_redirect("finance")
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO vendors (name, category, campus, contact_name, phone)
            VALUES (?, ?, ?, ?, ?)
        """, (name, category, campus, contact_name or None, phone or None))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That vendor is already listed for this campus.", "error")
        return operations_redirect("finance")
    finally:
        conn.close()
    flash("Vendor added.", "success")
    return operations_redirect("finance")


@app.route("/warden/operations/finance", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_finance_entry():
    entry_type = request.form.get("entry_type", "")
    category = request.form.get("category", "").strip()
    amount = parse_payment_amount(request.form.get("amount", ""))
    entry_date = request.form.get("entry_date", "").strip()
    vendor_id = request.form.get("vendor_id", type=int)
    invoice_reference = request.form.get("invoice_reference", "").strip()
    payment_status = request.form.get("payment_status", "")
    description = request.form.get("description", "").strip()
    try:
        parsed_date = date.fromisoformat(entry_date)
    except ValueError:
        parsed_date = None
    if (
        entry_type not in ("Income", "Expense")
        or not category or len(category) > 80 or amount is None
        or parsed_date is None or parsed_date > date.today()
        or payment_status not in ("Recorded", "Due", "Paid")
        or len(invoice_reference) > 100
        or not description or len(description) > 1000
    ):
        flash("Enter a valid finance record.", "error")
        return operations_redirect("finance")
    conn = get_db()
    if vendor_id is not None and not conn.execute(
        "SELECT id FROM vendors WHERE id = ? AND active = 1", (vendor_id,)
    ).fetchone():
        conn.close()
        flash("Choose an active vendor.", "error")
        return operations_redirect("finance")
    conn.execute("""
        INSERT INTO finance_entries (
            entry_type, category, amount, entry_date, vendor_id,
            invoice_reference, payment_status, description, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        entry_type, category, str(amount), entry_date, vendor_id,
        invoice_reference or None, payment_status, description,
        session["user_id"],
    ))
    conn.commit()
    conn.close()
    flash("Finance record saved. No payment was processed.", "success")
    return operations_redirect("finance")


@app.route("/warden/reports/<report_type>.csv")
@role_required("warden")
def download_operations_report(report_type):
    queries = {
        "students": (
            "Student roster",
            """SELECT students.name, students.usn, students.department,
                      students.year, students.campus, rooms.room_number,
                      beds.bed_label
               FROM students
               LEFT JOIN rooms ON rooms.id = students.room_id
               LEFT JOIN beds ON beds.student_id = students.id
               ORDER BY students.campus, students.name COLLATE NOCASE""",
        ),
        "emergency-contacts": (
            "Confidential emergency contact list",
            """SELECT students.name, students.usn, students.campus,
                      students.guardian_name, students.guardian_phone,
                      students.emergency_phone, students.home_address
               FROM students
               WHERE students.guardian_name IS NOT NULL
                  OR students.guardian_phone IS NOT NULL
                  OR students.emergency_phone IS NOT NULL
                  OR students.home_address IS NOT NULL
               ORDER BY students.campus, students.name COLLATE NOCASE""",
        ),
        "attendance": (
            "Attendance report",
            """SELECT attendance_records.attendance_date, students.name,
                      students.usn, students.campus, attendance_records.status,
                      attendance_records.marked_at
               FROM attendance_records
               JOIN students ON students.id = attendance_records.student_id
               ORDER BY attendance_records.attendance_date DESC,
                        students.name COLLATE NOCASE""",
        ),
        "fees": (
            "Fee receipt report",
            """SELECT students.name, students.usn, students.campus,
                      fee_receipts.amount, fee_receipts.payment_date,
                      fee_receipts.academic_year, fee_receipts.status,
                      fee_receipts.uploaded_at, fee_receipts.verified_at
               FROM fee_receipts
               JOIN students ON students.id = fee_receipts.student_id
               ORDER BY fee_receipts.uploaded_at DESC""",
        ),
        "gate": (
            "Visitor and outpass report",
            """SELECT 'Visitor' AS request_type, students.name, students.usn,
                      visitor_requests.visit_date AS scheduled_at,
                      visitor_requests.status, visitor_requests.purpose AS details
               FROM visitor_requests
               JOIN students ON students.id = visitor_requests.student_id
               UNION ALL
               SELECT 'Outpass', students.name, students.usn,
                      outpasses.departure_at, outpasses.status, outpasses.reason
               FROM outpasses
               JOIN students ON students.id = outpasses.student_id
               ORDER BY scheduled_at DESC""",
        ),
        "maintenance": (
            "Maintenance report",
            """SELECT students.name, students.usn, students.campus,
                      maintenance_tickets.subject, maintenance_tickets.priority,
                      maintenance_tickets.status, maintenance_tickets.created_at,
                      maintenance_tickets.resolution_note
               FROM maintenance_tickets
               JOIN students ON students.id = maintenance_tickets.student_id
               ORDER BY maintenance_tickets.created_at DESC""",
        ),
        "finance": (
            "Finance register",
            """SELECT finance_entries.entry_type, finance_entries.category,
                      finance_entries.amount, finance_entries.entry_date,
                      vendors.name AS vendor, finance_entries.invoice_reference,
                      finance_entries.payment_status, finance_entries.description
               FROM finance_entries
               LEFT JOIN vendors ON vendors.id = finance_entries.vendor_id
               ORDER BY finance_entries.entry_date DESC""",
        ),
    }
    if report_type not in queries:
        abort(404)

    report_title, query = queries[report_type]
    conn = get_db()
    report_rows = conn.execute(query).fetchall()
    conn.close()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([report_title])
    if report_rows:
        writer.writerow(report_rows[0].keys())
        for row in report_rows:
            writer.writerow([
                csv_safe_cell(value) for value in tuple(row)
            ])
    else:
        writer.writerow(["No records"])
    response = app.response_class(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=hostel-{report_type}-report.csv",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
    return response


def csv_safe_cell(value):
    if value is None:
        return ""
    text = str(value)
    if text[:1] in ("\t", "\r", "\n") or text.lstrip().startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + text
    return text


def parse_attendance_date(value):
    try:
        selected_date = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return selected_date if selected_date <= date.today() else None


@app.route("/warden/attendance", methods=["GET", "POST"])
@role_required("warden")
@csrf_protect
def warden_attendance():
    if request.method == "POST":
        date_value = request.form.get("attendance_date", "")
        selected_date = parse_attendance_date(date_value)
        if selected_date is None:
            flash("Choose a valid attendance date up to today.", "error")
            return redirect(url_for("warden_attendance"))

        campus_filter = request.form.get("campus", "").strip()
        if campus_filter and campus_filter not in CAMPUSES:
            flash("Choose a valid campus.", "error")
            return redirect(url_for(
                "warden_attendance",
                date=selected_date.isoformat()
            ))

        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            student_query = "SELECT id FROM students"
            parameters = []
            if campus_filter:
                student_query += " WHERE campus = ?"
                parameters.append(campus_filter)
            student_ids = [
                row["id"] for row in conn.execute(
                    student_query, parameters
                ).fetchall()
            ]
            if not student_ids:
                conn.rollback()
                flash("No students match this campus selection.", "error")
                return redirect(url_for(
                    "warden_attendance",
                    date=selected_date.isoformat(),
                    campus=campus_filter
                ))

            records = []
            for student_id in student_ids:
                status = request.form.get(f"attendance_{student_id}", "")
                if status not in ATTENDANCE_STATUSES:
                    conn.rollback()
                    flash(
                        "Choose an attendance status for every listed student.",
                        "error"
                    )
                    return redirect(url_for(
                        "warden_attendance",
                        date=selected_date.isoformat(),
                        campus=campus_filter
                    ))
                records.append((
                    student_id,
                    selected_date.isoformat(),
                    status,
                    session["user_id"]
                ))

            conn.executemany("""
                INSERT INTO attendance_records (
                    student_id, attendance_date, status, marked_by_user_id
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(student_id, attendance_date) DO UPDATE SET
                    status = excluded.status,
                    marked_by_user_id = excluded.marked_by_user_id,
                    marked_at = CURRENT_TIMESTAMP
            """, records)
            conn.commit()
        finally:
            conn.close()

        flash(f"Attendance saved for {len(records)} students.", "success")
        return redirect(url_for(
            "warden_attendance",
            date=selected_date.isoformat(),
            campus=campus_filter
        ))

    today = date.today()
    date_value = request.args.get("date", today.isoformat())
    selected_date = parse_attendance_date(date_value)
    if selected_date is None:
        flash("Choose a valid attendance date up to today.", "error")
        selected_date = today

    campus_filter = request.args.get("campus", "").strip()
    if campus_filter and campus_filter not in CAMPUSES:
        flash("Choose a valid campus.", "error")
        campus_filter = ""

    conn = get_db()
    try:
        query = """
            SELECT students.id, students.name, students.usn, students.campus,
                   rooms.room_number,
                   attendance_records.status AS attendance_status
            FROM students
            LEFT JOIN rooms ON rooms.id = students.room_id
            LEFT JOIN attendance_records
                ON attendance_records.student_id = students.id
                AND attendance_records.attendance_date = ?
        """
        parameters = [selected_date.isoformat()]
        if campus_filter:
            query += " WHERE students.campus = ?"
            parameters.append(campus_filter)
        query += """
            ORDER BY students.campus, rooms.room_number IS NULL,
                     rooms.room_number, students.name COLLATE NOCASE
        """
        students = conn.execute(query, parameters).fetchall()
    finally:
        conn.close()

    status_counts = {status: 0 for status in ATTENDANCE_STATUSES}
    for student in students:
        if student["attendance_status"] in status_counts:
            status_counts[student["attendance_status"]] += 1
    unmarked_count = len(students) - sum(status_counts.values())

    return render_template(
        "attendance.html",
        attendance_date=selected_date,
        attendance_date_label=selected_date.strftime("%A, %B %d, %Y"),
        campuses=CAMPUSES,
        campus_filter=campus_filter,
        students=students,
        attendance_statuses=ATTENDANCE_STATUSES,
        status_counts=status_counts,
        unmarked_count=unmarked_count,
        today=today.isoformat(),
        warden_username=session.get("username")
    )


@app.route("/student")
@role_required("student")
def student_dashboard():
    student_id = session.get("student_id")
    conn = get_db()
    student = conn.execute("""
        SELECT students.*, rooms.room_number, beds.bed_label,
               buildings.name AS building_name, floors.name AS floor_name
        FROM students
        LEFT JOIN rooms ON rooms.id = students.room_id
        LEFT JOIN beds ON beds.student_id = students.id
        LEFT JOIN buildings ON buildings.id = rooms.building_id
        LEFT JOIN floors ON floors.id = rooms.floor_id
        JOIN users ON users.student_id = students.id
        WHERE students.id = ?
    """, (student_id,)).fetchone()
    if student is None:
        conn.close()
        session.clear()
        flash("Student account not found. Please log in again.", "error")
        return redirect(url_for("login"))

    complaints = conn.execute("""
        SELECT id, body, status, created_at, resolved_at
        FROM complaints
        WHERE student_id = ?
        ORDER BY created_at DESC
    """, (student_id,)).fetchall()
    notices = conn.execute("""
        SELECT title, body, audience, created_at
        FROM notices
        WHERE published = 1 AND audience IN ('All', 'Students')
            AND (expires_on IS NULL OR expires_on >= date('now', 'localtime'))
        ORDER BY created_at DESC
        LIMIT 8
    """).fetchall()
    hostel_rules = conn.execute("""
        SELECT title, body, category, created_at
        FROM hostel_rules
        WHERE active = 1
        ORDER BY category, title
        LIMIT 30
    """).fetchall()
    today = hostel_today().isoformat()
    all_meal_menus = conn.execute("""
        SELECT meal_menus.*,
               SUM(CASE WHEN meal_reservations.status = 'Reserved'
                        THEN 1 ELSE 0 END) AS reservation_count,
               MAX(CASE WHEN meal_reservations.student_id = ?
                        THEN meal_reservations.status END) AS my_reservation
        FROM meal_menus
        LEFT JOIN meal_reservations
            ON meal_reservations.menu_id = meal_menus.id
        WHERE meal_menus.campus = ?
            AND meal_menus.meal_date >= ?
        GROUP BY meal_menus.id
        ORDER BY meal_menus.meal_date,
                 CASE meal_menus.meal_type
                     WHEN 'Breakfast' THEN 1 WHEN 'Lunch' THEN 2
                     WHEN 'Snacks' THEN 3 ELSE 4 END
        LIMIT 100
    """, (student_id, student["campus"], today)).fetchall()
    today_meals = {
        meal["meal_type"]: meal
        for meal in all_meal_menus
        if meal["meal_date"] == today
    }
    meal_menus = [
        meal for meal in all_meal_menus if meal["meal_date"] > today
    ][:12]
    visitor_requests = conn.execute("""
        SELECT * FROM visitor_requests
        WHERE student_id = ?
        ORDER BY visit_date DESC, visit_time DESC, id DESC
        LIMIT 10
    """, (student_id,)).fetchall()
    outpasses = conn.execute("""
        SELECT * FROM outpasses
        WHERE student_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 10
    """, (student_id,)).fetchall()
    student_tickets = conn.execute("""
        SELECT maintenance_tickets.*, facilities.name AS facility_name
        FROM maintenance_tickets
        LEFT JOIN facilities ON facilities.id = maintenance_tickets.facility_id
        WHERE maintenance_tickets.student_id = ?
        ORDER BY maintenance_tickets.created_at DESC
        LIMIT 10
    """, (student_id,)).fetchall()
    facilities = conn.execute("""
        SELECT id, name, category, location
        FROM facilities
        WHERE campus = ? AND status != 'Closed'
        ORDER BY category, name
    """, (student["campus"],)).fetchall()
    today_attendance = conn.execute("""
        SELECT status FROM attendance_records
        WHERE student_id = ? AND attendance_date = date('now', 'localtime')
    """, (student_id,)).fetchone()
    conn.close()
    fee_receipts, fee_summary = student_fee_receipts(student_id)

    return render_template(
        "student_dashboard.html",
        student=student,
        complaints=complaints,
        fee_receipts=fee_receipts,
        fee_summary=fee_summary,
        notices=notices,
        hostel_rules=hostel_rules,
        meal_types=MEAL_TYPES,
        today_meals=today_meals,
        meal_menus=meal_menus,
        visitor_requests=visitor_requests,
        outpasses=outpasses,
        student_tickets=student_tickets,
        facilities=facilities,
        ticket_priorities=TICKET_PRIORITIES,
        today_attendance=today_attendance["status"] if today_attendance else None,
        today=today,
    )


@app.route("/student/profile", methods=["POST"])
@role_required("student")
@csrf_protect
def update_student_profile():
    guardian_name = request.form.get("guardian_name", "").strip()
    guardian_phone = request.form.get("guardian_phone", "").strip()
    emergency_phone = request.form.get("emergency_phone", "").strip()
    home_address = request.form.get("home_address", "").strip()
    if not guardian_name:
        flash("Enter a parent or guardian name.", "error")
    elif len(guardian_name) > 100:
        flash("Guardian name must be 100 characters or fewer.", "error")
    elif not valid_contact_phone(guardian_phone, required=True):
        flash("Enter a valid guardian phone number.", "error")
    elif not valid_contact_phone(emergency_phone, required=True):
        flash("Enter a valid emergency contact number.", "error")
    elif not home_address:
        flash("Enter a permanent home address for emergencies.", "error")
    elif len(home_address) > 500:
        flash("Home address must be 500 characters or fewer.", "error")
    else:
        conn = get_db()
        conn.execute("""
            UPDATE students
            SET guardian_name = ?, guardian_phone = ?, emergency_phone = ?,
                home_address = ?
            WHERE id = ?
        """, (
            guardian_name or None,
            guardian_phone or None,
            emergency_phone or None,
            home_address or None,
            session["student_id"],
        ))
        conn.commit()
        conn.close()
        flash("Emergency contact details updated.", "success")
    return redirect(url_for("student_dashboard") + "#my-profile")


@app.route("/student/meals/<int:menu_id>/reservation", methods=["POST"])
@role_required("student")
@csrf_protect
def update_meal_reservation(menu_id):
    reservation_action = request.form.get("action", "")
    if reservation_action not in ("reserve", "cancel"):
        flash("Choose a valid meal reservation action.", "error")
        return redirect(url_for("student_dashboard") + "#meals")

    conn = get_db()
    menu = conn.execute("""
        SELECT meal_menus.id FROM meal_menus
        JOIN students ON students.id = ?
        WHERE meal_menus.id = ? AND meal_menus.campus = students.campus
            AND meal_menus.meal_date >= ?
    """, (session["student_id"], menu_id, hostel_today().isoformat())).fetchone()
    if menu is None:
        conn.close()
        flash("That meal menu is no longer available.", "error")
        return redirect(url_for("student_dashboard") + "#meals")

    if reservation_action == "reserve":
        conn.execute("""
            INSERT INTO meal_reservations (student_id, menu_id, status)
            VALUES (?, ?, 'Reserved')
            ON CONFLICT(student_id, menu_id) DO UPDATE SET
                status = 'Reserved', updated_at = CURRENT_TIMESTAMP
        """, (session["student_id"], menu_id))
    else:
        conn.execute("""
            UPDATE meal_reservations
            SET status = 'Cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE student_id = ? AND menu_id = ?
        """, (session["student_id"], menu_id))
    conn.commit()
    conn.close()
    flash(
        "Meal reserved." if reservation_action == "reserve"
        else "Meal reservation cancelled.",
        "success"
    )
    return redirect(url_for("student_dashboard") + "#meals")


@app.route("/student/visitors", methods=["POST"])
@role_required("student")
@csrf_protect
def submit_visitor_request():
    visitor_name = request.form.get("visitor_name", "").strip()
    relationship = request.form.get("relationship", "").strip()
    visitor_phone = request.form.get("visitor_phone", "").strip()
    visit_date = request.form.get("visit_date", "").strip()
    visit_time = request.form.get("visit_time", "").strip()
    purpose = request.form.get("purpose", "").strip()
    try:
        parsed_date = date.fromisoformat(visit_date)
    except ValueError:
        parsed_date = None

    try:
        datetime.strptime(visit_time, "%H:%M")
    except ValueError:
        valid_visit_time = False
    else:
        valid_visit_time = True
    if (
        not visitor_name or len(visitor_name) > 100
        or not relationship or len(relationship) > 60
        or not valid_contact_phone(visitor_phone, required=True)
        or parsed_date is None or parsed_date < date.today()
        or parsed_date > date.today() + timedelta(days=90)
        or not valid_visit_time
        or not purpose or len(purpose) > 500
    ):
        flash("Check the visitor details and choose a visit date within 90 days.", "error")
        return redirect(url_for("student_dashboard") + "#visitors")

    conn = get_db()
    conn.execute("""
        INSERT INTO visitor_requests (
            student_id, visitor_name, relationship, visitor_phone,
            visit_date, visit_time, purpose
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        session["student_id"], visitor_name, relationship, visitor_phone,
        visit_date, visit_time, purpose,
    ))
    conn.commit()
    conn.close()
    flash("Visitor request sent to the warden for review.", "success")
    return redirect(url_for("student_dashboard") + "#visitors")


@app.route("/student/outpasses", methods=["POST"])
@role_required("student")
@csrf_protect
def submit_outpass_request():
    destination = request.form.get("destination", "").strip()
    reason = request.form.get("reason", "").strip()
    departure = parse_local_datetime(request.form.get("departure_at", ""))
    expected_return = parse_local_datetime(
        request.form.get("expected_return_at", "")
    )
    current_time = datetime.now().replace(second=0, microsecond=0)

    if (
        not destination or len(destination) > 120
        or not reason or len(reason) > 500
        or departure is None or expected_return is None
        or departure <= current_time
        or expected_return <= departure
        or expected_return > departure + timedelta(days=30)
    ):
        flash("Enter a valid destination, reason, and future return window.", "error")
        return redirect(url_for("student_dashboard") + "#outpasses")

    conn = get_db()
    conn.execute("""
        INSERT INTO outpasses (
            student_id, destination, reason, departure_at, expected_return_at
        ) VALUES (?, ?, ?, ?, ?)
    """, (
        session["student_id"], destination, reason,
        departure.isoformat(timespec="minutes"),
        expected_return.isoformat(timespec="minutes"),
    ))
    conn.commit()
    conn.close()
    flash("Outpass request sent to the warden for approval.", "success")
    return redirect(url_for("student_dashboard") + "#outpasses")


@app.route("/student/tickets", methods=["POST"])
@role_required("student")
@csrf_protect
def submit_maintenance_ticket():
    subject = request.form.get("subject", "").strip()
    description = request.form.get("description", "").strip()
    priority = request.form.get("priority", "Normal").strip()
    facility_id = request.form.get("facility_id", type=int)
    if (
        not subject or len(subject) > 120
        or not description or len(description) > 2000
        or priority not in TICKET_PRIORITIES
    ):
        flash("Enter a subject and description and choose a valid priority.", "error")
        return redirect(url_for("student_dashboard") + "#maintenance")

    conn = get_db()
    if facility_id is not None:
        facility = conn.execute("""
            SELECT facilities.id FROM facilities
            JOIN students ON students.id = ?
            WHERE facilities.id = ? AND facilities.campus = students.campus
                AND facilities.status != 'Closed'
        """, (session["student_id"], facility_id)).fetchone()
        if facility is None:
            conn.close()
            flash("Choose a facility available at your campus.", "error")
            return redirect(url_for("student_dashboard") + "#maintenance")
    conn.execute("""
        INSERT INTO maintenance_tickets (
            student_id, facility_id, subject, description, priority
        ) VALUES (?, ?, ?, ?, ?)
    """, (
        session["student_id"], facility_id, subject, description, priority,
    ))
    conn.commit()
    conn.close()
    flash("Maintenance ticket submitted.", "success")
    return redirect(url_for("student_dashboard") + "#maintenance")


@app.route("/student/fees/upload", methods=["POST"])
@role_required("student")
@csrf_protect
def submit_fee_receipt():
    student_id = session.get("student_id")
    if student_id is None:
        session.clear()
        flash("Student account not found. Please log in again.", "error")
        return redirect(url_for("login"))

    amount = parse_payment_amount(request.form.get("amount", "").strip())
    payment_date = request.form.get("payment_date", "").strip()
    transaction_id = request.form.get("transaction_id", "").strip()
    academic_year = request.form.get("academic_year", "").strip()
    correction_value = request.form.get("correction_of_id", "").strip()

    if amount is None:
        flash("Enter a valid payment amount with up to two decimal places.", "error")
        return redirect(url_for("student_dashboard") + "#hostel-fees")
    try:
        parsed_payment_date = date.fromisoformat(payment_date)
    except ValueError:
        flash("Enter a valid payment date.", "error")
        return redirect(url_for("student_dashboard") + "#hostel-fees")
    if parsed_payment_date.isoformat() != payment_date:
        flash("Enter a valid payment date.", "error")
        return redirect(url_for("student_dashboard") + "#hostel-fees")
    if not transaction_id or len(transaction_id) > MAX_TRANSACTION_ID_LENGTH:
        flash("Enter a transaction ID or UTR (up to 128 characters).", "error")
        return redirect(url_for("student_dashboard") + "#hostel-fees")
    if not valid_academic_year(academic_year):
        flash("Enter an academic year in YYYY-YYYY format.", "error")
        return redirect(url_for("student_dashboard") + "#hostel-fees")

    correction_of_id = None
    if correction_value:
        if not correction_value.isdecimal():
            flash("The receipt selected for correction is invalid.", "error")
            return redirect(url_for("student_dashboard") + "#hostel-fees")
        correction_of_id = int(correction_value)

    try:
        content, extension, _ = read_receipt_upload(
            request.files.get("receipt_file")
        )
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("student_dashboard") + "#hostel-fees")

    conn = get_db()
    try:
        student = conn.execute(
            "SELECT id FROM students WHERE id = ?", (student_id,)
        ).fetchone()
        if student is None:
            session.clear()
            flash("Student account not found. Please log in again.", "error")
            return redirect(url_for("login"))

        if correction_of_id is not None:
            rejected_receipt = conn.execute("""
                SELECT id
                FROM fee_receipts
                WHERE id = ? AND student_id = ? AND status = 'Rejected'
            """, (correction_of_id, student_id)).fetchone()
            if rejected_receipt is None:
                flash("Only your rejected receipts can be corrected.", "error")
                return redirect(url_for("student_dashboard") + "#hostel-fees")
            active_correction = conn.execute("""
                SELECT id
                FROM fee_receipts
                WHERE resubmission_of = ?
                    AND status IN ('Pending', 'Approved')
                LIMIT 1
            """, (correction_of_id,)).fetchone()
            if active_correction is not None:
                flash("A corrected receipt is already awaiting review or approved.", "error")
                return redirect(url_for("student_dashboard") + "#hostel-fees")

        filename = f"{secrets.token_hex(16)}{extension}"
        file_path = receipt_file_path(filename)
        file_descriptor = os.open(
            file_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "wb") as receipt_file:
            receipt_file.write(content)

        try:
            conn.execute("""
                INSERT INTO fee_receipts (
                    student_id, amount, payment_date, transaction_id,
                    academic_year, receipt_filename, resubmission_of
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                student_id,
                str(amount),
                payment_date,
                transaction_id,
                academic_year,
                filename,
                correction_of_id,
            ))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            os.remove(file_path)
            raise
    finally:
        conn.close()

    flash("Receipt uploaded. Its status is Pending until a warden reviews it.", "success")
    return redirect(url_for("student_dashboard") + "#hostel-fees")


@app.route("/warden/fees/<int:receipt_id>/approve", methods=["POST"])
@role_required("warden")
@csrf_protect
def approve_fee_receipt(receipt_id):
    conn = get_db()
    try:
        result = conn.execute("""
            UPDATE fee_receipts
            SET status = 'Approved', rejection_reason = NULL,
                verified_by = ?, verified_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'Pending'
        """, (session["user_id"], receipt_id))
        conn.commit()
    finally:
        conn.close()

    if result.rowcount:
        flash("Fee receipt approved.", "success")
    else:
        flash("Pending fee receipt not found.", "error")
    return redirect(url_for("warden_dashboard") + "#fee-verification")


@app.route("/warden/fees/<int:receipt_id>/reject", methods=["POST"])
@role_required("warden")
@csrf_protect
def reject_fee_receipt(receipt_id):
    rejection_reason = request.form.get("rejection_reason", "").strip()
    if not rejection_reason:
        flash("Enter a reason before rejecting a receipt.", "error")
        return redirect(url_for("warden_dashboard") + "#fee-verification")
    if len(rejection_reason) > MAX_REJECTION_REASON_LENGTH:
        flash(
            f"Rejection reasons must be {MAX_REJECTION_REASON_LENGTH} characters or fewer.",
            "error"
        )
        return redirect(url_for("warden_dashboard") + "#fee-verification")

    conn = get_db()
    try:
        result = conn.execute("""
            UPDATE fee_receipts
            SET status = 'Rejected', rejection_reason = ?,
                verified_by = ?, verified_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'Pending'
        """, (rejection_reason, session["user_id"], receipt_id))
        conn.commit()
    finally:
        conn.close()

    if result.rowcount:
        flash("Fee receipt rejected. The student can upload a corrected receipt.", "success")
    else:
        flash("Pending fee receipt not found.", "error")
    return redirect(url_for("warden_dashboard") + "#fee-verification")


@app.route("/complaints", methods=["POST"])
@role_required("student")
@csrf_protect
def submit_complaint():
    body = request.form.get("body", "").strip()
    if not body:
        flash("Describe the issue before submitting.", "error")
        return redirect(url_for("student_dashboard"))
    if len(body) > 2000:
        flash("Complaint details must be 2,000 characters or fewer.", "error")
        return redirect(url_for("student_dashboard"))

    conn = get_db()
    conn.execute(
        "INSERT INTO complaints (student_id, body) VALUES (?, ?)",
        (session["student_id"], body)
    )
    conn.commit()
    conn.close()

    flash("Your complaint was submitted to the warden.", "success")
    return redirect(url_for("student_dashboard"))


@app.route("/complaints/<int:complaint_id>/resolve", methods=["POST"])
@role_required("warden")
@csrf_protect
def resolve_complaint(complaint_id):
    conn = get_db()
    result = conn.execute("""
        UPDATE complaints
        SET status = 'Resolved', resolved_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'Pending'
    """, (complaint_id,))
    conn.commit()
    conn.close()

    if result.rowcount:
        flash("Complaint marked as resolved.", "success")
    else:
        flash("Pending complaint not found.", "error")
    return redirect(url_for("warden_dashboard"))


@app.route("/logout", methods=["POST"])
@csrf_protect
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/warden/rooms", methods=["POST"])
@role_required("warden")
@csrf_protect
def create_hostel_room():
    campus = request.form.get("campus", "").strip()
    building_name = request.form.get("building_name", "").strip()
    room_number = request.form.get("room_number", "").strip()
    try:
        floor_number = int(request.form.get("floor_number", ""))
        capacity = int(request.form.get("capacity", ""))
    except ValueError:
        flash("Floor and bed capacity must be whole numbers.", "error")
        return redirect(url_for("warden_dashboard") + "#room-availability")

    if (
        campus not in CAMPUSES
        or not building_name
        or len(building_name) > 60
        or not room_number
        or len(room_number) > 30
        or not 1 <= floor_number <= 50
        or not 1 <= capacity <= 16
    ):
        flash("Enter a valid campus, block, floor, room, and bed capacity.", "error")
        return redirect(url_for("warden_dashboard") + "#room-availability")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            INSERT INTO buildings (name, campus) VALUES (?, ?)
            ON CONFLICT(name, campus) DO NOTHING
        """, (building_name, campus))
        building_id = conn.execute("""
            SELECT id FROM buildings WHERE name = ? AND campus = ?
        """, (building_name, campus)).fetchone()["id"]
        conn.execute("""
            INSERT INTO floors (building_id, floor_number, name)
            VALUES (?, ?, ?)
            ON CONFLICT(building_id, floor_number) DO NOTHING
        """, (building_id, floor_number, f"Floor {floor_number}"))
        floor_id = conn.execute("""
            SELECT id FROM floors
            WHERE building_id = ? AND floor_number = ?
        """, (building_id, floor_number)).fetchone()["id"]
        room_id = conn.execute("""
            INSERT INTO rooms (
                room_number, capacity, campus, building_id, floor_id
            ) VALUES (?, ?, ?, ?, ?)
        """, (room_number, capacity, campus, building_id, floor_id)).lastrowid
        conn.executemany("""
            INSERT INTO beds (room_id, bed_label)
            VALUES (?, ?)
        """, [
            (room_id, f"Bed {bed_number}")
            for bed_number in range(1, capacity + 1)
        ])
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("That room number already exists. Choose a unique room number.", "error")
        return redirect(url_for("warden_dashboard") + "#room-availability")
    finally:
        conn.close()

    flash(f"Room {room_number} and its {capacity} bed slots were created.", "success")
    return redirect(url_for("warden_dashboard") + "#room-availability")


@app.route("/warden/beds/<int:bed_id>/status", methods=["POST"])
@role_required("warden")
@csrf_protect
def update_bed_status(bed_id):
    status = request.form.get("status", "")
    if status not in ("Available", "Blocked", "Maintenance"):
        flash("Choose a valid bed status.", "error")
        return redirect(url_for("warden_dashboard") + "#room-availability")

    conn = get_db()
    result = conn.execute("""
        UPDATE beds SET status = ?
        WHERE id = ? AND student_id IS NULL
    """, (status, bed_id))
    conn.commit()
    conn.close()
    if result.rowcount:
        flash("Bed status updated.", "success")
    else:
        flash("Bed not found or currently occupied.", "error")
    return redirect(url_for("warden_dashboard") + "#room-availability")


@app.route(
    "/warden/students/<int:student_id>/allocation",
    methods=["GET", "POST"]
)
@role_required("warden")
@csrf_protect
def allocate_bed(student_id):
    conn = get_db()
    if request.method == "POST":
        bed_id = request.form.get("bed_id", type=int)
        if bed_id is None:
            conn.close()
            flash("Select an available bed before assigning the student.", "error")
            return redirect(url_for("allocate_bed", student_id=student_id))

        try:
            conn.execute("BEGIN IMMEDIATE")
            student = conn.execute(
                "SELECT id, room_id, campus FROM students WHERE id = ?",
                (student_id,)
            ).fetchone()
            bed = conn.execute("""
                SELECT beds.id, beds.room_id
                FROM beds
                JOIN rooms ON rooms.id = beds.room_id
                WHERE beds.id = ? AND beds.status = 'Available'
                    AND beds.student_id IS NULL AND rooms.campus = ?
            """, (bed_id, student["campus"] if student else "")).fetchone()
            if student is None or student["room_id"] is not None:
                conn.rollback()
                flash("Student not found or already has a room.", "error")
                return redirect(url_for("warden_dashboard") + "#student-directory")
            if bed is None:
                conn.rollback()
                flash("That bed is no longer available for this student's campus.", "error")
                return redirect(url_for("allocate_bed", student_id=student_id))

            assigned = conn.execute("""
                UPDATE beds SET student_id = ?
                WHERE id = ? AND student_id IS NULL AND status = 'Available'
            """, (student_id, bed_id))
            if assigned.rowcount != 1:
                conn.rollback()
                flash("That bed was just assigned. Choose another bed.", "error")
                return redirect(url_for("allocate_bed", student_id=student_id))
            conn.execute(
                "UPDATE students SET room_id = ? WHERE id = ?",
                (bed["room_id"], student_id)
            )
            conn.commit()
        finally:
            conn.close()

        flash("Student assigned to the selected bed.", "success")
        return redirect(url_for("warden_dashboard") + "#student-directory")

    student = conn.execute("""
        SELECT id, name, usn, campus, room_id FROM students WHERE id = ?
    """, (student_id,)).fetchone()
    if student is None:
        conn.close()
        abort(404)
    if student["room_id"] is not None:
        conn.close()
        flash("Student already has a room.", "error")
        return redirect(url_for("warden_dashboard") + "#student-directory")

    buildings = conn.execute("""
        SELECT id, name FROM buildings
        WHERE campus = ? ORDER BY name
    """, (student["campus"],)).fetchall()
    floors = conn.execute("""
        SELECT floors.id, floors.building_id, floors.name, floors.floor_number
        FROM floors
        JOIN buildings ON buildings.id = floors.building_id
        WHERE buildings.campus = ?
        ORDER BY buildings.name, floors.floor_number
    """, (student["campus"],)).fetchall()
    rooms = conn.execute("""
        SELECT id, room_number, building_id, floor_id
        FROM rooms WHERE campus = ?
        ORDER BY building_id, floor_id, room_number
    """, (student["campus"],)).fetchall()
    beds = conn.execute("""
        SELECT beds.id, beds.room_id, beds.bed_label
        FROM beds
        JOIN rooms ON rooms.id = beds.room_id
        WHERE rooms.campus = ? AND beds.status = 'Available'
            AND beds.student_id IS NULL
        ORDER BY rooms.building_id, rooms.floor_id, rooms.room_number,
                 beds.bed_label
    """, (student["campus"],)).fetchall()
    conn.close()
    return render_template(
        "allocate_bed.html",
        student=student,
        buildings=buildings,
        floors=floors,
        rooms=rooms,
        beds=beds,
    )


# Automatically allocate the next available bed
@app.route("/allocate/<int:student_id>", methods=["POST"])
@role_required("warden")
@csrf_protect
def allocate(student_id):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        student = conn.execute(
            "SELECT id, room_id, campus FROM students WHERE id = ?",
            (student_id,)
        ).fetchone()

        if student is None:
            conn.rollback()
            flash("Student not found.", "error")
        elif student["room_id"] is not None:
            conn.rollback()
            flash("Student already has a room.", "error")
        else:
            bed = conn.execute("""
                SELECT beds.id, beds.room_id, beds.bed_label, rooms.room_number
                FROM beds
                JOIN rooms ON rooms.id = beds.room_id
                WHERE rooms.campus = ? AND beds.status = 'Available'
                    AND beds.student_id IS NULL
                ORDER BY rooms.room_number, beds.bed_label
                LIMIT 1
            """, (student["campus"],)).fetchone()

            if bed is None:
                conn.rollback()
                flash("Sorry! No beds are available in this campus.", "error")
            else:
                conn.execute(
                    "UPDATE beds SET student_id = ? WHERE id = ?",
                    (student_id, bed["id"])
                )
                conn.execute(
                    "UPDATE students SET room_id = ? WHERE id = ?",
                    (bed["room_id"], student_id)
                )
                conn.commit()
                flash(
                    f"Room {bed['room_number']} {bed['bed_label']} allocated.",
                    "success"
                )
    finally:
        conn.close()
    return redirect(url_for("index"))


# Vacate student room
@app.route("/vacate/<int:student_id>", methods=["POST"])
@role_required("warden")
@csrf_protect
def vacate(student_id):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        student = conn.execute(
            "SELECT room_id FROM students WHERE id = ?",
            (student_id,)
        ).fetchone()

        if student is None:
            conn.rollback()
            flash("Student not found.", "error")
        elif student["room_id"] is None:
            conn.rollback()
            flash("Student has no allocated room.", "error")
        else:
            conn.execute(
                "UPDATE beds SET student_id = NULL WHERE student_id = ?",
                (student_id,)
            )
            conn.execute(
                "UPDATE students SET room_id = NULL WHERE id = ?",
                (student_id,)
            )
            conn.commit()
            flash("Room and bed vacated successfully!", "success")
    finally:
        conn.close()
    return redirect(url_for("index"))


# Delete student
@app.route("/delete/<int:student_id>", methods=["POST"])
@role_required("warden")
@csrf_protect
def delete_student(student_id):
    conn = get_db()

    student = conn.execute(
        "SELECT id FROM students WHERE id = ?",
        (student_id,)
    ).fetchone()

    if student is None:
        flash("Student not found.", "error")
    else:
        conn.execute(
            "DELETE FROM students WHERE id = ?",
            (student_id,)
        )

        conn.commit()
        flash("Student deleted successfully!", "success")

    conn.close()
    return redirect(url_for("index"))


# Run Flask application
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
