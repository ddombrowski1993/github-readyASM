# Field Planner

A Streamlit field-operations app for employees, stores, maps, schedules, call offs/PTO, follow-ups, deferred work orders, file uploads, and PDF/Excel reports.

This version runs as a Streamlit app. Account and workspace data require one external PostgreSQL database through `DATABASE_URL`; the app will not create or use SQLite storage for live data.

## Which folder to edit

Use `NEWFOLDERSSASM/github-ready` for future website changes. Add that folder
as a local repository in GitHub Desktop, then commit changes and push to the
existing `github-readyASM` repository. The parent folder is a separate checkout;
changes made there do not automatically appear in `github-ready`.

The app needs `app.py`, `src/`, `pages/`, `requirements.txt`, `data/`, and
`sample_data/`. Keep `scripts/`, `.github/`, and `database_migrations/` for
validation and maintenance. The start scripts support running locally.
`uploads/` and `reports/` hold generated files.

Local databases and cleanup backups stay out of Git. The October 26 cleanup
archive is in the parent folder at `_cleanup_backup_20261026`; its manifest
records how to restore archived files. The original local login and workspace
databases remain in the parent folder. This cleanup does not restore hosted
accounts or change the website's database connection.

## Folder Structure

```text
/app.py
/requirements.txt
/README.md
/.env.example
/start_windows.bat
/src/
/pages/
/sample_data/
/uploads/
/reports/
```

The app is intentionally kept as a small folder instead of one huge Python file. Streamlit uses `pages/` for navigation, and the shared code in `src/` keeps the app easier to maintain.

## Local Windows Setup

1. Install Python 3.11 or newer.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

Run the pre-deploy safety check before pushing:

```bash
python scripts/check_sql_safety.py
```

3. Run:

```bash
streamlit run app.py
```

Or double-click `start_windows.bat`.

Create a PostgreSQL database, copy `.env.example` to `.env`, and set:

```bash
DATABASE_URL=postgresql+psycopg2://postgres:your_password@localhost:5432/asm_command_center
```

## Streamlit Cloud Setup

Add secrets:

```toml
FIELD_PLANNER_ENV = "production"
DATABASE_URL = "postgresql+psycopg2://user:password@host:5432/database"
FIELD_PLANNER_DATABASE_INSTANCE_ID = "field-planner-production"
```

Use a hosted PostgreSQL database outside the Streamlit app container. Streamlit Community Cloud does not provide a built-in persistent PostgreSQL database.

Persistence rules:

- `DATABASE_URL` is always required.
- `DATABASE_URL` must be PostgreSQL, for example `postgresql+psycopg2://...`.
- Login accounts are stored in `public.app_users` in the hosted database.
- Each account workspace uses a stable PostgreSQL schema named from the account slug, for example `fp_jane_manager`.
- The app verifies `FIELD_PLANNER_DATABASE_INSTANCE_ID` before writing. If the connected database does not contain the expected identifier, startup stops instead of creating a new empty workspace.
- To initialize the metadata only after you have verified the correct hosted database, temporarily set `FIELD_PLANNER_ALLOW_DATABASE_METADATA_BOOTSTRAP = "true"`, start the app once, then remove that secret.
- If the database connection fails, the app shows a persistence error and does not show first-account setup.

Recommended hosted database backup process:

- Enable automated backups or point-in-time recovery with the PostgreSQL provider.
- Before code updates, export from Settings > Backup for each active workspace.
- For a full provider backup, use the database provider snapshot/export tool or `pg_dump` against the same `DATABASE_URL`.
- Restore provider backups through the database provider first; use Settings > Backup restore only for workspace-level merge recovery.

## Clean Upload Package

### Recovering a missing-account login

The login code reads accounts from `public.app_users`. Existing July login fixes
are included. An empty or missing hosted account table now stops startup before
creating account tables or metadata, instead of suggesting a new first account.
This check does not recover missing records or select a different database.

When no hosted accounts are found, a read-only diagnostic runs automatically
and writes `Account storage diagnostic` to the private Streamlit Cloud logs.
Open **Manage app** to view those logs after pushing this update.

You can also run `python scripts/diagnose_accounts.py` with the deployed app's secrets or
environment. It runs a read-only transaction, reports account counts across
accessible schemas, and checks the configured database identifier. It prints
no passwords, connection strings, email addresses, or password hashes. Keep its
host and schema details private. A local run without the deployed configuration
cannot diagnose the live database.

If accounts exist outside `public`, review the original schema before planning
a migration. If none exist, check the original database provider project,
database name, and provider backups. Do not create replacement accounts to
recover an existing workspace.

For an intentionally new installation only, temporarily set
`FIELD_PLANNER_ALLOW_FIRST_ACCOUNT_SETUP = "true"` along with any required
database metadata bootstrap setting. Remove these settings after creating the
first account. Existing installations with accounts need neither setting.

Run storage regression checks with
`python -m unittest discover -s tests -v`. These use simulated database responses;
live PostgreSQL verification still requires the deployment configuration.

For deployment, upload the cleaned project folder or the generated zip package from the parent folder. Do not upload local database files:

- `field_planner_users.db`
- `asm_command_center.db`
- `account_databases/*.db`

Generated files are also excluded from the clean package:

- Python cache folders
- Generated PDFs in `reports/`
- User uploaded files in `uploads/`
- Temporary backup files such as `*.before_*`

## What Works In This Starter

- Automatic table creation for PostgreSQL without dropping existing records.
- Setup screen when database configuration is missing.
- Login screen with separate username/email/password accounts.
- PostgreSQL-backed login accounts and separate PostgreSQL account schemas for each workspace.
- Employee/team CRUD, imports, inactive/reactivation flow.
- Store import with update-by-store-number behavior.
- Folium/OpenStreetMap map center with assignment/status coloring.
- Geographic schedule preview using latitude/longitude sorting or nearest-neighbor ordering.
- Draft/published schedule save.
- Manual schedule status/date/sequence updates.
- Weather delay workflow that marks scheduled stores as needing reschedule.
- Call off/PTO entry and reports.
- Follow-up tracker with file attachments.
- Deferred work order creation, assignment, and schedule item creation.
- PDF, Excel, and CSV exports.
- Audit logging for major actions.

## Upload Templates

Download templates from the Employees and Stores pages, or use:

- `sample_data/store_template.csv`
- `sample_data/employee_template.csv`

## Notes

Uploaded files are saved to `uploads/` and database records are written to `uploaded_files`. This is intentionally simple for local use and can later be swapped for cloud object storage.

For sensitive company or employee data, create individual accounts and deploy behind appropriate access controls.
