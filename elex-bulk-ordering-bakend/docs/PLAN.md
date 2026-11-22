# Flipkart Ordering Suite — Consolidation Plan

> **Audience:** Akanksha (project owner) and whoever builds this — a plan to review and approve before any code is written.
> **Goal:** Merge three existing repos (`login-otp-generator`, `add-coupons`, `flipkart-ordering-tool`) into one **web-based, scalable, fast** tool, built entirely inside the `ordering/` repo.
> **Status:** DRAFT — awaiting approval of architecture and scope.

---

## 1. What exists today

Three separate Python repos that are really **one pipeline** split across folders. All three share Selenium + Chrome, `openpyxl`, a single Excel workbook as their "database", and a cookie/session store (`*.pkl` files per account).

```
  Step 1: LOGIN            Step 2: COUPONS          Step 3: ORDER
  (make sessions)   ─────▶ (apply coupon)   ─────▶  (place order)
        │                       │                        │
        └──────── cookes/{email}.pkl  +  inputs.xlsx ────┘
                         (shared state)
```

| Repo | What it does | Core files |
|------|--------------|-----------|
| **login-otp-generator** | Reads emails (Excel col C), opens Flipkart login, enters email, triggers OTP, saves cookies to `{email}.pkl`, marks status in col E. Separate script opens `m.kuku.lu` (temp-mail) to read OTPs. | `FlipkartWindow`, `otp-generator`, `ExcelHandler` |
| **add-coupons** | Loads a saved session, opens Flipkart rewards page in **iPhone SE mobile emulation**, types a coupon, claims it, writes result to col E. | `Add Coupon`, `ExcelHandler` |
| **flipkart-ordering-tool** | tkinter GUI (choose Excel, enter GST/Name/Address/Pin, add product links + qty). Selenium flow: buy-now → address → qty → GST → card payment → scrape order id/model/color/amount. | `GUIScreen`, `Orderingfunctions`, `main`, `ExcelHandler` |

### Problems the merge fixes
- **3 duplicate `ExcelHandler`s** with **inconsistent, overlapping column maps** (see §4).
- **Column E is overloaded** — login writes `"Done"`, coupons writes the coupon value. Same cell, two meanings.
- **A real bug:** `Orderingfunctions` does `from selenium.webdriver.common import getSession` — that function does not exist in Selenium; it's defined in `add-coupons`. The ordering flow's session loading is broken as written.
- Config scattered across three `data` JSON files with different keys.
- No concurrency, no web access, no central logging, no visibility into progress.

### Current Excel schema (to be formalized)
| Col | Meaning | Col | Meaning |
|-----|---------|-----|---------|
| C | Email | J | Amount |
| D | Phone number | K | Order ID |
| E | Status / coupon (overloaded) | O | GST name |
| F | Model | T | Card number |
| G | Qty | U | Card month |
| H | Color | V | Card year |
| I | Pin | W | CVV |

---

## 2. Target: what we're building

A single **web application** that does Login → Coupons → Ordering for many accounts, with:

- A browser UI (dashboard + per-step screens + live progress) — no desktop install.
- A **per-order price cap ("target")** — see §6a. Each order is only placed if its final amount (after coupon) is at or below the target; otherwise that single order fails and the rest continue.
- **Parallel execution** across accounts with a configurable concurrency limit (the "speed" requirement).
- A **job queue + worker pool** so long-running browser work doesn't block the UI and can scale out (the "scalable" requirement).
- A real database replacing the single Excel file, **with Excel import/export kept** for backward compatibility.
- Central session store, central config, central logs, live status per account.

---

## 3. Recommended tech stack (my decision, with rationale)

You said: web, highly scalable, highest speed, stack is my call. Here's what I recommend and why.

| Layer | Choice | Why |
|-------|--------|-----|
| **Browser automation** | **Playwright (async)** — *replacing Selenium* | Native async → run many accounts concurrently in one process; faster, auto-waiting (fewer flaky `time.sleep`), headless by default, built-in browser contexts for clean per-account isolation. **The valuable Flipkart XPaths/selectors from the current Selenium code port over almost 1:1** — that domain knowledge is preserved, only the driver layer changes. |
| **Backend API** | **FastAPI** (Python, async) | Keeps the project in Python (reuse all existing logic), async-first to match Playwright, auto OpenAPI docs, WebSocket support for live progress. |
| **Job queue / workers** | **Arq + Redis** (async) *or* Celery + Redis | Browser jobs run in background workers, not the web process. Scale by adding workers. Bounded concurrency per worker to avoid Flipkart rate-limiting. |
| **Database** | **SQLite** to start → **PostgreSQL** for scale (via SQLAlchemy, so the switch is config-only) | Replaces the fragile single-Excel-file state; supports concurrent reads/writes, proper status tracking, and querying progress. |
| **Session/cookie store** | DB table or Redis (migrated from `*.pkl`) | Shared across workers; no file-locking issues. |
| **Frontend** | **React + Vite + a component lib (e.g. shadcn/ui or MUI)** | A real, modern, "complete UI": dashboard, tables, forms, live progress. Talks to FastAPI over REST + WebSocket. |
| **Live progress** | **WebSocket / SSE** | Stream each account's status to the browser in real time. |
| **Packaging** | **Docker Compose** (api + worker + redis + db + web) | One command locally; same images deploy to cloud. Scale workers with `--scale`. |
| **Deployment (decided)** | **Containers on the cloud:** API + web on **AWS ECS Fargate**, a **separately-scalable pool of Playwright worker containers** (browser work is CPU/RAM-heavy, so its own auto-scaling service), **RDS PostgreSQL** + **ElastiCache Redis**, behind HTTPS/ALB. | Matches "scalable + fast": the worker pool scales independently of the web tier, managed DB/Redis remove ops burden. **Quick-start alternative:** Fly.io / Render / Railway + managed Postgres+Redis if you want a cheaper, faster first deploy; same Docker images, so moving to AWS later is not a rewrite. |
| **App authentication** | **Login on the app itself** (JWT sessions; start single-admin, extensible to multi-user roles) | Since it's on a public web server, the tool must not be open to the internet. |

> If you'd rather stay closer to the current code and avoid a React frontend initially, a lighter alternative is **FastAPI + server-rendered templates + HTMX**. It's faster to build but less "app-like". My default recommendation is the React stack above; tell me if you want the lighter path.

> **Honest caveat on "highest speed":** Flipkart actively rate-limits and bot-detects. True throughput is capped by what the site tolerates per account/IP, not by our concurrency. The design will let you *tune* concurrency and add proxies, but I won't promise unlimited parallelism — pushing too hard gets accounts/sessions blocked. The architecture maximizes safe throughput; it can't remove the site's limits.

---

## 4. Proposed repo structure (inside `ordering/`)

```
ordering/
├─ docker-compose.yml
├─ README.md
├─ docs/
│  └─ PLAN.md                      # this file
├─ backend/
│  ├─ pyproject.toml / requirements.txt
│  ├─ app/
│  │  ├─ main.py                   # FastAPI entry
│  │  ├─ config.py                 # unified settings (merges the 3 old data.json)
│  │  ├─ db/                       # SQLAlchemy models + migrations
│  │  │  ├─ models.py              # Account, Session, Order, CouponRun, Job
│  │  │  └─ session.py
│  │  ├─ core/
│  │  │  ├─ browser.py             # Playwright context factory (desktop + mobile emulation)
│  │  │  ├─ session_store.py       # save/load cookies (replaces getSession + pickle)
│  │  │  ├─ selectors.py           # ALL Flipkart XPaths/selectors in one place
│  │  │  └─ logging.py             # central structured logging
│  │  ├─ automation/               # the 3 flows, ported to Playwright async
│  │  │  ├─ login.py               # from login-otp-generator/FlipkartWindow
│  │  │  ├─ otp.py                 # from otp-generator (kuku.lu reader)
│  │  │  ├─ coupons.py             # from add-coupons/Add Coupon
│  │  │  └─ ordering.py            # from flipkart-ordering-tool/Orderingfunctions
│  │  ├─ jobs/                     # Arq worker tasks + concurrency control
│  │  │  └─ tasks.py
│  │  ├─ api/                      # REST routes
│  │  │  ├─ accounts.py            # upload/import Excel, CRUD
│  │  │  ├─ jobs.py                # start login/coupon/order runs
│  │  │  ├─ settings.py            # concurrency / proxy / OTP inbox (business details live in the Address Book; cards are per-run uploads)
│  │  │  └─ ws.py                  # live progress WebSocket
│  │  └─ io/
│  │     └─ excel.py               # unified Excel import/export (replaces 3 ExcelHandlers)
│  └─ tests/                       # pytest (carry over existing test intent)
└─ frontend/
   ├─ package.json
   └─ src/
      ├─ pages/  Dashboard · Accounts · Login · Coupons · Ordering · Settings
      ├─ components/  AccountTable · ProgressStream · ConfigForms
      └─ api/    REST + WebSocket client
```

### How each old file maps in
- `login-otp-generator/FlipkartWindow` → `automation/login.py`
- `login-otp-generator/otp-generator` → `automation/otp.py`
- `add-coupons/Add Coupon` → `automation/coupons.py`
- `flipkart-ordering-tool/Orderingfunctions` + `main` → `automation/ordering.py` + `jobs/tasks.py`
- `flipkart-ordering-tool/GUIScreen` (tkinter) → **retired**, replaced by the React Ordering/Settings pages
- all three `ExcelHandler`s → one `io/excel.py`
- all three `data` JSON files → one `config.py` + Settings table
- `cookes/*.pkl` → `session_store` (DB/Redis)

---

## 5. Data model (replaces the overloaded Excel columns)

```
Account   id, email, phone, status, created_at
Session   account_id, cookies(json), created_at, valid
CouponRun account_id, coupon_code, result, run_at
Order     account_id, product_link, qty, model, color, pin,
          amount, target, order_id, gst_name, status, placed_at
          # status includes "failed — over target" (see §6a)
Job       id, type(login|coupon|order), status, progress, log, started_at
```
**No Card table.** Card details are uploaded per run (corporate-card Excel), kept in memory only for that run, and discarded when it ends. Corporate cards need no payment OTP. In transit they travel over HTTPS and are never written to logs. **Amended 25 Oct 2026 at the owner's decision:** the *number* of the card an order was paid with is kept on that order's row (`Order.card_number`) so the ledger can be matched against card statements; the expiry and the CVV still have no column anywhere, and the number never enters one of the app's own log lines or an alert. Since 26 Oct 2026 the ledger's Card Number search, which travels in the URL, is cut from the web servers' access lines too (uvicorn's and nginx's log the path without its query string), and an invoice compilation's job records only its last four digits (§10, "Card data defences"). One place is left: nginx's error log, which writes the whole request line when the API is down or times out. **Amended 19 Nov 2026 at the owner's decision (CorpAssist):** an HDFC virtual child card read from its email keeps its number, its parent card's last four digits and its opening amount (`hdfc_child_cards`), so its balance can be tracked and a run can pass over a card with too little left. Its expiry and CVV still have no column: they come from the run's card sheet as before. What a child card has spent is read from `Order.card_number`, never stored beside it.

Excel import maps old columns → these tables; Excel export regenerates the familiar sheet so your current workflow still works.

---

## 6. UI (the "complete UI")

- **Dashboard** — counts: accounts, sessions valid, coupons applied, orders placed; live job progress.
- **Accounts** — import Excel / add manually; table with per-account stage (session? coupon? order?).
- **Login** — pick accounts, start session-generation run, watch login progress live; OTP auto-read from the configured central inbox (manual-entry box only as fallback).
- **Coupons** — enter coupon code, pick accounts, run, see per-account success/"already used"/error.
- **Ordering** — product links + qty + **target (price cap)**, **upload the corporate-card Excel for this run**, pick accounts, run; results table (order id, amount, status incl. "failed — over target"); export.
- **Settings** — ~~GST / Name / Address / Pin (from old `input_data.txt`)~~ *(moved to the Address Book on 24 Oct 2026; there is no config fallback)*; **OTP inbox config** (mail server + domain, changeable per run); concurrency limit; timeouts (from old `data.json`); proxy config; app users.

---

## 6a. Price-target cap (confirmed requirement)

A safety rule applied during the **Ordering** flow:

- **Per order, independent.** Every order in a run is checked on its own.
- **Strict upper cap.** Just before confirming payment, the tool reads the **final payable amount after the coupon is applied** and compares it to the **target**. The order is placed **only if `final_amount ≤ target`** — no overshoot allowed, not even ₹0.01 (₹999.99 ✅, ₹1000.00 ✅, ₹1000.01 ❌).
- **On breach, fail that order only.** If `final_amount > target`, the tool does **not** place the order, marks it `failed — over target` with the amount it saw, and continues to the next account. One over-target order never stops the batch.
- **Target value:** one value set per run (applies to all orders in the batch). *Assumption pending confirmation; per-product/per-account targets can be added later.*
- **A cap, or "no cap" said on purpose.** A run gives a target, or ticks "No price cap — order at any price" (`no_cap=true` on `POST /jobs/order`, `--no-cap` on the command line). A blank target is refused rather than read as "no cap", and so is a target with "no cap" ticked (audit F-23, 26 Oct 2026). A run without a cap says so on its job (`Job.payload.no_cap`).
- Implemented as a final gate in `automation/ordering.py` right before the pay/confirm click; the amount read reuses the existing order-detail scraping logic.

## 6b. Payment modes

Orders are placed either by **card** or by **Cash on Delivery**, chosen per run
on the Ordering page and stored per order (`Order.payment_mode`), so the orders
table can show a Cash-on-Delivery column the way the operator reads it.

| Mode | Card file | Behaviour |
|------|-----------|-----------|
| `card` | **required** | Card entered from the per-run sheet; accounts with no matching card are skipped. |
| `cod` | **not used** | No card is uploaded, parsed or touched. Accounts without a card are included. |

Two rules this must never break:

- **The price cap still applies to COD.** Cash on Delivery can carry a
  collection fee, so the §6a gate reads the final payable total and blocks an
  over-target order exactly as it does for a card.
- **A COD run never falls back to a card.** If Flipkart does not offer COD for
  that product or pincode, the order *fails* rather than quietly charging a
  card the operator did not intend to use.

⚠️ The one piece that cannot be finished from the office tool's screenshots is
the **Flipkart** COD element itself — `selectors.PAY_COD_OPTION` is matched on
the label text "Cash on Delivery" and is marked VERIFY until confirmed against
a live payment page. Flipkart also words it "Pay on Delivery" / "COD" in some
flows.

Not implemented (visible in the reference tool, no request yet): *COD + Card
(advance payment)*, *RTGS*, *IMPS*.

## 7. Delivery phases

1. **Scaffold & data layer** — repo structure, FastAPI skeleton, DB models, Excel import/export, config merge. *(No automation yet; proves data flow.)*
2. **Port automation to Playwright** — login, otp, coupons, ordering as async modules sharing `browser.py` + `selectors.py` + `session_store.py`. Fixes the `getSession` bug. Runnable from CLI/tests first.
3. **Jobs & concurrency** — Arq workers, bounded parallelism, per-account status, live logs.
4. **API + WebSocket** — REST routes + live progress stream.
5. **Frontend** — React pages above, wired to API.
6. **Dockerize & docs** — compose file, README, run instructions, basic tests green.

Each phase is independently runnable so you can review as we go.

---

## 8. Open decisions & risks (need your call)

1. ~~Playwright vs. Selenium~~ — **DECIDED: Playwright** (faster + scalable; selectors port over).
2. ~~Card data~~ — **DECIDED: never stored** (uploaded per run, in memory only, discarded after; no payment OTP on corporate cards) — **revised 25 Oct 2026:** the card's number alone is kept per order at the owner's decision (see §5); expiry and CVV never.
3. ~~OTP~~ — **DECIDED: auto-read from a centralized inbox**, email domain configurable/changeable per run; manual-entry fallback in UI.
4. ~~Deployment~~ — **DECIDED: cloud web server** (AWS ECS Fargate + RDS + ElastiCache; PaaS quick-start optional). App gets its own login.
5. **React frontend vs. lighter HTMX** — still open. React = full app feel; HTMX = faster to ship. I lean React.
6. **Account/throughput limits** — how many accounts and orders per run, realistically? Drives concurrency defaults and whether proxies are needed. *(Not blocking Phase 1.)*
7. **OTP inbox details** — mail provider + how to read it (IMAP catch-all vs. web inbox) + credentials. *(Needed at Phase 2, not Phase 1.)*
8. **Git** — merge into `ordering/` as fresh code (simplest), or preserve the three repos' git history via subtree merges? *(Not blocking.)*

---

## 10. Implementation decisions (Phases 3–6)

Decisions taken while building, recorded here because they change the §3 picture.

### Execution engine: in-process asyncio runner, Redis optional
The job engine is a **bounded-concurrency asyncio runner** (`app/jobs/runner.py`), not Arq-only.
`ORDERING_MAX_CONCURRENCY` caps how many accounts a job processes in parallel. Arq + Redis
(`app/jobs/arq_worker.py`) is an **optional distributed adapter** enabled by setting
`ORDERING_REDIS_URL`; with it blank the whole app runs with no Redis at all.

Why: it keeps local dev and the test suite infrastructure-free (Redis isn't needed to run or test
anything), while still scaling horizontally in production by running more worker containers.

### Ordering jobs are never queued through Redis
**Login and coupon** jobs are queue-safe. **Order** jobs always run in the API process.

Why: enqueuing a job serializes its arguments into Redis. Order jobs carry card details, and those must
never be written anywhere (§5). Running them in-process keeps card data in memory for the life of the
run and nowhere else. Ordering therefore runs in exactly **one** API process, never more: its runs are
tracked in that process, so a second API replica would mark the first one's live runs failed when it
started and let two runs of a kind start at once (audit F-19; compose pins the one `api` container by
name, and DEPLOY.md §3 says how to keep a cloud deploy from running two). Ordering throughput comes
from the accounts run in parallel inside that process (`ORDERING_MAX_CONCURRENCY`, and one browser per
several accounts — below), not from more API copies — an acceptable trade for not persisting card
data. More machines for ordering are a parked item in `docs/SCALING-PLAN.md`.

### Card data defences
- No `Card` table; cards are parsed from a per-run upload (`app/io/cards.py`) into memory only.
- `CardRecord.__repr__`/`__str__` are **masked**, so a stray log line, f-string or stack trace cannot
  leak a card number.
- `Job.payload` holds what a run was asked to do and who asked — product link, quantity, target, coupon
  code, the filter that chose the accounts and how many it matched, the chosen address and GST, the
  signed-in user who started it, and for a card run the sheet rows set aside (by row number and field
  name, never a value) — and never a card's expiry or CVV. One entry can carry part or all of a card
  number: an invoice compilation records the ledger filter it was built from (`payload.filters`), and
  that filter may include the Card Number box — see audit F-20 for how much of the number it keeps.
- The uploaded file is never written to disk.

### One origin, no CORS
The browser only ever talks to its own origin. The frontend calls the relative base `/api`, and both the
Vite dev server and the production nginx proxy `/api` → the API (prefix stripped) and `/ws` → the API
(prefix preserved). This removes CORS from the picture entirely and means no build-time API URL.

### App authentication
JWT bearer tokens and PBKDF2-SHA256 password hashing from the standard library (`app/core/security.py`).
Users and their roles (admin, operator) live in the `users` table (`app/core/users.py`); the configured
admin from the environment is the bootstrap account, seeded at start-up, whose environment password stays
a second key. Every protected request reads the user's row, so a deactivation or a password change takes
effect on the next request (tokens carry the password epoch they were issued under). Every route except
`/`, `/health`, `/auth/login`, `/auth/options` and the two reset-link routes requires a token; the sign-in
and reset doors are throttled. WebSockets carry the token as the `bearer` subprotocol in the
`Sec-WebSocket-Protocol` header — the one header a browser can set on a WebSocket, so the token never
lands in a URL or an access log — and read the same row.

### Per-account progress
`JobItem` rows give each account its own status/message inside a job, and an in-process `EventBus`
publishes progress events that the `/ws/jobs/{id}` WebSocket streams to the UI — so a run of 100 accounts
is legible while it happens, not just afterwards.

### One browser per run, one window per account
Until 25 Oct 2026 every account in a run started a complete Chromium of its own and shut it down
afterwards: ten accounts, ten browsers, each paying its own start-up (a few seconds) and its own memory
(a few hundred megabytes before a page is shown), which is what capped a run at four accounts at once
on an 8 GB machine. A run now opens **one** Chromium and gives each account an isolated *window* in it
(a Playwright context: its own cookies, storage and login, walled off from the other windows like
separate incognito windows), opening another Chromium only past `ORDERING_CONTEXTS_PER_BROWSER`
windows (default 8) — `app/core/browser.py` `BrowserPool`, used by every job body through
`browser_page(..., pool=...)`. The pool is lazy (a run that skips every account launches nothing), a
browser that dies is replaced with the next window, and the pool closes every browser when the batch
ends. What this buys: the memory of one browser per eight accounts instead of per account, no
per-account start-up, and the headroom to raise `ORDERING_MAX_CONCURRENCY`. What it does not change:
the time Flipkart's pages take, and Flipkart's own limits on how many sessions one address may run —
raise the concurrency gradually and watch for captchas. The audit of 25 Oct 2026
(`docs/AUDIT-2026-10-25.md`) and the parked speed items (`docs/SCALING-PLAN.md`) have the rest.

### One account, one run at a time
An order run and a read-back never work one account at once (26 Oct 2026, `app/jobs/account_guard.py`).
The order goes first. A read-back passes over an account an order run is using. If an order starts
on an account mid-read, the read-back stops at its next page, keeps what it has read, and leaves
the invoices for later. The order waits for that page (checking every 2 s, at most 2 minutes)
before it begins. A passed-over account does not count as read back, so the timer reads it again
after the run. The timer itself starts no read-back while an order run is going. The rule is read
off the job tables, not a lock in memory, so it holds across processes too.

### UI colour is computed, not chosen
The interface palette comes from a validated reference palette, checked with a CVD/contrast validator
rather than picked by eye. The status set in use (good `#0ca30c`, warning `#fab219`, critical `#d03b3b`)
passes colour-blind separation (ΔE 11.3) and normal-vision separation (ΔE 27.6). Amber measures only
1.79:1 on the light surface, so **every status indicator pairs colour with a glyph and a text label** —
colour never carries meaning alone. The "serious" hue was dropped because it sits too close to amber.

---

## 9. What I need from you to start

The stack (§3), structure (§4), and the core behaviors (§6a target cap, no card storage, auto-OTP, cloud deploy) are settled. The only open item that affects early code is **React vs HTMX** (item 5) — and even that doesn't block **Phase 1** (scaffold + DB + Excel import/export), which is backend-only.

**Proposed next step:** approve the plan and I start **Phase 1**. React-vs-HTMX can be decided before Phase 5; throughput, OTP inbox details, and git can be answered as we reach them.
