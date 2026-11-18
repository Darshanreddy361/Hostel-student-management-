# NIE Girls' Hostel Portal

A Flask project demo for student registration, room allocation, attendance, and complaints.

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
