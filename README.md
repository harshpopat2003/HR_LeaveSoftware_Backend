# HR Leave backend

FastAPI and PostgreSQL API for Sabin Plastic, Sabin Signs and Trading: employees, leave and balances,
passports, Emirates IDs and visit visas, company air tickets, the dashboard, reports, logins, roles and rules.

- 18 tables, 72 endpoints under `/api/v1`, interactive docs at `/api/v1/docs`.
- 159 tests run against a real PostgreSQL database. Coverage is 99%. ruff and mypy are clean.

## Folder layout

```
backend/
  app/
    main.py            app setup: CORS, security headers, error format, routers
    config.py          settings from .env
    db.py, models.py   database connection and the 18 tables
    deps.py            signed-in user, permission checks, company filter, paging
    security.py        Argon2 passwords, JWT access token, refresh token
    clock.py           "today" in Asia/Dubai
    rules/leave_rules.py   leave, ticket and document rules (pure functions, mirrors the frontend)
    services/          business logic (one file per area)
    api/               HTTP routes (one file per area)
    seed.py            starting data (and demo data with --demo)
    seed_data/demo.json    the demo employees, the same as the frontend mock data
  alembic/             database migrations
  tests/               pytest tests
```

## Setting it up on WSL (Ubuntu)

Run these in the WSL terminal, inside the `backend` folder.

1. Install Python and PostgreSQL:

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip postgresql postgresql-contrib
   sudo service postgresql start
   ```

2. Create the database user and the two databases (`hr_leave` for the app, `hr_test` for the tests).
   Choose your own password instead of `change_me`:

   ```bash
   sudo -u postgres psql -c "CREATE USER hr_app WITH PASSWORD 'change_me' CREATEDB;"
   sudo -u postgres psql -c "CREATE DATABASE hr_leave OWNER hr_app;"
   sudo -u postgres psql -c "CREATE DATABASE hr_test OWNER hr_app;"
   ```

3. Create the Python environment with conda (Miniconda or Anaconda inside WSL) and install the packages:

   ```bash
   conda env create -f environment.yml
   conda activate hr-backend
   ```

   If `requirements.txt` changes later, run `conda env update -f environment.yml --prune`.
   (Without conda: `python3 -m venv .venv`, `source .venv/bin/activate`, `pip install -r requirements-dev.txt`.)

4. Create `.env` from the example and fill it in. Use the password from step 2, and make a JWT secret with
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.

   ```bash
   cp .env.example .env
   code .env
   ```

5. Build the tables and load the starting data:

   ```bash
   alembic upgrade head
   python -m app.seed --demo     # demo logins admin, manager, hr1, hr2 with password demo123
   ```

   For real use, leave out `--demo`. Then only the companies, roles, permissions, leave types, rules and one
   `admin` login are created. The admin password is printed once, or you can choose it with
   `python -m app.seed --admin-password "your password"`.

6. Start the API:

   ```bash
   uvicorn app.main:app --reload
   ```

   Open http://localhost:8000/api/v1/docs to try every endpoint. Press **Authorize** after signing in with
   `POST /auth/login` and paste the `accessToken`.

Every time you open a new WSL terminal: `sudo service postgresql start`, then `conda activate hr-backend`.

## Running the tests

```bash
pytest                          # all tests (uses hr_test, never hr_leave)
pytest --cov=app                # with coverage
ruff check . && mypy app tests  # lint and type checks
```

The tests rebuild `hr_test` from the migrations at the start (up, down and up again), load the demo data,
and run each test inside a transaction that is rolled back. They refuse to run on a database whose name
does not contain `test`. Dates are fixed to 27 Nov 2026 so the expected numbers do not change.

## How it works

**Sign-in.** `POST /auth/login` returns a 15-minute access token (send it as `Authorization: Bearer ...`) and
sets a 7-day refresh cookie (`hr_refresh`, httpOnly). `POST /auth/refresh` swaps the cookie for a new one.
If an old refresh token is used again, every session of that user is ended. After 5 wrong passwords the
account is locked for 15 minutes. The administrator can unlock it.

**Permissions.** Every endpoint checks one of the 12 permissions. The System Administrator role always has all
of them. The Manager and HR roles can be changed on the Roles screen, and new roles can be added.

**Companies.** A login can be limited to some companies (`PUT /users/{id}/companies`). An empty list means all
three. Every list takes `company_id` (one company) and also stays inside the companies the user may see.
Records of other companies answer 404.

**Leave.** New leave is `pending` until a manager approves or rejects it. A rejection needs a note. Only pending
leave can be edited. Pending or approved leave can be cancelled. Balances are calculated from the leave
records and adjustments every time, so they are never out of date. The database itself refuses two pending
or approved leaves of one employee on overlapping dates.

**Rules.** One rule set for the group, and optionally a different one per company. Every save is a new version,
and `GET /rules/history` lists them.

**Audit.** Every change writes a row to `audit_log` with who, when, from which address, and the values before
and after. The database blocks changing or deleting audit rows.

**Errors.** Every error has the same shape:
`{"error": {"code": "conflict", "message": "...", "details": ...}}`.

**JSON.** Field names are camelCase, the same as the frontend (`empNo`, `joiningDate`). Query parameters are
snake_case (`company_id`, `page`, `size`). Dates are `YYYY-MM-DD`. "Today" is the date in Asia/Dubai.

**Excel.** The report endpoints return rows. The Excel files are still built in the browser, so the layout
stays in one place.

## Hosting with Cloudflare

The plan:

- The frontend goes on **Cloudflare Pages** (`npm run build`, publish `frontend/dist`).
- The API and PostgreSQL run on a small server (any Linux VM) behind a **Cloudflare Tunnel**, so the server
  needs no open ports.
- Serve the API on the same site as the frontend, for example `hr.yourcompany.com` for the frontend and
  `hr.yourcompany.com/api` (or `api.yourcompany.com`) for the API. The refresh cookie is `SameSite=Strict`,
  so the two must share the same main domain.

On the server, in `.env`:

```
COOKIE_SECURE=true
CORS_ORIGINS=https://hr.yourcompany.com
JWT_SECRET=<long random value>
DATABASE_URL=postgresql+psycopg://hr_app:<password>@localhost:5432/hr_leave
```

The API refuses to start when `COOKIE_SECURE=true` and the JWT secret is the development one. Run it with
`uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2` (as a systemd service) and point the tunnel at
`http://127.0.0.1:8000`. Behind Cloudflare, the visitor's address is read from `CF-Connecting-IP` for the
audit log. `GET /api/v1/health` checks the process. `GET /api/v1/health/ready` also checks the database.

Back up the database every night, for example with `pg_dump hr_leave | gzip > hr_leave_$(date +%F).sql.gz`
from cron, and keep copies off the server.

## Changing the database

1. Change `app/models.py`.
2. `alembic revision --autogenerate -m "what changed"`, then read the new file in `alembic/versions`.
3. `alembic upgrade head`, then `pytest`.
