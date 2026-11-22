# Bulk ordering — API and browser workers

The server side of the bulk-ordering tool: it signs buyer accounts in to
Flipkart, applies coupons, places orders under a price cap, reads every
order's status back, keeps a ledger of every unit bought, raises alerts, and
stores and reads GST invoices.

The web UI is a separate repository, **elex-bulk-ordering-web**. The two are
checked out side by side:

```
some-folder/
  elex-bulk-ordering-bakend/   this repository
  elex-bulk-ordering-web/      the web UI
```

Only Flipkart is supported. Card details are never stored, except the card
number of a card order (the owner's decision; see `docs/DEPLOY.md`, "A note on
card data").

## What is here

| Path | What it is |
|------|------------|
| `app/api/` | The HTTP API (FastAPI): accounts, runs, orders, alerts, invoices, settings, users |
| `app/automation/` | The Flipkart flows in a real browser (Playwright): sign-in, OTP, coupons, ordering, read-back |
| `app/core/` | The rules: status reading, alerts, unit limits, invoices, security |
| `app/jobs/` | Runs: one job fans out over many accounts, with live progress |
| `app/io/` | Spreadsheets, CSV exports, invoice PDFs |
| `app/db/` | Models and database set-up; `alembic/` holds the migrations |
| `tests/` | The test suite (about 3,100 tests) |
| `docker-compose.yml` | The whole stack: database, queue, API, workers, web UI |
| `docs/` | The deploy guide, the plan, the spec and the working record |

## Run it on your own machine

Python 3.12 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium     # once: the browser the flows drive

cp .env.example .env                      # then set ORDERING_DEV_MODE=true in it
uvicorn app.main:app --reload             # the API on http://127.0.0.1:8000
```

To keep the data in a hosted PostgreSQL such as Neon instead, see
`docs/DEPLOY.md` §6: one command copies this machine's database across.

With `ORDERING_DEV_MODE=true` the sign-in is `admin` / `admin`; anywhere else
set `ORDERING_ADMIN_PASSWORD_HASH` and `ORDERING_JWT_SECRET` (the commands to
make them are beside each in `.env.example`). The database is a SQLite file,
`ordering.db`, created at the first start and copied into `backups/` before
every later one.

Then start the web UI from its own repository (`npm install`, `npm run dev`)
and open http://localhost:5173.

`--reload` restarts the server whenever a `.py` file is saved, which cuts
short any run in flight: do not edit code while a real order runs.

## Tests

```bash
pytest
```

The suite makes its own throwaway database and needs no `.env`. A handful of
tests compare this repository with the web one (nginx's config against the
API's routes, the ledger's columns against the page's); they find it beside
this folder, or at `ORDERING_WEB_DIR`, and are skipped — saying so — when it
is not there.

## Run the whole stack (Docker)

```bash
cp .env.example .env     # set ORDERING_DB_PASSWORD, ORDERING_JWT_SECRET, ORDERING_ADMIN_PASSWORD_HASH
docker compose up --build
```

The UI is then on http://localhost:8080. `docs/DEPLOY.md` covers a real
server: TLS, backups, encryption at rest, the settings that matter, and
upgrades.

## From the command line

```bash
python -m app.cli login                       # sign accounts in (the OTP is read from the inbox when one is set)
python -m app.cli coupon --code SAVE10        # apply a coupon to every signed-in account
python -m app.cli order --product <link> --email you@example.com --target 1000 --payment-mode cod
python -m app.cli session-check               # are the stored sign-ins still good?
python -m app.cli otp-check --email <address> # what does the OTP reader see?
python -m app.cli set-password <email>        # the way back in when no admin can sign in
python -m app.cli test-mail <address>         # prove the mail settings
python -m app.cli copy-to-postgres --check    # move the data to Neon: check first, then without --check
```

An order needs `--email` (one account) or `--all`, and `--target` (the cap per
order, in rupees) or `--no-cap`. A card order asks for the card at a hidden
prompt.

## When a run misbehaves

Flipkart's page markup changes without notice. Every selector the flows use is
in `app/core/selectors.py`; check there before the logic. A run that fails
saves a screenshot in `diagnostics/` (card and password boxes painted over).

## More

- `docs/DEPLOY.md` — deployment, settings, backups, upgrades
- `docs/PLAN.md`, `docs/ROADMAP.md` — why it is built the way it is
- `docs/specs/order-unit-ledger/` — the spec, the order-report format and its template, and the record of what was built and when
