# Roadmap — gaps against the reference portal

> **Audience:** Akanksha, deciding what to build next (and anyone who picks up the work).
> **Source:** screenshots of the existing BulkOrderBuddy portal, compared against what
> Phases 1–6 actually built. See [PLAN.md](PLAN.md) for the current design.
> **Status (26 Oct 2026):** Phases 7–11 are built; Phase 12 is parked and Phase 13 dropped at the owner's request. A row marked ✅ below says when it closed; a row still marked ❌ is not built and not asked for.

---

## 0. Read this first: we built for 40 accounts, you run 37,062

This is not a feature gap, it's a **defect at your real scale**, and it outranks
everything else below.

| Where | What it does today | What happens at your scale |
|-------|--------------------|----------------------------|
| `GET /accounts` | Returns **every** account in one response, no paging, no filters | A multi-megabyte payload |
| Accounts table | Renders one row per account | ~37,000 DOM rows — the tab freezes |
| Account picker | Renders one checkbox per account | Same, on every run page |
| `GET /orders` | `limit=100` default | Fine now, won't hold for 140+ units a month over time |

**Everything else on this list is optional. This part is not** — the tool is
unusable against your real data until it's fixed. Treat it as Phase 7.

**Approach:** server-side paging + filtering + sorting (`?page=&size=&q=&status=&tag=`),
a total count returned alongside, and the account picker switched from "render
everything" to search-and-select with a "select all *matching the filter*" that
sends a **filter**, not 37,000 ids. Add the DB indexes the filters need.
*Size: M.*

---

## 1. Gap summary

Grouped by area. "Have" = working today; "Partial" = exists but much thinner.

### Ordering

| Capability | Us | Notes |
|---|---|---|
| Price cap (their "Final Amount Limit") | ✅ Have | Ours is stricter — blocks the order outright |
| Payment: Card | ✅ Have | Built, audited and reviewed; the bank's password page after Pay (HDFC's corporate cards) is typed from the sheet's password column; not yet exercised on a live order — the first card run is one account with the browser visible, started by the owner with their card sheet (26 Oct 2026) |
| Payment: Cash on Delivery | ✅ Have (proven live 24–26 Oct 2026) | Three real orders placed by the tool; Flipkart's seller policy cancelled each, and the second account is offered no COD for this phone |
| Payment: COD+Card advance, RTGS, IMPS | ⏸️ TBD — parked, process undefined | |
| **Mock / dry-run mode** | ❌ | They have Real / Mock / AI |
| Total quantity · qty per order · attempts | Partial (24 Oct 2026) | Units per order are honoured where the product allows and reported where Flipkart caps them (phones: one per order); one order per account per run; no total-quantity or attempts counter |
| Cart Amount Limit | ❌ | Separate from final amount |
| Max delivery days | ❌ | Reject slow delivery promises |
| Seller selection | ❌ | They pin a seller per run |
| Platform unit limits | ✅ Have (Phase 9, Unit limits page) | Windows per brand, model or category, e.g. "vivo 16 / 31 days" |
| Coupon codes at order time | ❌ | We apply coupons as a separate step only |
| SuperCoins / wallet / gift vouchers | ❌ | |
| Ref code, Flipkart Minutes, GST-mandatory | ❌ | |
| Product search (vs pasting a URL) | ❌ | |

### Accounts

| Capability | Us | Notes |
|---|---|---|
| Import / export, sessions, coupons | ✅ Have | |
| Paging + server-side filters | ✅ Have (Phase 7) | One page at a time with a total; filter, sort and search on the server; the account picker sends a filter, not 37,000 ids — §0 is closed |
| Custom tags (+ bulk tagging) | ✅ Have (Phase 7) | One tag per account; applied to a selection or to everything matching a filter |
| Subscription type (Plus/VIP tiers) | ✅ Have (Phase 7) | A filter on the list and on the run pages |
| Login status breakdown | ✅ Have (Phase 7) | not-registered, blocked, multiple-attempts and logged-out beside valid/invalid |
| Parent / child accounts | ❌ | |
| Per-account GST + phone pool | ❌ | |
| Multi-platform (Amazon, Samsung) | ⬜ | Descoped — Flipkart only |

### After the order

| Capability | Us | Notes |
|---|---|---|
| Record of what we placed | ✅ Have | |
| **Sync status back from the platform** | ✅ Have (24–25 Oct 2026) | Status, delivery date, OTP, seller, invoice link, delivery address per unit; a tracking id, an invoice link and an address on a live order that was not cancelled are still to be seen on an account the tool holds a session for (a cancelled order's page shows none of them) |
| Invoices (fetch, compile, extract) | ✅ Have (24–26 Oct 2026) | Fetched during the read-back or uploaded, loose or as Flipkart's per-order zip; Compile Invoices zips; Extract Invoices Data reads the PDFs into a CSV — four document kinds read on the owner's 35 real PDFs (product, seller fees, Flipkart Internet fees, Flipkart India transport charges) |
| Reschedule / cancel order units | ✅ Have (25 Oct 2026) | A request log for a person to action on Flipkart by hand (spec C-4); nothing is sent to the platform |
| Reviews | ❌ | |
| Saved views + column chooser | ✅ Have (24 Oct 2026) | Per user |

### Configuration

| Capability | Us | Notes |
|---|---|---|
| Business details (one set) | ⤳ Superseded | Replaced by the Address Book; the Settings block was removed on 24 Oct 2026 — there is no config fallback |
| **Multiple addresses, each linked to a GST** | ✅ Have (Phase 8) | You clearly run several entities |
| Multiple GSTs | ✅ Have (Phase 8) | |
| Order phone numbers pool | ❌ | |
| Alerts / "needs attention" | ✅ Have (Phase 11, extended 25–26 Oct 2026) | Nine rules: over target, failed, unconfirmed, delivery overdue, session invalid, stale read-back, address mismatch, address change, and (the owner's addition of 26 Oct) GST mismatch — the GST the order page says the order was billed under against the one the run chose |

---

## 2. What I'd build, in order

Ordered by *value per unit of effort*, not by how the screenshots are laid out.

> **Mock / dry-run mode is out of scope** — handled separately by the user.

### Phase 7 — Account layer at scale: paging **+ segmentation** `M–L` ⬅ start here
One phase, because they are the same feature seen from two sides: paging needs
filters, and tags *are* the filters.

- Server-side paging, filtering and sorting (`?page=&size=&q=&status=&tag=&subscription=`)
  returning a total count, with the DB indexes those filters need.
- Custom tags: create/rename/delete, apply to one account or to a selection,
  and **apply by filter** rather than by a list of ids.
- Subscription type and a richer login status (blocked / not-registered /
  multiple-attempts, not just valid / invalid).
- Account picker rebuilt as search-and-select, where "select all" sends a
  **filter**, not 37,000 ids — and the run pages pass that filter to the job.

**Why first:** without it nothing else can be used against real data, and
without tags "all accounts" is not a real selection at your scale.

### Phase 8 — Addresses & GSTs `M`
Promote the single business block to managed lists: Address (label, name, phone,
line, floor, city, state, pincode) ⟷ GST, linked, with one selected per run.
**Why:** you run several entities; today every order ships to one hard-set
address. This is a correctness issue, not a convenience.

### Phase 9 — Order quantity model & limits `M–L`
Total quantity, qty per order, attempts, cart-amount limit, max delivery days,
seller selection, and **platform unit limits** (per model/brand/category windows)
enforced before a run starts.
**Why:** this is the biggest behavioural gap. We place one order per account;
you order *N units* across accounts under per-model caps. It changes the job
model, so it needs its own phase.

### Phase 10 — Post-order sync `L`
Pull status back from Flipkart: delivered state, tracking id, actual delivery
date, delivery OTP, unit id. Then invoices, cancel/reschedule, reviews.
**Why later:** high value but large, and it depends on a stable order record.

### Phase 11 — Alerts `S–M`
Rules over synced data (address mismatch, delivery overdue) with an open/resolved
queue, like your "Needs attention — 443 open".

### Phase 12 — Remaining payment modes `S` each — **TBD (parked)**
**Parked at the user's request.** COD + Card (advance), RTGS and IMPS are not
Flipkart checkout options, and what they mean in this workflow is undefined —
a bank transfer to the seller, an internal reconciliation step, or something
else. Building from a guess here would move real money the wrong way, so it
waits for a description of the actual process.
COD + Card (advance), RTGS, IMPS. Each needs its own live selectors.

### Phase 13 — Multi-platform `XL` — **DROPPED**
Amazon / Samsung alongside Flipkart. **Descoped at the user's request: Flipkart
only.** The `selectors.py` + `automation/` split already anticipates a second
platform (one selector set each), so this can be picked up later without
rework — it is simply not worth its size today.

---

## 3. Two things worth deciding early

**Is this replacing the existing portal, or sitting beside it?**
If it's a replacement, Phases 7–10 are all mandatory and the honest order of
magnitude is months, not weeks — the reference tool is a mature product. If it's
a focused companion for one workflow, we can stop after Phase 8 and keep it
sharp.

**Where does the account data come from?**
If the 37k accounts live in the existing portal, importing by Excel is a poor
long-term answer. A one-way sync (or a direct DB/API read) would beat re-exporting
spreadsheets.

---

## 4. Suggested first slice

**Phase 7 on its own.** It is the only thing standing between this tool and your
real data, and it carries its own payoff: once accounts can be paged, filtered
and tagged, every run page immediately gets a usable way to choose who orders.

**Phase 8 (addresses & GSTs) next**, because until it lands every order ships to
one hard-coded address — which is wrong for a business running several entities,
and wrong quietly rather than loudly.
