# Speed and scale plan — what is done, what is parked

*Written 25 Oct 2026 for the owner and whoever builds the next steps. The audit of the same day
(`AUDIT-2026-10-25.md`) covers correctness and safety; this file covers throughput only.*

## The decision on the stack

**Stay with Python for the backend and React for the screens.** The owner asked whether moving to
Next.js, React and GraphQL would make the tool faster or more scalable. It would not: the slow part of
every order is a real browser driving Flipkart's pages (product, checkout, address, payment,
confirmation), which takes 20 to 80 seconds per order whatever language drives it — the browser tool
(Playwright) is the same engine in Node.js. GraphQL changes how the screens ask the server for data,
which already answers in under a tenth of a second at this data size, and it fits this tool worse than
plain web calls (file uploads, gigabyte downloads, live progress streams). A rewrite would also discard
about 50,000 lines, 1,350 automated tests and days of live mapping of Flipkart's page elements.

## What actually limits throughput, heaviest first

1. **Flipkart.** It rate-limits and bot-detects. Past roughly 8 to 12 parallel sessions from one
   internet address it answers with captchas and blocked accounts, so pushing harder makes the run
   slower. Traffic from datacenter addresses (AWS and the like) is challenged more than traffic from an
   office or home connection. This ceiling is independent of the code.
2. **Memory per browser.** Until 25 Oct every account launched its own Chromium (a few hundred
   megabytes each). **Fixed: see item 1 below.**
3. **Ordering runs inside the one API process**, by design, so card details never go through a queue.
   More machines therefore do not mean more ordering throughput until item 9.
4. **One run at a time per job type.** Two products cannot be ordered at once (item 8).
5. **The laptop database** (SQLite) allows one writer at a time; the server plan uses PostgreSQL.

## The items

Effort is a working estimate for one developer who knows the code.

| # | Item | What it gives | Effort | Status |
|---|---|---|---|---|
| 1 | One browser per run, an isolated window per account | A browser per 8 accounts instead of per account: far less memory, no per-account start-up, room to raise the parallel setting | 1–2 days | **Done 25 Oct 2026** (`app/core/browser.py` `BrowserPool`, `ORDERING_CONTEXTS_PER_BROWSER`) |
| 2 | Measure each step of an order | Where the seconds go (login, checkout, address, payment, confirmation), so the next fix is the right one | ½ day | Parked |
| 3 | Remember which Address Book address is already saved on which Flipkart account | Skips the 15–40 s saved-address search on every repeat order | 1 day | Parked |
| 4 | Raise `ORDERING_MAX_CONCURRENCY` from 4 to 8, then 12 | More orders per minute | Minutes, plus watching for captchas | Parked — after items 1–3 |
| 5 | Prepare accounts in advance | A background job logs in and saves the address and GST on each Flipkart account ahead of the run, so the ordering run only orders; about half the time per order | 2–3 days | Parked |
| 6 | Proxies, pacing and auto-pause | Accounts spread over several addresses, a speed limit per address, and an automatic pause when Flipkart shows captchas; needs a paid proxy service | 2 days + proxy cost | Parked — before running more than ~10 in parallel |
| 7 | Automatic follow-up on "unconfirmed" orders | A read-back a few minutes after the run settles them without checking accounts by hand | 1 day | **Mostly done 26 Oct 2026**: the timer now reads back every account holding an order still on its way, or unconfirmed within 7 days, every hour (every 30 minutes once out for delivery) — `app/jobs/schedule.py` `tracking_due`. **Done 26 Oct 2026, afternoon:** an account ordered on since its last read-back is read back 3 minutes after the order, once its run has ended (`AFTER_ORDER_MINUTES`) |
| 8 | Several runs at the same time | Two products at once, as long as no account is in two runs | 1–2 days | Parked — when needed |
| 10 | Faster read-backs | One account's automatic follow-up took about 10.5 minutes: Flipkart lists 20 rows (one per unit) and the read-back opens a page for each, 31.4 s a page — 30.0 s of it waiting for an invoice link an order on its way does not have, on the browser's 30 s default (measured live 26 Oct 2026) | ½ day | **Done 26 Oct 2026**: the invoice link gets a budget of its own — a 1.5 s look on an order on its way or cancelled, the 5 s field budget otherwise (`app/automation/sync.py` `_invoice_url`). About 3 s a page, a minute an account. Left, if ever needed: one page per order when its units share it |
| 9 | Worker machines for ordering | Add a machine to add throughput; card details handed to the worker over the internal network, encrypted with a key that lives only in memory, never through the queue | 1–2 weeks | Parked — only when one machine is not enough |

## Item 1 as built

A run opens one Chromium and gives each account a window of its own inside it (a Playwright
*context*: its own cookies, storage and login, walled off from the other windows exactly like separate
incognito windows). Past `ORDERING_CONTEXTS_PER_BROWSER` windows (default 8) it opens another
Chromium. The pool is lazy — a run that skips every account launches nothing — a browser that dies is
replaced when the next account asks for a window, and the pool closes every browser when the batch
ends. The login, coupon, order and read-back jobs all use it; the command-line tool and the product
preview keep a browser of their own.

Measured on the development laptop on 25 Oct 2026 with four empty windows (`about:blank`, no
Flipkart page loaded, so these are the browsers' own weight): four separate browsers took 725 MB
and 1.5 s to be ready; one shared browser with four windows took 363 MB and 0.7 s. Real Flipkart
pages add their own weight per window, so the saving per account shrinks as pages get heavier, but
the browser's fixed cost is paid once per eight accounts instead of once per account.

What to watch after raising the parallel setting: memory on the box (`docker stats`), the share of
accounts ending in "could not reach the payment page" or "not signed in" (Flipkart pushing back),
and captchas in the failure screenshots under `backend/diagnostics/`.

## Notes for items 2–9

- **Item 2 (measure).** `JobItem.started_at/finished_at` already time each account; add a per-step
  stamp inside `run_order` (buy now, address, quantity, payment, confirmation) written to the job
  log, and a small table on the run page. Without it the next items are guesses.
- **Item 3 (address memory).** A table `account_addresses(account_id, address_id, seen_at)` written
  by `ensure_delivery_address` on success; on the next run `select_saved_address` tries the known
  row first and skips the full list scan when the checkout already shows the address.
- **Item 5 (prepare).** A `prepare` job type: login if needed, `ensure_delivery_address`, set the GST
  at account level (the GST step at checkout must be mapped live first — audit F-4), then mark the
  account "ready for <address, GST>". The order run then refuses accounts that are not ready instead
  of fixing them inline.
- **Item 6 (proxies).** A proxy pool with per-address concurrency and a rate; `BrowserPool` already
  takes a proxy per browser, so one browser per proxy is the natural unit. Auto-pause: a captcha or
  "unusual activity" page counted per address; past a threshold the run pauses and raises an alert.
- **Item 8 (several runs).** Replace the per-type guard in `app/api/jobs.py` with "no account in two
  active runs", tracked through `job_items`.
- **Item 9 (workers).** Keep the card rule: the API holds the card map in memory and hands one
  account's card to the worker over an internal, authenticated call just before paying, encrypted
  with a per-run key; nothing card-shaped is ever enqueued. Workers need the same address-book and
  reservation code paths, and the orphan sweep must become heartbeat-based (audit F-19).
