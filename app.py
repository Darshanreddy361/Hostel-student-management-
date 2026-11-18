
from datetime import date
from functools import wraps
from hmac import compare_digest
import os
import secrets
import sqlite3

import click
from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true",
)
if not os.environ.get("SECRET_KEY"):
    app.logger.warning("SECRET_KEY is not set; active sessions will reset on restart.")

# Database configuration for local and Docker execution
DATABASE = os.environ.get("DATABASE_PATH", "hostel.db")
CAMPUSES = ("South Campus", "North Campus")
ROOM_CAPACITY = 3
ROOM_NUMBERS = range(101, 351)
MIN_PASSWORD_LENGTH = 8
ATTENDANCE_STATUSES = ("Present", "Absent", "On Leave")


# Database connection
def get_db():
    db_folder = os.path.dirname(DATABASE)

    if db_folder:
        os.makedirs(db_folder, exist_ok=True)

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_number TEXT UNIQUE NOT NULL,
            capacity INTEGER NOT NULL,
            campus TEXT NOT NULL
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

    room_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(rooms)")
    }
    if "campus" not in room_columns:
        conn.execute("""
            ALTER TABLE rooms
            ADD COLUMN campus TEXT NOT NULL DEFAULT 'South Campus'
        """)

    student_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(students)")
    }
    if "campus" not in student_columns:
        conn.execute("""
            ALTER TABLE students
            ADD COLUMN campus TEXT NOT NULL DEFAULT 'South Campus'
        """)

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
        password = request.form.get("password", "")
        password_confirmation = request.form.get("password_confirmation", "")

        if not all([name, usn, department, year]) or campus not in CAMPUSES:
            flash("Please fill all student details.", "error")
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
                INSERT INTO students (name, usn, department, year, campus)
                VALUES (?, ?, ?, ?, ?)
            """, (name, usn, department, year, campus))
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
            SELECT rooms.*,
                   COUNT(students.id) AS occupied
            FROM rooms
            LEFT JOIN students ON rooms.id = students.room_id
            GROUP BY rooms.id
            ORDER BY rooms.campus, rooms.room_number
        """).fetchall()
    finally:
        conn.close()


@app.route("/warden")
@role_required("warden")
def warden_dashboard():
    attendance_today = date.today().isoformat()
    conn = get_db()

    students = conn.execute("""
        SELECT students.*, rooms.room_number,
               attendance_records.status AS attendance_status
        FROM students
        LEFT JOIN rooms ON students.room_id = rooms.id
        LEFT JOIN attendance_records
            ON attendance_records.student_id = students.id
            AND attendance_records.attendance_date = ?
        ORDER BY students.id DESC
    """, (attendance_today,)).fetchall()

    rooms = conn.execute("""
        SELECT rooms.*,
               COUNT(students.id) AS occupied
        FROM rooms
        LEFT JOIN students ON rooms.id = students.room_id
        GROUP BY rooms.id
        ORDER BY rooms.campus, rooms.room_number
    """).fetchall()

    total_students = conn.execute(
        "SELECT COUNT(*) FROM students"
    ).fetchone()[0]

    allocated = conn.execute(
        "SELECT COUNT(*) FROM students WHERE room_id IS NOT NULL"
    ).fetchone()[0]

    available = sum(
        room["capacity"] - room["occupied"]
        for room in rooms
    )
    pending_complaints = conn.execute(
        "SELECT COUNT(*) FROM complaints WHERE status = 'Pending'"
    ).fetchone()[0]
    complaint_count = conn.execute(
        "SELECT COUNT(*) FROM complaints"
    ).fetchone()[0]
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

    if room_campus_filter not in CAMPUSES:
        room_campus_filter = ""
    if room_status_filter not in ("available", "full"):
        room_status_filter = ""

    filtered_rooms = []
    for room in rooms:
        has_space = room["occupied"] < room["capacity"]
        searchable_text = f"{room['room_number']} {room['campus']}".casefold()

        if room_query.casefold() not in searchable_text:
            continue
        if room_campus_filter and room["campus"] != room_campus_filter:
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
    displayed_rooms = filtered_rooms[
        first_room_index:first_room_index + room_page_size
    ]

    conn.close()

    return render_template(
        "index.html",
        students=students,
        rooms=displayed_rooms,
        total_rooms=len(rooms),
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
        total_students=total_students,
        allocated=allocated,
        available=available,
        campuses=CAMPUSES,
        warden_username=session.get("username")
    )


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
        SELECT students.*, rooms.room_number
        FROM students
        LEFT JOIN rooms ON rooms.id = students.room_id
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
    conn.close()

    return render_template(
        "student_dashboard.html",
        student=student,
        complaints=complaints
    )


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


# Automatically allocate available room
@app.route("/allocate/<int:student_id>", methods=["POST"])
@role_required("warden")
@csrf_protect
def allocate(student_id):
    conn = get_db()

    student = conn.execute(
        "SELECT * FROM students WHERE id = ?",
        (student_id,)
    ).fetchone()

    if student is None:
        flash("Student not found.", "error")

    elif student["room_id"] is not None:
        flash("Student already has a room.", "error")

    else:
        room = conn.execute("""
            SELECT rooms.id, rooms.room_number
            FROM rooms
            LEFT JOIN students ON rooms.id = students.room_id
            WHERE rooms.campus = ?
            GROUP BY rooms.id
            HAVING COUNT(students.id) < rooms.capacity
            ORDER BY rooms.room_number
            LIMIT 1
        """, (student["campus"],)).fetchone()

        if room:
            conn.execute("""
                UPDATE students
                SET room_id = ?
                WHERE id = ?
            """, (room["id"], student_id))

            conn.commit()

            flash(
                f"Room {room['room_number']} allocated successfully!",
                "success"
            )

        else:
            flash("Sorry! No rooms are available.", "error")

    conn.close()
    return redirect(url_for("index"))


# Vacate student room
@app.route("/vacate/<int:student_id>", methods=["POST"])
@role_required("warden")
@csrf_protect
def vacate(student_id):
    conn = get_db()

    student = conn.execute(
        "SELECT room_id FROM students WHERE id = ?",
        (student_id,)
    ).fetchone()

    if student is None:
        flash("Student not found.", "error")

    elif student["room_id"] is None:
        flash("Student has no allocated room.", "error")

    else:
        conn.execute("""
            UPDATE students
            SET room_id = NULL
            WHERE id = ?
        """, (student_id,))

        conn.commit()
        flash("Room vacated successfully!", "success")

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
