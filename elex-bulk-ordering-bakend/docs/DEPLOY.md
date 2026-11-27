# Deployment guide

How to run the Flipkart Ordering Suite locally in containers and how to put it
on a cloud web server. Architecture rationale lives in [PLAN.md](PLAN.md) §3.

**Where it will run (decided 25 Oct 2026, left to the engineer by the owner):**
one Linux virtual server (4 vCPU / 8 GB to start) running this compose stack —
nginx in front, the API, two Playwright workers, PostgreSQL and Redis — with a
TLS proxy added in front of nginx (the shipped stack has none: §4, "TLS"), and
invoice PDFs and compiled zips on a mounted disk volume
(`ORDERING_INVOICE_DIR`, `ORDERING_ARCHIVE_DIR`) backed up nightly off the box.
S3 only if that volume outgrows the disk; the invoice store already has the
seam for it. The database volume and its backups must be encrypted, because
order rows now carry card numbers (see "A note on card data"). Nothing in this
repository does that: it is a step to carry out on the server (§4,
"Encryption at rest").

---

## 0. Where the files are

The suite is two repositories, checked out **side by side** in one folder:

```
some-folder/
  elex-bulk-ordering-bakend/   the API, the browser workers, this guide, docker-compose.yml
  elex-bulk-ordering-web/      the web UI (React) and its nginx config
```

Everything in this guide is run from the first one. Compose builds the API
image from it and the web image from the folder beside it; if the web
repository is somewhere else, say where with `ORDERING_WEB_DIR=/path/to/it` in
the `.env` beside `docker-compose.yml`. Paths this guide names without a
repository (`app/…`, `.env.example`, `docker-compose.yml`) are in the API
repository; `nginx.conf` and `docker-entrypoint.d/` are in the web one.

## 1. Run the whole stack locally (Docker)

```bash
git clone <the API repository>  elex-bulk-ordering-bakend
git clone <the web repository>  elex-bulk-ordering-web
cd elex-bulk-ordering-bakend
cp .env.example .env        # then fill in the three secrets compose asks for (§2)
docker compose up --build
```

| Service | What it is | Address |
|---------|-----------|---------|
| `web` | React UI behind nginx (proxies `/api` and `/ws`) | http://localhost:8080 |
| `api` | FastAPI app | not published: reached only through `web`, at http://localhost:8080/api and `/ws`. Port 8000 stays inside the compose network (see the comment in `docker-compose.yml`); `/docs` needs `ORDERING_EXPOSE_DOCS=true` and the port published in a `docker-compose.override.yml`, on a laptop only |
| `worker` | Playwright browser workers (login + coupon jobs) | — |
| `migrate` | One-shot: `alembic upgrade head`, then exits; `api` and `worker` start after it (§5) | — |
| `db` | PostgreSQL 16 | internal `db:5432` |
| `redis` | Redis 7 (job queue) | internal `redis:6379` |

**Rehearsed on 26 Oct 2026** on the development laptop, under its own compose
project name and with generated secrets: the three images built from this
tree (the Playwright base image is about 2 GB, the first pull is the slow
part), all five services came up healthy, `/api/health` answered through nginx
about six seconds after `up`, the web app was served, the configured admin
signed in through the proxy and read accounts, orders, alerts, settings,
addresses and stats, a wrong password and an unauthenticated read were refused,
the sign-in throttle logged the forwarded browser address and not the proxy's,
the WebSocket upgrade passed through `/ws/`, and the worker connected to Redis.
So the stack described here starts from scratch as written; what a real
deployment adds is the machine, TLS in front of port 8080 (§4, "TLS"), and the
backups. The one-shot `migrate` service (§5) came later and went through its
own rehearsal on 26 Oct 2026, 13:30 IST, on a fresh PostgreSQL: "upgrade ->
0001 (baseline)" then "0001 -> 0002", the API healthy after it, the same
sign-in and refusal probes passing, and access lines logging paths without
their query strings. The same run loaded every page of the built app in a
real browser (headless Chromium) behind nginx's Content-Security-Policy: no
violation and no console error on any of the 13 pages, and the live run
view's WebSocket opened. A database volume made by an older build
(one with tables but no migration record) needs one step before the first
`up` of this build, or `migrate` stops and the API does not start:
`docker compose run --rm migrate alembic stamp 0001` (§5 explains why).
Since the same day the API pins every PostgreSQL session to UTC (`SET TIME
ZONE 'UTC'` on connect), so the stamps it keeps in plain timestamp columns
are the UTC instants on any host, whatever the machine's or the database's
own zone says; the suite was run against a server set to Asia/Kolkata to
prove it.

Sign in as the configured admin: `ORDERING_ADMIN_USERNAME` with the password
behind `ORDERING_ADMIN_PASSWORD_HASH`. The shipped default (`admin` / `admin`)
is refused at sign-in unless `ORDERING_DEV_MODE=true`, which belongs on a
laptop only. Then add everyone else under **Users** in the app, as admins or
operators (spec FR-058): operators place orders, read them back, upload,
compile and resolve alerts; admins also manage users, addresses, GSTs, unit
limits and settings. A user signs in with their email; a new user gets a
temporary password and must choose their own before anything else opens; a
password change or reset ends that user's other sessions. With a mail server
configured (`ORDERING_SMTP_*` and `ORDERING_APP_URL`) the sign-in page offers
"Forgot password?", which mails a one-hour, single-use link; without one an
admin sets a temporary password under Users. An ordinary mailbox that lets
programs send through it is enough — for Gmail: host `smtp.gmail.com`, port
587, STARTTLS on, the mailbox as user and sender, and an App Password (Google
Account → Security → 2-Step Verification → App passwords), never the account
password. Prove the settings before anyone needs them with
`python -m app.cli test-mail <your address>` from the API repository's folder (on the server:
`docker compose exec api python -m app.cli test-mail <address>`): it prints the
settings without the password, sends one test mail, and explains a refusal in
plain words. No admin can sign in at all?
`python -m app.cli set-password <email>` on the server sets one.

Wrong passwords are throttled: five in fifteen minutes lock that email for
fifteen minutes, and fifty from one address lock the address (an office shares
one, so its limit is far higher — `ORDERING_LOGIN_MAX_FAILURES_PER_EMAIL` /
`_PER_ADDRESS`). For the address to be the browser's and not the proxy's, the
API runs with `--proxy-headers` and trusts `X-Forwarded-For` only from the
private ranges in `FORWARDED_ALLOW_IPS` (set in the API repository's `Dockerfile`); never
widen that to `*` where a browser could reach port 8000 directly, and keep
nginx sending exactly the one address it saw (`$remote_addr`, never
`$proxy_add_x_forwarded_for`, which appends to whatever the browser sent and
would let a browser on a private address name its own). With a TLS proxy or
a load balancer in front of nginx, the address nginx sees is that hop's, the
same for every browser: name the hop in `ORDERING_TRUSTED_PROXIES` (§4,
"TLS") and nginx takes the browser's address from the `X-Forwarded-For` the
hop adds (audit F-30). A run that never
finishes — a worker killed with it — is closed from the Runs page: Cancel
closes a run that never started outright, and a second Cancel closes a
running one that has shown no progress for an hour. The live
progress stream carries the sign-in token in the `Sec-WebSocket-Protocol`
header (as the `bearer` subprotocol), not in the URL, so it never lands in an
access log; nginx passes that header through — any other proxy in front of
`/ws/` must too.

### Scale throughput
```bash
docker compose up -d --scale worker=4
```
Each worker runs browsers, so size on CPU/RAM, not just count. Within a single
job, `ORDERING_MAX_CONCURRENCY` caps how many accounts run in parallel. Never
scale `api`: its runs live in that one process (audit F-19), and its fixed
container name makes `--scale api=2` fail on purpose.

> **Speed has a ceiling that isn't ours.** Flipkart rate-limits and bot-detects.
> Raising concurrency past what the site tolerates produces CAPTCHAs, blocked
> sessions and blocked IPs — i.e. it gets *slower*. Tune `MAX_CONCURRENCY` up
> gradually and add proxies (`ORDERING_PROXY_SERVER`) before pushing volume.

---

## 2. Configuration

Every setting is an env var prefixed `ORDERING_` (full list in
`.env.example`). The ones that matter in production:

| Variable | Why it matters |
|----------|----------------|
| `ORDERING_DATABASE_URL` | `postgresql+psycopg://user:pass@host:5432/ordering` |
| `ORDERING_DB_PASSWORD` | Compose only: the PostgreSQL password. Compose pastes it into `ORDERING_DATABASE_URL` as it stands, so use letters, digits, `-` and `_` only — an `@`, `:`, `/`, `?`, `#` or `%` breaks the address. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`; compose refuses to start without it |
| `ORDERING_REDIS_URL` | `redis://host:6379`; blank = in-process execution only |
| `ORDERING_JWT_SECRET` | **Must** be a long random value; signs login tokens, download tokens and reset links |
| `ORDERING_ADMIN_USERNAME` | The configured admin's sign-in name (an email, or a name such as `admin`) |
| `ORDERING_ADMIN_PASSWORD_HASH` | Preferred over the plaintext fallback. Generate with:<br>`python -m app.core.security hash '<password>'`<br>In the compose `.env` put it in **single quotes**: it holds `$` signs, which compose otherwise reads as variables and strips — a hash with a part missing locks the admin out, and the API names that at start-up as an ERROR |
| `ORDERING_LOG_LEVEL` | Level of the app's own log lines (default `INFO`). The audit trail — one line per request that changed something, with who made it — stays at INFO whatever this says, on stderr, where the container's logs are read from |
| `ORDERING_LOGIN_MAX_FAILURES_PER_EMAIL` / `_PER_ADDRESS` | Wrong passwords tolerated in fifteen minutes before that email (default 5) or address (default 50) is locked for fifteen minutes |
| `ORDERING_TRUSTED_PROXIES` | Compose only, read by the `web` container (nginx) at every start: the addresses or CIDRs of the TLS proxy or load balancer in front of it, separated by spaces or commas. Empty by default, which is right only while browsers reach port 8080 directly; with a hop in front and this empty, every browser shares one sign-in throttle count. Which value: §4, "TLS". A value that is not an address stops `web` at start, and `docker compose logs web` names it |
| `ORDERING_DEV_MODE` | Keep `false`: `true` accepts the shipped default admin password |
| `ORDERING_PBKDF2_ITERATIONS` | Password hashing work factor (default 390000) |
| `ORDERING_EXPOSE_DOCS` | Keep `false`: `true` serves `/docs` and `/openapi.json` |
| `ORDERING_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM`, `_STARTTLS`, `ORDERING_APP_URL` | A mail server and the app's public address (the `https://…` address browsers use), for the "Forgot password?" link; blank = no link, admins reset passwords. Compose names all seven in the environment the `api`, `worker` and `migrate` services share, so put them in the project's `.env` (a value with a `$` in single quotes, as for the admin hash) and they reach the API. Left out, the host, sender and address stay blank, which keeps the link off, and the port and STARTTLS keep 587 and on. Prove them with `docker compose exec api python -m app.cli test-mail <your address>` |
| `ORDERING_MAX_CONCURRENCY` | Accounts in parallel per job |
| `ORDERING_BROWSER_HEADLESS` | Hidden (`true`) or shown (`false`) browser on this machine. Each machine decides in its own `.env`; the Settings page has no switch for it (it had one, shared by every machine, until 24 Nov 2026). Read when the API starts: restart the API (and any worker) after changing it. Keep `true` on a server (no display) |
| `ORDERING_PROXY_SERVER` | Route browser traffic through a proxy (blank: none). The Settings page can set it instead, and wins |
| `ORDERING_ARCHIVE_RETENTION_DAYS` | Days a compiled invoice archive is kept (default 90, spec C-11); compose passes it from the `.env`. |
| `ORDERING_OTP_IMAP_*` | Central OTP inbox (host, port, user, password, mailbox, SSL). Blank = no automatic OTP reading. Compose passes all six from the project's `.env` (port, mailbox and SSL default to 993, INBOX and on); the Settings page can set them instead, and wins. |
| `ORDERING_OTP_IMAP_USER_2` / `_PASSWORD_2` (then `_3` …) | More inboxes, for accounts whose login codes land in another mailbox. `_FOR_2` lists the account emails (or `@domain`) it serves, its own address when left out. A Gmail address needs no `_HOST_2` (its codes are read from imap.gmail.com); any other borrows the first inbox's host from the server's `.env` (as compose passes it), never one saved on the Settings page, so moving the first inbox there never sends this inbox's App Password elsewhere. The Settings page lists an inbox it cannot use, with the reason. Compose passes the second inbox's `_USER_2`, `_PASSWORD_2`, `_FOR_2` and `_HOST_2` from the project's `.env` (blank means none, and a blank host the first inbox's); a third or later inbox goes in a `docker-compose.override.yml` on the server (an `environment:` block under `api` and `worker`, values from the `.env` as `${ORDERING_OTP_IMAP_USER_3}`, its `_HOST_3` among them), rather than in `docker-compose.yml`. |
| `ORDERING_TRACK_EVERY_MINUTES` / `ORDERING_OUT_FOR_DELIVERY_MINUTES` | Placed orders are read back on their own until they arrive: every 60 minutes while on their way, every 30 once Flipkart says out for delivery (the delivery code appears only then). Only the accounts holding such orders are read, never while another run is going. `0` switches either off |

**On Docker** a container gets only the variables `docker-compose.yml` names:
the image copies no `.env`. The `api`, `worker` and `migrate` services share
one list, which takes each row above from the project's `.env`, with the
app's own default when one is left out, except these: `ORDERING_DATABASE_URL`
and `ORDERING_REDIS_URL`, which compose fills in itself; `ORDERING_MAX_CONCURRENCY`
and `ORDERING_BROWSER_HEADLESS`, written into the file as 4 and true (the
Settings page changes the concurrency, and wins); and `ORDERING_DEV_MODE` and
`ORDERING_EXPOSE_DOCS`, which a server keeps off. To give the containers one of
those, or any setting this table leaves out (`.env.example` lists them
all), put a `docker-compose.override.yml` beside `docker-compose.yml` with an
`environment:` block under `api`, `worker` and `migrate` naming it (`migrate`
upgrades the database `ORDERING_DATABASE_URL` names, so a database address set
for the other two must reach it too), for example
`ORDERING_CONTEXTS_PER_BROWSER: ${ORDERING_CONTEXTS_PER_BROWSER}` with the value
in the `.env`, then run `docker compose up -d`. A value set in the `.env` alone
never reaches them.

The email **domain can change per run** — the OTP reader matches on the
recipient address, so a catch-all mailbox works without reconfiguration.

---

## 3. Cloud deployment (recommended: AWS)

The containers are the unit of deployment; nothing is tied to AWS specifically.

One thing changes behind a load balancer (audit F-30, 25 Oct 2026): nginx then
sees the balancer's address, not the browser's, so the sign-in throttle would
key fifty wrong passwords from anywhere onto one address and lock everyone
out at once. Set `ORDERING_TRUSTED_PROXIES` on the `web` service (on ECS, an
environment variable of its task) to the addresses the balancer connects
from, and nginx takes the browser's address from the `X-Forwarded-For` the
balancer adds; nothing in the web repository's `nginx.conf` needs editing. An AWS
Application Load Balancer connects from addresses in the subnets it is
attached to: list those subnets' CIDRs (for example
`10.0.0.0/24,10.0.1.0/24`), or the VPC's (`10.0.0.0/16`) when nothing else
inside it can reach `web`. Another provider: the ranges it documents for its
balancer or proxy. A balancer that writes its own address into the header
after the browser's (Google Cloud's does) needs that address listed too, or
nginx stops at it. Should the balancer send `/api` straight to the `api`
service rather than through `web`, nginx is not on that path and the setting
does not apply there: uvicorn's own trust (`FORWARDED_ALLOW_IPS`, every
private range, the VPC's included) reads the balancer's header from the right
and finds the browser's address, as long as browsers come from public
addresses.

**One API at a time, during a deploy too (audit F-19).** Runs are tracked
inside the one API process, so two must never run at once — not even for the
minute a deploy overlaps the old container and the new: the new one's start-up
marks the old one's live runs failed, and while both are up two runs of a kind
can start together. Rolling and zero-downtime deploys — ECS's default among
them — start the new API before they stop the old one, which is exactly that
overlap. Set the `api` service to stop the old one first: on ECS, a desired
count of 1 with `minimumHealthyPercent: 0` and `maximumPercent: 100`, and no
auto-scaling on that service; on Fly.io, Render or Railway, check the api's
deploy strategy and pick one that stops the old instance before it starts the
new, or, where the platform has none, scale the api to zero before deploying
it and back to one after.
Give the old one time to finish its orders as it stops: the API waits up to
80 s for the runs in flight, so its stop timeout must be at least 90 s (on
ECS the container's `stopTimeout`, 30 s unless set; Fargate allows up to
120 s). Only `api` is bound by this; `web` and `worker` can roll as usual.

```
            ┌── ALB (HTTPS, ACM cert) ──┐
            │                            │
      ┌─────▼─────┐              ┌───────▼───────┐
      │  web      │              │  api          │   ECS Fargate services
      │ (nginx)   │              │ (FastAPI)     │
      └───────────┘              └───────┬───────┘
                                         │
                      ┌──────────────────┼──────────────────┐
                 ┌────▼────┐       ┌─────▼─────┐      ┌─────▼─────┐
                 │ workers │       │ RDS       │      │ElastiCache│
                 │ (scale) │       │ Postgres  │      │  Redis    │
                 └─────────┘       └───────────┘      └───────────┘
```

1. **Images** — build and push the two images (the API repository's and the web repository's) to ECR.
2. **Data** — RDS PostgreSQL + ElastiCache Redis, both private-subnet only.
3. **Services** — three ECS Fargate services: `web`, `api` (one task, never
   more — see "One API at a time" above), `worker`.
   Give `worker` its own auto-scaling policy; browser work is CPU/RAM-heavy and
   should scale independently of the web tier. Start workers at 2 vCPU / 4 GB.
4. **Ingress** — ALB with an ACM certificate, HTTPS only, HTTP redirected.
   Make sure the ALB has **WebSocket support** (it does by default) and a read
   timeout ≥ 300s so live progress streams don't get cut.
5. **Secrets** — put `JWT_SECRET`, `ADMIN_PASSWORD_HASH`, DB credentials and
   the OTP inbox password in AWS Secrets Manager; inject as task secrets, never
   bake into an image.
6. **Logs** — CloudWatch log group per service.

### Cheaper / faster first deploy
Same images work unchanged on **Fly.io, Render or Railway** with their managed
Postgres + Redis. Deploy `api`, `worker` and `web` as three services. Moving to
AWS later is a re-point of env vars, not a rewrite — that's why the stack is
containerized.

---

## 4. Security checklist before going live

- [ ] `ORDERING_JWT_SECRET` set to a long random value (not the default)
- [ ] `ORDERING_ADMIN_PASSWORD_HASH` set; plaintext `ADMIN_PASSWORD` removed
- [ ] HTTPS enforced end to end (the app login travels over it) — the shipped stack has no TLS; see "TLS" below
- [ ] Database and Redis reachable only from the app's private network
- [x] `/docs` and `/openapi.json` are off unless `ORDERING_EXPOSE_DOCS=true` (default off since 24 Oct 2026)
- [ ] Postgres backups / snapshots enabled
- [ ] The database volume and every backup encrypted — a step on the server; see "Encryption at rest" below
- [x] `FORWARDED_ALLOW_IPS` names only the proxy's network (the Dockerfile's private ranges), and port 8000 is not published
- [ ] `ORDERING_TRUSTED_PROXIES` names the TLS proxy or load balancer in front of `web` (see "TLS" below), so the sign-in throttle counts each browser on its own
- [x] The audit trail reaches the container log (`ORDERING_LOG_LEVEL`, section 2)

### TLS — not in the shipped stack
The `web` container serves plain HTTP on port 8080, and nothing in
`docker-compose.yml` terminates TLS: without an addition, the sign-in, the
card sheet and the card numbers on screen cross the internet in clear. Add one
of these in front of `web`:

- **A TLS proxy on the server** — Caddy, which obtains and renews a Let's
  Encrypt certificate by itself, or nginx with certbot — answering on 443 (and
  on 80, to redirect and for the certificate check) and passing requests to
  `web`. Then publish `web` on the loopback only (`"127.0.0.1:8080:80"` under
  its `ports:`), or not at all when the proxy runs in the same compose project
  and reaches `web:80` directly, so port 8080 is not open to the internet.
- **A cloud load balancer with a certificate**, as in §3.

Either way nginx then sees the proxy's address instead of the browser's, and
the sign-in throttle would count every browser as one (audit F-30). Name the
proxy in `ORDERING_TRUSTED_PROXIES`, in the project's `.env` beside
`docker-compose.yml` (addresses or CIDRs, separated by spaces or commas), and
run `docker compose up -d` to recreate `web` with it. At every start nginx's
config is rendered from the web repository's `nginx.conf` with a `set_real_ip_from` line
for each, then `real_ip_header X-Forwarded-For` and `real_ip_recursive on`
(`docker-entrypoint.d/40-trusted-proxies.sh` in the web repository; nothing to edit by
hand). For a connection from the proxy, `$remote_addr` is then the browser's
address, read from the right of the proxy's `X-Forwarded-For`, and nginx
still passes the API that one address. The value is the address nginx inside
the `web` container actually sees, which is not `127.0.0.1`:

- **A proxy on the host** (Caddy or nginx installed on the server) reaches
  `web` through Docker's port forwarding, so every request arrives from the
  compose network's gateway: use that address. This prints the gateway, then
  the subnet:
  `docker network inspect ordering_default --format '{{range .IPAM.Config}}{{.Gateway}} {{.Subnet}}{{end}}'`
  — for example `ORDERING_TRUSTED_PROXIES=172.18.0.1`. Do it together with
  publishing port 8080 on the loopback only (above), so that nothing but the
  server's own programs can reach nginx through the gateway.
- **A proxy in the same compose project** (a `caddy` service beside `web`)
  arrives from its own container's address, which changes when it restarts:
  use the network's subnet, the second value printed above, for example
  `ORDERING_TRUSTED_PROXIES=172.18.0.0/16`.
- **A cloud load balancer**: the ranges §3 gives for it.

The hop you list must write `X-Forwarded-For` itself, with the address it
actually received the browser's connection from; nginx then trusts the
right-hand end of that header. Caddy does this by default. nginx as the
proxy needs `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
(or `$remote_addr`) in every location that passes to `web`, as in the site
below; a plain `proxy_pass` hands on whatever header the browser sent, and a
browser could then name any address it liked. An AWS ALB appends by default
(leave its X-Forwarded-For setting on "append"). Never list a TCP-only
(layer-4) balancer, such as an NLB, or any hop that passes the header
through untouched.

**nginx as that proxy needs its whole site written out.** Left at its
defaults it refuses a request body over 1 MB (one upload of invoice PDFs is
often bigger), does not pass on the WebSocket upgrade the live run view
needs, and waits only 60 seconds for an answer where `web` waits 300. It
also keeps a request body in memory only up to about 10 KB and writes
anything larger to its own temporary folder before passing it on; a card
sheet of a hundred or so rows is larger, and so is an account workbook, an
order-id sheet or an order report in the old layout, which carries card
columns. `web`'s own nginx streams those five routes for that reason
(the web repository's `nginx.conf`), and the outer one must too. Here is the whole site, as one file such as
`/etc/nginx/conf.d/ordering.conf`; `sudo certbot --nginx -d <your domain>`
then adds the `listen 443 ssl` and certificate lines, and the redirect from
port 80:

```nginx
# The request line without its query string: a ledger search by card
# (?card=) and an archive's download link (?token=) stay out of the logs.
log_format ordering_no_query '$remote_addr - $remote_user [$time_local] '
                             '"$request_method $uri $server_protocol" $status '
                             '$body_bytes_sent "$http_user_agent"';

server {
    listen 80;
    server_name orders.example.com;     # your domain
    client_max_body_size 25m;           # web's own limit
    access_log /var/log/nginx/ordering-access.log ordering_no_query;
    error_log /var/log/nginx/ordering-error.log crit;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        # A big answer (the ledger's CSV carries full card numbers) passes
        # through memory to a slow browser, never this server's disk.
        proxy_max_temp_file_size 0;
    }

    # Invoice PDFs come in by the hundred and a compiled archive goes out at
    # a gigabyte or more: both streamed, never copied to this server's disk.
    location /api/invoices/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 500m;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_max_temp_file_size 0;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # The live run view's socket: the upgrade passed on, the socket kept open.
    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Sheets that can hold card numbers, streamed to web as they come:
    # a card sheet (a run's Start, and its preview)...
    location = /api/jobs/order {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        client_max_body_size 1m;
        proxy_read_timeout 300s;
    }
    location = /api/jobs/order/card-preview {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        client_max_body_size 1m;
        proxy_read_timeout 300s;
    }
    # ...and an account workbook or an order-id sheet in the old layout.
    location = /api/accounts/import {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        client_max_body_size 25m;
        proxy_read_timeout 300s;
    }
    location = /api/orders/import-ids {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        client_max_body_size 25m;
        proxy_read_timeout 300s;
    }
    location = /api/orders/import-report {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;
        client_max_body_size 25m;
        proxy_read_timeout 300s;
    }
}
```

Each of the five exact (`=`) locations must be written out in full: an
empty one answers 404 without reaching `web`, and a missing one leaves its
sheet to `location /`, which writes it to the outer nginx's own disk first.
The log lines mirror `web`'s own nginx (audit F-20 and F-29): the host's
usual log format records each request's query string, which can hold a card
number searched for in the ledger or an archive's download token, so this
site logs paths only and keeps its error log to critical lines.
Caddy's `reverse_proxy` needs none of this — it passes WebSocket upgrades on
its own, sets no request-body limit unless one is configured, and streams
request bodies (its `request_buffers` is off by default) — and an AWS ALB
needs only what §3 says.

After `sudo nginx -t` and `sudo systemctl reload nginx`, check it through
the proxy: on Place orders, with an account chosen, pick a card sheet
(without pressing Start) and see the table "Which card pays for which
account" appear, not "The run would refuse this sheet"; then start a
read-back (Order Units, "Read back from Flipkart", then "Sync from
Flipkart"; one account is enough) and see its live view say "Live", not
"Reconnecting".

Docker picks the compose network's range when it creates the network, and
`docker compose down` removes it, so the next `up` may land on another range.
Check the value again after a `down`, or fix the range in a
`docker-compose.override.yml` (it takes effect at the next `down` and `up`):

```yaml
networks:
  default:
    ipam:
      config:
        - subnet: 172.30.0.0/24   # any private range no other network on the server uses
```

and the gateway is then `172.30.0.1`. List the proxy and nothing else: any
address listed can write whatever browser address it likes into
`X-Forwarded-For`, so never a range browsers connect from, such as an office
network (a `/0` is refused outright). `docker compose logs web` shows what
the start made of the value ("taking the browser's address from
X-Forwarded-For on connections from: 172.18.0.1"), or why it refused it, in
which case `web` does not start. To see it work, sign in once with a wrong
password through the proxy: the API's line,
`docker compose logs api | grep "failed login"`, should end in your
browser's address, not the gateway's.

Set `ORDERING_APP_URL` to the `https://` address, so reset links use it.

### Encryption at rest — a step on the server
The order rows hold full card numbers (see "A note on card data"), and so does
every copy of the database. Nothing in this repository encrypts a disk or a
backup, and the compose volumes are plain Docker volumes; on the server:

- Keep Docker's data directory (`/var/lib/docker`, where the `pgdata` and
  `appdata` volumes live) on an encrypted disk: the provider's encrypted block
  storage, or LUKS on a disk of your own.
- Encrypt each `pg_dump` before it leaves the box (with `age` or
  `gpg --symmetric`, for example), and keep the key off the server.
- A laptop that runs the app on SQLite keeps whole copies in
  `backups/`: its disk must be encrypted too (FileVault on a Mac).

### Backups, and what a laptop set-up gets for free
On PostgreSQL use the platform's snapshots or `pg_dump` on a schedule — the
app does nothing itself. A **SQLite** database (a laptop, a single-server
trial) is copied by the app at every start, before anything touches its
schema: `backups/ordering-<date>-<time>.db` beside the file, the newest ten
kept and, besides them, the newest copy of each of the last 30 days, so a
burst of restarts cannot push older recovery points out (`BACKUPS_KEPT` and
`BACKUP_DAYS_KEPT` in `app/db/base.py`); an unchanged or empty file is
skipped. That copy exists because on 25 Oct
2026 the development database was emptied by a table drop that reached it
from a script run beside the app; the engine now refuses to drop tables in any
database that is not a throwaway one (SQLite in memory, or a file under the
temp directory — what the tests use), unless `ORDERING_ALLOW_DROP=1` says so.
Never run a script that imports the app from the API repository's folder without pointing
`ORDERING_DATABASE_URL` at a temp file first.

### Proving the suite on PostgreSQL
Development runs on SQLite, the compose stack on PostgreSQL, and the two do
not behave alike in every corner (enum ordering, grouping rules, foreign keys
— SQLite only enforces those because the app now asks it to on every
connection). Before a deploy, run the whole suite against a scratch
PostgreSQL once:

```bash
docker run -d --name ordering-pg-test -e POSTGRES_PASSWORD=test -e POSTGRES_USER=ordering \
  -e POSTGRES_DB=ordering_test -p 54329:5432 postgres:16
cd backend && ORDERING_TEST_DATABASE_URL="postgresql+psycopg://ordering:test@127.0.0.1:54329/ordering_test" \
  .venv/bin/python -m pytest -q
docker rm -f ordering-pg-test
```

`ORDERING_TEST_DATABASE_URL` is read only by the test fixtures (the app's own
`ORDERING_DATABASE_URL` never is), and naming it also allows the fixtures to
drop the tables of that one database. First run on 26 Oct 2026: 1,289 of
1,299 passed before fixes, all after — see
`docs/specs/order-unit-ledger/remaining-work.md` §0 for what it caught.

### What a gigabyte of invoices costs
Measured 26 Oct 2026 on the development laptop: compiling 1,024 one-megabyte
PDFs into one archive took 2 s and streaming it back through the download
route 2.5 s, with the API process's memory flat at about 130 MB throughout —
the build and the download both stream. Disk, not memory, is what the
`appdata` volume needs sizing for.

### A note on card data
Card details are uploaded per ordering run (one sheet, `.xlsx` or `.csv`, read
straight from the upload into memory — nothing is written to disk, provided
any nginx put in front of `web` streams the five routes §4 "TLS" lists;
`web`'s own nginx and the API keep a card sheet of up to 1 MB in memory and
refuse a larger one before reading it; an account sheet or a sheet of order
ids in the legacy layout carries card columns too, which neither import
reads, and is kept in memory the same way, up to 25 MB), held in
process memory for that run, and discarded — no card table, no Redis queue, no
logs. The sheet may carry a password column (any heading naming a password or
PIN): HDFC's corporate cards ask for that password on the bank's own page
after Pay instead of a one-time code, and the run types it there (one password
field, then submit) and never anywhere else. A bank that asks for a one-time
code instead needs a person at a visible browser window; the run waits
`ORDERING_BANK_OTP_FILL_WAIT` seconds (default 300) for that. Since the audit of 25 Oct 2026 that holds on the failure paths too: a
card field that cannot be filled is logged by field name and error type only
(the browser driver's own error text would repeat the value typed), a failed
database write logs its error's type and the engine hides statement
parameters, and the diagnostic screenshot of a failed or unconfirmed checkout
paints over the card form's boxes and any password box in the picture (it
empties them first only when a painted-over picture cannot be taken). Those screenshots live in
`diagnostics/`, show a checkout (name, address, phone, GSTIN), are
pruned after a fortnight, and are git-ignored along with the database, its
backups and the env files. That is why
ordering jobs deliberately run in the API process rather than being queued
through Redis (see `app/api/jobs.py` and PLAN.md §5). Card records also mask
themselves in `repr()`/`str()` so a stray log line or stack trace cannot leak
a card number.

**Since 25 Oct 2026, at the owner's decision, the number of the card an order
was paid with is kept on that order's row** (the ledger's Card Number column
and filter, and every export), so orders can be matched against card
statements. The expiry and the CVV are still never written anywhere. This
means the database, its backups and the CSV files now hold full card numbers:
keep the database volume and backups encrypted (a step on the server — §4,
"Encryption at rest") and off the public network,
treat downloaded exports as sensitive files, and give sign-ins only to people
who may see the cards.

Card data also transits the app, so: serve only over HTTPS, keep access
limited to the app's sign-in, and prefer corporate cards with spend controls.

---

## 5. Database migrations

Two things keep the database in step with the models
(`app/db/models.py`), each where it fits:

- **Alembic owns a PostgreSQL deployment's schema** (since 26 Oct 2026, audit
  F-33). The revisions in `alembic/versions/` are the schema's
  history: `0001` is the baseline, the whole schema exactly as `create_all`
  builds it; `0002` widens `compiled_archives.size` to BIGINT where an older
  table still has INTEGER (an archive passes 2 GiB). That is the hand-run
  `ALTER` this section used to ask for, and it does nothing where the column
  is already right, or on SQLite, which needs nothing. `0003`–`0009` add the
  reason Flipkart gave for a cancellation, the change feed's events, a GST and
  billing phone per account, a unit's promised and actual delivery dates and
  payment, the first promise a slipping delivery's later ones do not replace
  (filled from the promise already held), every step of a cancel or
  reschedule request (`0008`'s `unit_request_events`; the API fills in the
  steps of older requests at its next start), and who marked a unit's goods
  received, and when (`0009`); each adds only what is missing. Alembic reads the
  database address from the app's own settings (`ORDERING_DATABASE_URL`, or
  the `.env` beside the app), never from `alembic.ini`.
- **`init_db()` still runs at every start**, unchanged: it creates missing
  tables and adds missing nullable columns and indexes, and never retypes,
  renames or drops anything. The laptop's SQLite database and the test suite
  run on exactly that. On PostgreSQL, after Alembic has run, it finds no gap
  in the schema to fill.

### A fresh PostgreSQL
`alembic upgrade head` creates everything. Compose does it by itself: the
one-shot `migrate` service runs it once per `docker compose up`, and `api` and
`worker` start only after it has finished without an error. A failed
migration stops the deploy there, with the reason in
`docker compose logs migrate`, rather than starting an API whose code does
not match its schema. On a platform that runs the services separately (§3),
run `alembic upgrade head` from the backend image, with the API's
`ORDERING_DATABASE_URL`, before starting the new API.

The order matters on every deploy: migrations first, then the API. A newer
API started first lets `init_db` add that build's new nullable columns
itself, and the revision that adds them then stops on "already exists".
Should that happen, mark that revision done with
`alembic stamp <that revision>`, then run `alembic upgrade head` and
`alembic check`.

### An existing PostgreSQL made by an older build
A database the app created before Alembic arrived has every table but no
record of a revision, so the baseline refuses to run over it ("already holds
the app's tables"). Tell Alembic once where it stands, then upgrade, before
the new build's first `up`. Take a `pg_dump` first:

```bash
docker compose build
docker compose run --rm migrate alembic stamp 0001    # "this database is at the baseline"
docker compose run --rm migrate alembic upgrade head  # 0002: size to BIGINT, only if still INTEGER
docker compose run --rm migrate alembic check         # expect "No new upgrade operations detected."
docker compose up -d
```

`alembic check` compares the database with the models. Anything it lists is a
difference the older build left behind (a NOT NULL column `init_db` could not
add, for example) and needs a revision of its own before going on.

### A database `init_db` built
When Alembic is installed, `init_db()` stamps a database it has just built
from nothing with the newest revision (`app/db/migrations.py`): its whole
schema came from `create_all` a moment earlier, which is what the newest
revision describes, so `alembic current` names it and a later
`alembic upgrade head` applies only what came after. That covers a PostgreSQL
the API was started on before any migration ran. A database that already had
tables is never stamped — the laptop's own included. The stamp records the
revision a database was born at, and `init_db` never moves it afterwards, so
a SQLite file `init_db` keeps up to date is re-stamped before Alembic takes
it over. Alembic comes from the backend's own venv, and a venv set up before
26 Oct 2026 lacks it, so run `pip install -r requirements.txt` in the
activated venv once (the backend setup in [README.md](../README.md)). To put
any such database under Alembic, start the current app on it once (so
`init_db` brings it up to date), then from the API repository's folder:

```bash
alembic stamp 0004      # "everything up to the change feed is here"
alembic upgrade head    # 0005: adds the one thing init_db could not, below
alembic check           # expect "No new upgrade operations detected."
```

Why not `alembic stamp head`: `init_db` adds new tables, nullable columns
and indexes, but never a foreign key. Revision `0005` links an account to its
own GST (`accounts.gst_id` → `gsts.id`, ON DELETE SET NULL), the first part of
the models `init_db` cannot add to a table that already exists. A database it
keeps up to date has the column but not the link, so stamped at head it would
not match the models, and `alembic check` would say so. Stamped at `0004`,
`alembic upgrade head` runs `0005`, which finds its columns and index already
there and adds only the link — after setting to NULL any `gst_id` of a GST
deleted since, as the link itself would have. A later revision that adds
something else `init_db` cannot will move the revision to stamp; its own
docstring says so.

### Every model change gets a revision
1. Change the model.
2. Generate the revision against a scratch database at head, never the real
   one:
   ```bash
   cd backend
   export ORDERING_DATABASE_URL=sqlite:////tmp/ordering-migrations.db
   alembic upgrade head
   alembic revision --autogenerate --rev-id 00NN -m "what changed"   # the next free number
   ```
3. Read and tidy `alembic/versions/00NN_what_changed.py`. Autogenerate cannot
   see a rename (it writes a drop and an add, which loses the column's data);
   a NOT NULL column on a table that has rows needs a `server_default` or a
   backfill first; and it does not notice a new member of an enum (the API's
   start-up still adds those on PostgreSQL, `sync_enum_values`, or a revision
   can: `ALTER TYPE ... ADD VALUE`). Revisions are written in batch mode, so
   the same file runs on SQLite in the tests and on PostgreSQL in production.
4. `alembic upgrade head` and `alembic check` on the scratch database, then
   the suite. `tests/test_w1c_migrations.py` fails while the revisions and the
   models disagree: that is the guard that makes every model change come
   with a migration. It runs on PostgreSQL too when `ORDERING_TEST_DATABASE_URL`
   names one ("Proving the suite on PostgreSQL", §4).

`alembic upgrade <from>:head --sql` prints the SQL instead of running it,
without connecting, for review before a deploy. A revision that drops a table,
and any `alembic downgrade` past the baseline, meets the same guard as every
other drop (§4, "Backups"): refused on anything but a throwaway database unless
`ORDERING_ALLOW_DROP=1` is set for that one command. Take a `pg_dump` first.

### SQLite: rebuilt tables, and a copy left behind
SQLite can change almost nothing about a table in place, so on SQLite a
revision that drops a column or adds a foreign key rebuilds the table:
Alembic copies its rows into `_alembic_tmp_<table>`, drops the table and
renames the copy. Every downgrade of `0003`–`0007` and of `0009` does this
(`order_units`, `users`, `orders` and `accounts`), and so does the upgrade to
`0005`, which links `accounts` to `gsts`. The drop guard lets Alembic swap a
table for that full copy, so a rebuild needs no `ORDERING_ALLOW_DROP=1`; the
downgrades of `0004` and `0008` still do, because they drop a whole table
(`unit_events`, `unit_request_events`), and so does any downgrade past the
baseline. Each step runs in one transaction on SQLite
too, so a step that is refused or fails part-way leaves the file as it was,
and the same command can simply be run again (with the override, if the
refusal asked for it).

A rebuild that failed under an older build could leave the copy behind, and
every later attempt then stops on `table _alembic_tmp_<table> already exists`.
To clear it, stop the app, take a copy of the file, drop the table the error
names by hand, and run the command again — from the API repository's folder:

```bash
cp ordering.db ordering-before-cleanup.db
sqlite3 ordering.db 'DROP TABLE _alembic_tmp_accounts'   # the table the error names
alembic upgrade head                                     # or the command that stopped
```

## 6. Moving this machine's database to a hosted PostgreSQL (Neon)

The laptop keeps everything in one SQLite file, `ordering.db`. Moving it to a
hosted PostgreSQL such as [Neon](https://neon.com) takes the data off the
laptop and onto managed storage with its own backups. The app itself still
runs on this machine: it drives a browser.

**Before.** Create the Neon project in **Singapore** (`aws-ap-southeast-1`,
the nearest to India; there is no Mumbai, and a project's region cannot be
changed later), on PostgreSQL 16, the version the suite is proven on. Take its
**direct** connection string: in Neon's Connect box, with connection pooling
switched **off** (the pooled one has `-pooler` in its host). The app pins each
connection to UTC, and a pooler drops that between transactions; the copy also
sets the database itself to UTC, so a pooled address is safe after it, but the
direct one is the one to use. Never paste the string anywhere but the `.env`:
whoever holds it can read every account, sign-in and card number.

**Steps**, from this folder:

1. Stop the app.
2. Add one line to `.env`: `ORDERING_NEW_DATABASE_URL=<the direct string>`.
   Leave `ORDERING_DATABASE_URL` as it is for now: the app must still read
   the SQLite file.
3. `.venv/bin/python -m app.cli copy-to-postgres --check` — it reaches the new
   database, sees that it is empty, and counts the rows here. Nothing is written.
4. `.venv/bin/python -m app.cli copy-to-postgres` — it builds the tables,
   copies every row, moves each id counter past the copied ids, records the
   newest migration, sets the database to UTC and compares the row counts of
   every table on both sides. It all happens in one transaction: a failure
   leaves the new database as it was, and the SQLite file is only read.
5. In `.env`, set `ORDERING_DATABASE_URL` to the same string as
   `ORDERING_NEW_DATABASE_URL`, and start the app. Check the dashboard and
   Order Units show what they showed before.

If `ORDERING_DATABASE_URL` in `.env` already names the new database (the
line was switched first), leave it: name the SQLite file for the copy alone,
`ORDERING_DATABASE_URL=sqlite:////full/path/to/ordering.db python -m app.cli
copy-to-postgres`, and the copy goes into the database the file names. An app
started against the new database once has built its tables and its own admin
sign-in; the copy replaces that one untouched sign-in and refuses any other row.

**Done on 11 Nov 2026:** the laptop's 312 rows went into Neon (Singapore,
PostgreSQL 18), every value of all 19 tables compared equal afterwards, and the
database reads UTC on a fresh connection.

**After.**
* `ordering.db` stays where it is, unchanged: the way back is to set
  `ORDERING_DATABASE_URL` back to `sqlite:///./ordering.db`. Anything done
  after the move is only in Neon.
* The app's own copies in `backups/` are made for a SQLite file only. On Neon
  the backups are its restore window (how many days depends on the plan).
* Invoice PDFs and compiled zips are files in `data/`, not rows: they stay on
  this machine.
* Neon puts the database to sleep after a few quiet minutes and closes the
  app's connections; the app tests each one before use and opens a new one, so
  the first page after a quiet spell is only a moment slower.
* Never point `ORDERING_TEST_DATABASE_URL` at this database: the test suite
  drops every table it is pointed at. Use a Neon branch made for the purpose.

