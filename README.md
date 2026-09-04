# Hospital Equipment Fault Reporting and Help Desk Management System

A role-based Flask application for reporting hospital equipment faults, assigning work to technicians, managing assets, and reviewing workload reports.

## Features

- Flask application factory, Blueprints, SQLAlchemy, Flask-Login, Flask-Migrate, and WTForms.
- Administrator, Technician, and Staff roles.
- Equipment, department, user, ticket, comment, and CSV-report workflows.
- Responsive Bootstrap 5 interface with role-aware navigation.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`
3. Set a unique `SECRET_KEY` environment variable for deployment.
4. Initialize the database: `flask --app run.py db init`, then `flask --app run.py db migrate -m "Initial schema"` and `flask --app run.py db upgrade`.
5. Create the first administrator: `flask --app run.py seed-admin` (you will be prompted for a password).
6. Run locally: `flask --app run.py run --debug`

For a fresh SQLite development database, the application creates tables on start. Run `seed-admin` to establish the Administration department and initial administrator, then add departments before staff self-register. In production, disable `db.create_all()` after migrations are established and deploy with a production WSGI server.
