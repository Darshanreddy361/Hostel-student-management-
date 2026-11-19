# NIE Girls' Hostel Portal

A Flask hostel operations portal demo for students and wardens, covering bed
allocation, attendance, fee verification, student services, facilities,
staffing, notices, operational finance, and reports.

## Public demo deployment

This project can be deployed from the public GitHub repository using Render's Blueprint flow:

1. Open [Deploy to Render](https://render.com/deploy?repo=https://github.com/Darshanreddy361/Hostel-student-management-).
2. Sign in to Render and authorize access to the repository.
3. Review the `nie-hostel-portal-demo` service and choose **Apply**.
4. Wait for the first deployment, then open the URL Render provides.

The service starts with a new, empty database and generated room inventory. The local `hostel.db` is not included in the repository or deployment. The free demo uses temporary local storage, so demo accounts and records may be lost when the service restarts or is redeployed. No warden account is provisioned by default.

**Demo only:** This is a student project demonstration, not an official NIE service. Use fictional student details and a unique throwaway password only. Do not enter real student information, real credentials, or operational hostel data. Do not use this deployment for hostel operations.

For an NIE-authorized production deployment, use institution-approved hosting, persistent storage, and the institution's required privacy and security controls.

## Run locally

```sh
python -m pip install -r requirements.txt
python app.py
```

The development server is available at `http://localhost:5000`.

In another terminal, create a warden account when running locally:

```sh
python -m flask --app app create-warden
```

The command prompts for a username and a password (minimum 12 characters).

## Room, floor, and bed allocation

The warden dashboard now tracks inventory as campus → block → floor → room →
bed. Existing rooms are retained and safely linked to a default **Block A**;
their numeric room prefix is used to infer a floor. A bed slot is created for
each existing room capacity, and current student room assignments are backfilled
to individual beds. The migration does not replace existing room or student
records. Startup stops with an explicit error if legacy assignments exceed a
room's configured capacity, rather than silently dropping an assignment.
If an older database has existing room or student records but no campus
column, startup also stops rather than guessing that every record belongs to
South Campus. Back up the database, assign the correct campus to every
existing row, and add/populate the missing column before restarting; this
keeps campus-scoped operations from misclassifying legacy records.

Wardens can browse the bed map by campus, block, floor, and availability; add a
room with its block, floor, and capacity; mark an unoccupied bed available,
blocked, or under maintenance; and choose a specific bed when allocating a
student. The existing automatic allocation action now selects the next
available bed. Vacating a student releases that bed.

## Student services and warden operations

The student dashboard provides a personal overview, warden-published notices,
daily campus mess menus with reservations, visitor appointments, outpass requests,
maintenance tickets, room complaints, fee receipt history, and optional
guardian/emergency contact fields. Students can only view or change their own
profile and submissions. Visitors and outpasses require an in-app warden
decision; the demo does not send SMS, OTPs, or external notifications.

The **Operations hub** on the warden dashboard covers:

- Publishing and expiring student notices.
- Publishing a daily menu for either campus with separate breakfast, lunch,
  snacks, and dinner entries. Students see today's four meal slots for their
  own campus, with future menus and reservation controls below.
- Reviewing visitor/outpass requests with recorded decisions; logging outpass
  exit and return events at the gate.
- Maintaining campus facility status and assigning staff to maintenance
  tickets with priority, status, and a required resolution note.
- Keeping a staff directory and dated day/evening/night duty roster.
- Recording vendor, income, and expense entries, including invoice and due/paid
  status. These records do not trigger or reconcile real payments.
- Downloading warden-only CSV reports for student assignments, attendance,
  fee receipts, gate requests, maintenance, and finance. CSV cells are escaped
  against spreadsheet formula execution.

Student contact details and operational records contain personal or sensitive
information. This is still a project demonstration, not an approved hostel
system. Use fictional data; production use needs institution-approved hosting,
privacy review, persistent backups, and a security assessment.

The meal-menu migration adds the Snacks meal type by rebuilding the menu table
inside a transaction, preserving existing menu IDs, entries, and reservation
references.

### Feature benchmark and integration boundaries

The demo's student self-service, room inventory, meal reservations, maintenance
queue, visitor/leave approvals, staff workflows, and operational exports reflect
common workflows presented by established housing and boarding platforms such
as [StarRez](https://www.starrez.com/),
[eRezLife](https://erezlife.com/), [Orah](https://www.orah.com/), and
[SpaceBasic](https://www.spacebasic.com/). This project implements those
workflows locally; it does not claim their scale, certification, or integrations.

Enterprise single sign-on, SIS/ERP synchronization, payment processing, SMS or
OTP delivery, RFID/QR access, geolocation, biometric identification, and
predictive or AI services require institutional approval and external providers.
They are intentionally not simulated as live integrations.

## Hostel fee receipt workflow

Students sign in, open **Hostel Fee Payment**, enter the amount, payment date,
transaction ID/UTR, and academic year, then upload a JPG, JPEG, PNG, or PDF
receipt (maximum 5 MB). The student name, USN, and campus come from the signed-in
account. A local preview is shown before upload. Each new receipt starts as
**Pending**; students can view only their own receipt files and can upload a
corrected receipt after a rejection. The rejected submission and its reason
remain in the payment history.

Wardens open **Fee Verification** on the dashboard to search and filter the
receipt list, securely view or download a receipt, then approve or reject it.
Rejection requires a reason. The decision, warden account, and verification
timestamp are saved with the receipt. Uploaded files are stored outside
`static/` and are only served by an authenticated route.

### Local storage and Docker

The upload directory is controlled by `RECEIPT_UPLOAD_DIR`. By default, uploads
go to a `receipts/` directory next to `DATABASE_PATH`. For persistent Docker
storage, mount a volume at `/app/data` (the Docker image defaults to
`/app/data/hostel.db` and `/app/data/receipts`):

```sh
docker build -t nie-hostel-portal .
docker run -d --name nie-hostel-portal \
  -p 5000:5000 \
  -v hostel-data:/app/data \
  -e SECRET_KEY=replace-with-a-long-random-secret \
  nie-hostel-portal
```

Create a warden account interactively inside the running container:

```sh
docker exec -it nie-hostel-portal python -m flask --app app create-warden
```

Without a volume, container-local uploads are not durable across container
replacement.

### Render and production storage

The included free Render demo configuration uses `/tmp/hostel.db` and
`/tmp/hostel-receipts`; both are temporary and can be lost when the service
restarts or is redeployed. Do not use the free demo for real payment records.
For a production Render deployment, provision an approved persistent disk and
set both `DATABASE_PATH` and `RECEIPT_UPLOAD_DIR` to locations on that disk
(for example `/var/data/hostel.db` and `/var/data/hostel-receipts`). The project
does not automatically provision or attach a paid disk. Alternatively, a
production object-storage integration must use private buckets, restricted
credentials, and authenticated application-mediated downloads; do not make
receipt objects public. No cloud provider or credentials are assumed here.

Keep the database and upload storage on durable storage together, back them up,
and set `SECRET_KEY` to a persistent secret. The app rejects a receipt storage
path inside Flask's public `static/` directory.
