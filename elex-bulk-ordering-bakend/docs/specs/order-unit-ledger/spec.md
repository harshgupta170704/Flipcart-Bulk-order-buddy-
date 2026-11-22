# Feature Specification: Order-unit ledger, invoices, and organization settings

**Feature Branch**: `order-unit-ledger`

**Created**: 2026-10-24

**Status**: Draft

**Input**: Video `demo.mp4` (38:43), processed 2026-10-24. Narration from an existing transcript of the recording (Whisper large-v3-turbo, English pass).

## How to read this *(answer first)*

Source: a working-app recording. It is a recorded screen share in which the presenter drives a live web app with a real cursor, types values, waits on loading states, and opens the downloaded files in Excel. What each screen looks like, and what every frame showed, is in [ui-evidence.md](ui-evidence.md).

**The one idea**: A trading business that buys stock on online marketplaces gets most of its day-to-day value from one ledger of every unit it bought, with GST invoices ready for filing and alerts when a unit ships to the wrong place, and that ledger can be kept from the organization's own order reports alone.

**What this spec covers**: Six stories taken from the recording's post-order screens, for one release: the Order Units History ledger with filters and CSV export (P1), invoice data extraction (P2), invoice compilation into downloadable archives (P3), the Dashboard's deliveries and order-unit alerts (P4), organization addresses and GSTs (P5), and sign-in with organization switching (P6). Order units enter the ledger from order reports and invoice PDFs that the organization downloads from each platform and uploads (FR-021 to FR-023).

**What it deliberately leaves out**: Placing orders, and every screen, setting, and field that exists only for placing orders, are out of scope. So is any software that signs in to or acts on a platform: order data arrives only through reports and invoice PDFs that the organization uploads (decision C-1). Screens the recording never opened, such as the full alerts list and the CSV Downloads tab, are also out of scope.

**The spec-driven caveat**: This document specifies behavior and contracts, not implementation. It is meant to be regenerated when requirements change, not patched by hand. Full spec-driven development is a poor fit for small, exploratory, or throwaway changes; if this feature is one of those, say so here and use iterative prompting instead.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Find, filter, and export order units (Priority: P1)

An operator opens Order Units History, narrows the organization's order units by date range, platform, status, product, and identifiers, reads each unit's order, delivery, billing, and payment details in one table, and downloads the matching units as a CSV for reconciliation.

**Why this priority**: The ledger is the record every other story reads from: invoices are extracted for its units, the Dashboard counts its undelivered units, and alerts point at its rows. The presenter spent the closing part of the demo here (video 32:56 to 38:42).

**Independent Test**: Load a fixture of order units for one organization, filter to one platform, one status, and a date range, and download the CSV. The on-screen count and the CSV rows must both match the fixture's matching units, which gives a usable reconciliation file without any other story.

**Acceptance Scenarios**:

1. **Given** an organization with more than 100 order units, **When** the operator opens Order Units History, **Then** the list shows the first 100 units, newest order date first, under the line "Showing 100 of <total> order units" (video 33:05).
2. **Given** the ledger is open, **When** the operator sets Order Date to Sep 01, 2026 to Sep 30, 2026, picks one platform, sets Order Status to Delivered, and enters a product keyword, then applies the filters, **Then** only matching units remain and the line reads "Showing 100 of 140 order units" (video 34:12).
3. **Given** the operator is choosing statuses, **When** the Order Status picker opens, **Then** the statuses appear in three groups, Confirmed, Cancelled, and Others, each with its own checkbox (video 34:12).
4. **Given** filters for one platform, Last 30 days, and Out for delivery that match 332 units, **When** the operator selects Download CSV, **Then** the message "CSV downloaded. Your export has been downloaded." appears and the file `order_units_2026-10-21_1516.csv` opens in Excel with the units' rows (video 35:05 to 35:42).
5. **Given** a Delivered unit whose invoice PDF is on file and an Approved unit whose invoice is not, **When** the operator looks at their rows, **Then** the Delivered row's Invoice action is available and the Approved row's is greyed out (video 34:12 and 33:05, approximate read).

---

### User Story 2 - Extract invoice data for filing (Priority: P2)

The operator narrows the ledger to a period, chooses which invoice types to include, and downloads one CSV holding each invoice's number, dates, product, IMEI, HSN, taxable value, taxes, total, and buyer and seller GSTINs, ready for the accountant.

**Why this priority**: It is the most direct money value after the ledger itself: the presenter called the result ready for filing (video 37:55 to 38:07), and GST input credit depends on it.

**Independent Test**: With fixture units whose invoice PDFs are present, select the Product type and download. The CSV must contain the invoice lines of that type for the filtered units, with the tax split, and nothing of the unselected types.

**Acceptance Scenarios**:

1. **Given** the ledger filtered to Oct 17, 2026 to Oct 21, 2026 and one platform, showing 182 units, **When** the operator selects Extract Invoices Data, **Then** a dialog lists the invoice types (Product, four fee-invoice types, and a sixth that is partly hidden) and states "Selected 1 invoice type" with Product ticked (video 37:32).
2. **Given** at least one type is ticked, **When** the operator selects Download CSV, **Then** the file `invoice_data_2026-10-21_1519.csv` downloads with columns from Invoice Type to Seller GST, holding product and fee invoice rows (video 37:39 to 38:10).

---

### User Story 3 - Compile invoice PDFs into an archive (Priority: P3)

The operator compiles the invoice PDFs of the filtered units into one named zip, keeps working while it builds, and later downloads it, or any earlier archive, from Compiled Invoices.

**Why this priority**: Auditors and the accountant need the PDFs themselves, not only the extracted data. The presenter demonstrated it right after the extraction (video 38:13 to 38:42).

**Independent Test**: Start a compilation for a fixture selection, check that the start message names the order count, and download the resulting zip. It must hold one PDF per invoice in the selection.

**Acceptance Scenarios**:

1. **Given** 182 filtered units, **When** the operator selects Compile Invoices, enters a name prefix, and starts the compilation, **Then** the message "Invoice compilation started. Compiling invoices for 182 orders. This may take a few minutes." appears and the button reads "Compiling Invoices..." beside a Cancel action (video 38:25 to 38:32).
2. **Given** the compilation has finished, **When** the operator opens Compiled Invoices, **Then** Compilation Status reads "No compilation in progress" and the list shows the new archive, "<prefix>.zip" (69.54 MB, last modified Oct 21, 2026 15:20), above the earlier archives, each with Download (video 38:42).

---

### User Story 4 - Watch deliveries and order-unit alerts on the Dashboard (Priority: P4)

The operator opens the Dashboard to see how many order units are still to be delivered, broken down by product model, which of them are overdue, and which order units need attention because of address problems.

**Why this priority**: It turns the ledger into a daily worklist, but it only summarizes data the P1 ledger already holds.

**Independent Test**: Seed undelivered units across several models and two kinds of address alert. Every Dashboard count must equal the seeded counts.

**Acceptance Scenarios**:

1. **Given** undelivered order units across several product models, **When** the operator opens the Dashboard, **Then** "To be delivered" shows "<n> order units across <m> models", the per-model counts in descending order, and "View all <m> models" (video 13:52).
2. **Given** active alerts, **When** the Dashboard loads, **Then** "Needs attention" shows the count per alert type, including "Order unit address mismatch" and "Order unit address change", the active and resolved totals, the latest alerts with their age, and "Open all alerts" (video 13:52).
3. **Given** a Dashboard panel is still fetching, **When** the page first renders, **Then** that panel shows placeholders instead of figures (video 13:52).

---

### User Story 5 - Keep the organization's addresses and GSTs (Priority: P5)

An administrator records the organization's delivery addresses and GST registrations once, links each address to its GSTs, and edits or removes them later. The address alerts are checked against these records.

**Why this priority**: The address alerts in P4 depend on these records, and the presenter set them up first during onboarding (video 05:26 to 05:45). The ledger and invoices still work without them.

**Independent Test**: Add an address linked to a new GST, edit it, and delete it. The lists must reflect each change and show the link as "<GST label> - <GST number>".

**Acceptance Scenarios**:

1. **Given** an organization with saved addresses, **When** the operator opens Organization Addresses, **Then** each address shows its Label with business name, Name, Phone, Address, Floor, City, State, Pincode, Linked GSTs (each shown as "<GST label> - <GST number>"), Created, and edit and delete actions, under an Add Address action (video 05:35).
2. **Given** an organization with saved GSTs, **When** the operator opens Organization GSTs, **Then** each GST shows Label, GST Number, Business Name, Created, and edit and delete actions, under an Add GST action (video 05:48).

---

### User Story 6 - Sign in and switch organization (Priority: P6)

A user signs in with email and password and works inside one organization at a time, switching organizations from the header.

**Why this priority**: Every other story needs it, but it is standard and carries no product-specific behavior beyond scoping data to the organization.

**Independent Test**: Sign in as a user who belongs to two organizations, switch between them, and confirm that each organization's lists show only its own records.

**Acceptance Scenarios**:

1. **Given** a registered user on the Sign-in screen reached from a link to the Orders page, **When** they enter their email and password and select Sign in, **Then** the app opens that page inside their current organization (video 01:56 to 03:13).
2. **Given** a user working in an organization whose lists are empty, **When** they switch the header's organization selector to a second organization, **Then** the screens show that organization's records (video 03:13, 04:31).

---

### Edge Cases

- What happens when the organization has no order units, or the filters match none?
- What happens when a unit's invoice PDF has not been uploaded yet?
- What happens when the operator unticks every type in Extract Invoices Data?
- What happens when some units in the selection have no invoice PDF on file?
- What happens when the operator starts a compilation while one is running?
- What happens when the operator cancels a running compilation?
- What happens when a compilation fails?
- How are long identifiers kept intact when an export is opened in Excel? The recorded export shows Unit ID as 4.39E+20 and Billing Phone as 8.08E+09 (video 36:23).
- What happens when the same unit arrives twice in uploaded reports?
- What happens when an uploaded report has rows the system cannot read?
- What happens when a unit's delivery address matches none of the organization's addresses?
- What happens when a unit's delivery address changes after it was ordered?
- What happens when an administrator deletes a GST that an address links to?
- What happens when a GST number is not a valid GSTIN?
- What happens when a platform reports a status outside the known list, such as "Order received" (video 33:05)?
- What happens while a Dashboard panel is loading?
- What happens when the email or password is wrong?

Every edge case above MUST also appear as a row below, with the requirement that governs it. An edge case with no requirement id is an unrecorded decision.

| Situation | Required behavior | Requirement |
|-----------|-------------------|-------------|
| No order units, or filters match none | Show "Showing 0 of 0 order units" with the column headers; disable Download CSV, Extract Invoices Data, and Compile Invoices | FR-018 |
| A unit's invoice PDF has not been uploaded | That row's Invoice action is greyed out until the PDF is uploaded | FR-014 |
| No invoice type ticked | Download CSV in Extract Invoices Data is disabled | FR-029 |
| Some selected units have no invoice PDF on file | Produce the CSV for the others and state how many units were left out | FR-030 |
| A compilation is already running | The button reads "Compiling Invoices..." and is disabled; a second start is refused | FR-034, FR-037 |
| The operator cancels a running compilation | Stop it and add no archive | FR-035 |
| A compilation fails | Show the failure under Compilation Status; earlier archives stay unchanged | FR-036 |
| An export is opened in Excel | Every digit of long identifiers survives | NFR-008 |
| The same unit arrives twice | Update the stored unit; never add a second | FR-024 |
| An uploaded report has unreadable rows | Import the readable rows and list each rejected row with its reason | FR-060 |
| Delivery address matches no organization address | Raise an "Order unit address mismatch" alert | FR-044 |
| Delivery address changes after ordering | Raise an "Order unit address change" alert | FR-045 |
| A linked GST is deleted | Delete it and unlink its addresses, saying how many (decided 2026-10-25) | FR-054 |
| A GST number is malformed | Reject it and name the GST Number field | FR-053 |
| A platform reports an unlisted status | Show the status as reported and offer it in that platform's status filter | FR-009 |
| A Dashboard panel is loading | Show placeholders instead of figures | FR-049 |
| Email or password is wrong | Refuse sign-in without saying which of the two was wrong | FR-056 |

## Requirements *(mandatory)*

### Functional Requirements

*Order Units History (User Story 1)*

- **FR-001**: The system shall keep one Order Unit per unit bought, identified by its platform Unit ID, linked to its platform Order ID, and holding the in-scope fields listed for Order Unit in ui-evidence.md (video 33:05, 35:42 to 36:27). Source: Observed
- **FR-002**: WHEN a user opens Order Units History, the system shall list the active organization's order units with the newest Order Date first (video 33:05). Source: Observed
- **FR-003**: The system shall show order units 100 per page under the line "Showing <shown> of <total> order units" (video 33:05, 34:12, 35:05). Source: Observed
- **FR-004**: The system shall let the user filter order units by Platform, Order ID (several values), Platform Email (several values), Order Status (several values), Product Name (text match), Card Number, Tracking ID (several values), and a date range on a chosen date field, with presets such as Last 7 days and Last 30 days and custom start and end dates (video 33:05, 34:12, 35:05). Source: Observed. [DECIDED 2026-10-25: the card column and filter hold the full card number the order was paid with, not a masked form — the owner's choice, so the ledger can be matched against card statements; the expiry and CVV are never stored.]
- **FR-005**: WHEN the user selects Apply Filters, the system shall show only the order units matching every set filter and update the count (video 34:12). Source: Observed
- **FR-006**: WHEN the user selects Reset Filters, the system shall clear every filter and show the unfiltered list. Source: Inferred (button seen at 34:12; never used)
- **FR-007**: The system shall group the Order Status choices into Confirmed, Cancelled, and Others (video 34:12). Source: Observed
- **FR-008**: WHEN the user ticks a status group, the system shall select every status in that group. Source: Inferred (group checkboxes seen at 34:12; never used)
- **FR-009**: The system shall show each unit's status as its platform reports it and offer, in the status filter, the statuses known for the selected platform (video 33:05, 34:12). Source: Inferred (rows from a second platform show "Order received", which is not in the first platform's list at 34:12)
- **FR-010**: The system shall let the user choose which table columns are visible (video 33:05). Source: Inferred (Columns control seen; never opened)
- **FR-011**: WHEN the user selects Quick Save, the system shall save the current columns and filters as that user's view. Source: Inferred (control seen at 33:05; never used)
- **FR-012**: WHEN the user selects Default View, the system shall restore the default columns and clear saved filters. Source: Inferred (control seen at 33:05; never used)
- **FR-013**: WHEN the user selects View on an order unit, the system shall show every stored field of that unit. Source: Inferred (View action seen at 33:05; never opened)
- **FR-014**: WHERE an order unit's invoice PDF is on file, the system shall enable that row's Invoice action to download the PDF (video 33:05, 34:12). Source: Observed (approximate read of enabled and greyed actions)
- **FR-015**: WHEN the user selects Download CSV, the system shall download every order unit matching the current filters, across all pages, as `order_units_<YYYY-MM-DD>_<HHMM>.csv`, with no size limit [DECIDED: C-9] (video 35:30, 35:42). Source: Inferred (file name seen at 35:42; that the file holds all 332 matches and not only the 100 on screen cannot be checked from the frames)
- **FR-016**: WHEN a CSV download completes, the system shall confirm with "CSV downloaded. Your export has been downloaded." (video 35:30). Source: Observed
- **FR-017**: The system shall write the order-units CSV with the in-scope Order Unit columns, dates as DD-MM-YYYY, and times as HH:MM:SS (video 36:23 to 36:27). Source: Observed
- **FR-018**: IF the organization has no order units or the filters match none, THEN the system shall show "Showing 0 of 0 order units" with the column headers and disable Download CSV, Extract Invoices Data, and Compile Invoices. Source: Inferred (an empty list in an organization with no records keeps its headers, video 03:13)
- **FR-019**: WHERE an order unit [NEEDS CLARIFICATION: C-3], the system shall tick its Internal Order column and include it when the Internal Orders filter is on (video 33:05). Source: Inferred (column and toggle seen; their meaning in this spec must be decided)
- **FR-020**: WHEN the user selects a row's Cancel action, Cancel Order Units, or Reschedule Order Units, the system shall record a request on each selected unit — the kind, a reason (required for a cancel), the wanted new delivery date (for a reschedule), who asked and when — and keep its status (open, done, dropped) for a person to action on the platform by hand [DECIDED: C-4] (video 33:05, 35:05). Source: Inferred (actions seen, Cancel only on undelivered rows; never used)

*Bringing order units in*

- **FR-021**: WHEN the user uploads an order report downloaded from a platform, the system shall add the order units it contains to the ledger [DECIDED: C-1] (video 33:05). Source: Inferred (the recording shows a Fetch action at this point; decision C-1 makes uploads the only way data arrives)
- **FR-022**: WHEN the user uploads a later order report from a platform, the system shall update the status, tracking, delivery dates, refunds, and cancellations of the order units it contains [DECIDED: C-1] (video 33:05). Source: Inferred (the recording shows an Update action at this point; decision C-1)
- **FR-023**: WHEN the user uploads invoice PDFs downloaded from a platform, the system shall attach each invoice to its order units by Order ID and mark those units as having an invoice on file [DECIDED: C-1] (video 33:05, 36:27). Source: Inferred (the Extract Invoices Data dialog says its data comes "from downloaded invoice PDFs", video 37:32; decision C-1)
- **FR-024**: IF an uploaded report holds an order unit with the same platform and Unit ID as a stored one, THEN the system shall update the stored unit instead of adding a second (video 33:05, 36:23). Source: Inferred (Unit ID is the per-unit key in the table and the export)
- **FR-025**: The system shall support the platforms [NEEDS CLARIFICATION: C-2] (units from two platforms seen at 33:05). Source: Observed
- **FR-060**: IF an uploaded report has rows the system cannot read, THEN the system shall import the readable rows and list each rejected row with its reason (video 33:05). Source: Inferred (follows from decision C-1; the recording shows no upload step)
- **FR-061**: The system shall accept order reports as CSV or XLSX files and invoices as PDF files (video 35:42, 37:32). Source: Inferred (decision C-1; the recording's own exports are CSV files and its invoice data comes from invoice PDFs)

*Invoices (User Stories 2 and 3)*

- **FR-026**: WHEN the user selects Extract Invoices Data, the system shall open a dialog listing the invoice types [NEEDS CLARIFICATION: C-10] with a count of the types ticked (video 37:32). Source: Observed
- **FR-027**: WHEN the user selects Download CSV in that dialog, the system shall download `invoice_data_<YYYY-MM-DD>_<HHMM>.csv` with one row per invoice line of the ticked types for the order units matching the current filters (video 37:50, 38:10). Source: Observed (file name and rows; one row per invoice line is read from the rows at 38:10)
- **FR-028**: The system shall write these invoice-data columns: Invoice Type, Invoice Number, Order ID, Invoice Date, Order Date, Original Invoice, Reason, IMEI, Invoice Product, Ledger Product, Qty, Platform Product Code, HSN, Taxable Amount, IGST, SGST, CGST, Total Amount, Buyer Name, Buyer GST, Seller Name, Seller GST (video 38:10). Source: Observed (several headers are truncated in the frame and two are renamed here; see ui-evidence.md)
- **FR-029**: IF no invoice type is ticked, THEN the system shall disable Download CSV in the Extract Invoices Data dialog (video 37:32). Source: Inferred (the dialog counts ticked types; the none-ticked state was not shown)
- **FR-030**: IF some matching order units have no invoice PDF on file, THEN the system shall still produce the invoice-data CSV for the others and state how many units were left out (video 36:27). Source: Inferred (the order-units export carries an invoice-on-file column, so the system knows which units lack one)
- **FR-031**: WHEN the user selects Compile Invoices, the system shall ask for an Invoice Name Prefix and offer Start Compilation and Cancel (video 38:25). Source: Observed (the dialog is read through a fade; approximate)
- **FR-032**: WHEN the user selects Start Compilation, the system shall build one zip of the invoice PDFs of the order units matching the current filters, named with the given prefix (video 38:32, 38:42). Source: Observed
- **FR-033**: WHEN a compilation starts, the system shall announce "Invoice compilation started. Compiling invoices for <n> orders. This may take a few minutes." (video 38:32). Source: Observed
- **FR-034**: WHILE a compilation is running, the system shall show the Compile Invoices button as "Compiling Invoices..." and disabled, with a Cancel action beside it (video 38:32). Source: Observed
- **FR-035**: WHEN the user selects Cancel during a compilation, the system shall stop it and add no archive. Source: Inferred (Cancel seen at 38:32; never used)
- **FR-036**: IF a compilation fails, THEN the system shall show the failure under Compilation Status and leave earlier archives unchanged (video 38:42). Source: Inferred (only the idle status was shown)
- **FR-037**: IF a compilation is already running in the organization, THEN the system shall refuse to start another and point the user to Compilation Status. Source: Inferred (one Compilation Status panel per organization, video 38:42)
- **FR-038**: WHEN the user selects Download Compiled Invoices, the system shall open Compiled Invoices showing the Compilation Status ("No compilation in progress" when idle) and earlier archives with name, size, and last-modified time, newest first, each with Download (video 38:42). Source: Observed
- **FR-039**: The system shall keep compiled archives for [NEEDS CLARIFICATION: C-11] (archives a week old are listed at 38:42). Source: Inferred

*Dashboard (User Story 4)*

- **FR-040**: The system shall show, under "To be delivered" on the Dashboard, the number of undelivered order units, the number of product models they span, and the per-model counts in descending order (video 13:52). Source: Observed
- **FR-041**: WHERE "Ordered 15+ days ago" is switched on, the system shall count only undelivered order units ordered more than 15 days before today. Source: Inferred (toggle seen at 13:52; never used)
- **FR-042**: WHEN the user selects "View all <n> models", the system shall list every model with its undelivered count. Source: Inferred (seen at 13:52; never opened)
- **FR-043**: The system shall show, under "Needs attention" on the Dashboard, the count of active alerts per type, the totals of active and resolved alerts, the three latest alerts with type and age, and "Open all alerts" (video 13:52). Source: Observed
- **FR-044**: WHEN an order unit is stored or updated with a delivery address that matches none of the organization's addresses under the rule set by [NEEDS CLARIFICATION: C-5], the system shall raise an "Order unit address mismatch" alert for it (alert type seen at 13:52). Source: Inferred
- **FR-045**: WHEN an uploaded report changes an order unit's delivery address, the system shall raise an "Order unit address change" alert for it (alert type seen at 13:52). Source: Inferred
- **FR-046**: WHEN the condition behind an alert no longer holds, the system shall [NEEDS CLARIFICATION: C-6] (resolved alerts are counted at 13:52). Source: Inferred
- **FR-047**: WHERE a platform is chosen in the Dashboard's platform filter, the system shall limit the "To be delivered" and "Needs attention" figures to that platform. Source: Inferred (in the recording the filter sits on a different row of the Dashboard, video 13:52, so its reach is unconfirmed)
- **FR-048**: WHEN the user selects Refresh on the Dashboard, the system shall reload every panel (video 13:52). Source: Inferred (button seen; never used)
- **FR-049**: WHILE a Dashboard panel is loading, the system shall show placeholders in place of its figures (video 13:52). Source: Observed

*Organization addresses and GSTs (User Story 5)*

- **FR-050**: The system shall let an administrator add, edit, and delete Organization Addresses with Label, Name, Phone, Address, Floor, City, State, Pincode, and Linked GSTs (video 05:35). Source: Observed (fields, Add Address, and the edit and delete icons; the dialogs were never opened)
- **FR-051**: The system shall let an administrator add, edit, and delete Organization GSTs with Label, GST Number, and Business Name (video 05:48). Source: Observed (fields, Add GST, and the edit and delete icons; the dialogs were never opened)
- **FR-052**: The system shall show each address's linked GSTs as "<GST label> - <GST number>", or "None" when it has none (video 05:35). Source: Observed
- **FR-053**: IF a GST Number is not a 15-character GSTIN, THEN the system shall reject it and name the GST Number field. Source: Inferred (every GST number shown has 15 characters, video 05:48)
- **FR-054**: IF an administrator deletes a GST that an address links to, THEN the system shall delete the GST and unlink those addresses, saying how many it unlinked [DECIDED 2026-10-25: the owner chose "only unlink" over the refusal the recording suggested] (video 05:35, 05:48). Source: Inferred (addresses show their linked GSTs; deletion was never used)

*Sign-in and organizations (User Story 6)*

- **FR-055**: WHEN a user submits a registered email and matching password on the Sign-in screen, the system shall open the page named in the sign-in link, inside the user's current organization (video 01:56 `/login?from=%2Forders`, 03:13 `/orders`; narrated 02:04). Source: Observed
- **FR-056**: IF the email or password is wrong, THEN the system shall refuse sign-in without saying which of the two was wrong (video 01:56). Source: Inferred (error state not shown)
- **FR-057**: WHERE a user belongs to more than one organization, the system shall let the user switch the active organization from the header selector (video 03:13, 04:31; narrated 03:54). Source: Observed
- **FR-058**: The system shall allow each user only the actions of their role, as set by [NEEDS CLARIFICATION: C-7] (user menu and Admin menu seen at 03:13; never opened). Source: Inferred
- **FR-059**: WHEN a user asks to reset a forgotten password, the system shall mail them a single-use link that works for an hour, and an admin may set a temporary password instead [DECIDED: C-13] (the Sign-in screen shows no reset link, video 01:56). Source: Inferred

### Key Entities *(include if feature involves data)*

- **Organization**: A tenant: one business using the app. It owns its users' memberships, addresses, GSTs, platform accounts, order units, alerts, and compiled archives.
- **User**: A person who signs in with email and password and belongs to one or more organizations, with a role in each ([NEEDS CLARIFICATION: C-7] in FR-058).
- **Organization Address**: A delivery address of the organization, linked to zero or more Organization GSTs, and the reference for the address alerts.
- **Organization GST**: A GST registration of the organization, with a label, GSTIN, and business name.
- **Platform Account**: One of the organization's own accounts on a platform, known by platform and email. Order units are bought through it. It holds no password or session (NFR-003).
- **Order Unit**: One unit of a product bought on a platform. It belongs to a platform order (Order ID) and a Platform Account and carries status, amounts, delivery and tracking details, billing and GST details, a masked card reference, seller, IMEI, refund and cancellation state, and whether its invoice PDF is on file and the goods received.
- **Invoice**: A platform-issued invoice for an order, of one type (a product invoice or one of several fee invoices), covering one or more order units of that order. Its lines feed the invoice-data CSV.
- **Compiled Invoice Archive**: A zip of invoice PDFs built for a filtered set of order units, named with a prefix, with size and last-modified time.
- **Compilation**: The background job that builds a Compiled Invoice Archive. At most one runs per organization (FR-037).
- **Alert**: A flagged problem on an order unit ("Order unit address mismatch" or "Order unit address change"), active or resolved, with the time it was raised.
- **Saved View**: A user's saved choice of visible columns and filters for Order Units History.

### Non-functional Requirements

- **NFR-001**: The system shall run in a current desktop web browser with nothing to install on the user's computer (narrated 01:48 to 02:04). Source: Observed
- **NFR-002**: The system shall never show or export a full payment card number; card references stay masked as in the recorded export (for example "53XXXXXX", video 36:25). Source: Observed. [REVISED 2026-10-25 at the owner's decision: the full number of the card an order was paid with is shown in the ledger and written to exports (FR-004); the expiry and the CVV are never stored or shown, and the deployment guide says how to keep the database and exports safe.]
- **NFR-003**: The system shall store no password or browser session for any platform account (video 33:05). Source: Inferred (under decision C-1 the organization uploads its own reports, so the system never signs in to a platform)
- **NFR-004**: WHILE an organization is active, the system shall return only that organization's records in every list, count, export, and archive. Source: Inferred (one organization shows an empty list at 03:13 while another shows its own records from 04:31)
- **NFR-005**: The system shall run invoice compilation in the background so the user can keep using the app until it finishes (video 38:32, "This may take a few minutes"). Source: Observed [MEASURED 2026-10-26: 1,024 one-megabyte PDFs compiled into one archive in 2 s and streamed back through the download route in 2.5 s, the process's memory flat at about 130 MB throughout — remaining-work §8]
- **NFR-006**: The system shall build and serve compiled archives of at least 1.08 GB (the largest archive listed, video 38:42). Source: Observed
- **NFR-007**: The system shall return a filtered page of Order Units History within [NEEDS CLARIFICATION: C-12] for an organization's full ledger (count line at 33:05). Source: Inferred (latency cannot be measured from the recording) [MEASURED 2026-10-24 on SQLite and 2026-10-26 on PostgreSQL with a year of units loaded — remaining-work §8; every typical filtered page is far inside the 2-second target]
- **NFR-008**: The system shall write long numeric identifiers (Unit ID, Order ID, IMEI, phone numbers, tracking IDs) into CSV exports so that spreadsheet software keeps every digit. Source: Inferred (the recorded export shows Unit ID as 4.39E+20 and Billing Phone as 8.08E+09 in Excel, video 36:23 to 36:27)
- **NFR-009**: IF an upload, extraction, compilation, or export fails, THEN the system shall tell the user what failed and leave stored data unchanged (video 35:30, 38:32). Source: Inferred (only success messages were shown)
- **NFR-010**: The system shall show money in rupees with the ₹ sign, Indian digit grouping, and two decimals, for example ₹89,447.00 (video 33:05). Source: Observed

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator gets from the unfiltered ledger to an opened invoice-data file for one period and one platform in under 1 minute (the recording shows about 40 seconds, 37:09 to 37:50, for 182 units). *MEASURED 26 Oct 2026 (server side, remaining-work §8): for a 200-unit filter the types list answers in 12 ms and the CSV's first byte arrives in 18 ms, the whole 800-row file in 168 ms; the minute is the operator's clicks, not the tool.*
- **SC-002**: For 100% of order units in a selection whose invoice PDFs are on file, the invoice-data file carries every line of the ticked types with taxable amount, the tax columns, total, and buyer and seller GSTINs.
- **SC-003**: An archive for 200 order units is listed for download within [NEEDS CLARIFICATION: C-8] of starting (the recording shows about 10 seconds for 182 units, 38:32 to 38:42). *MEASURED 26 Oct 2026 (remaining-work §8): the compile job zipped 200 orders' 600 real invoice PDFs (350 MiB) in 0.7 s against C-8's minute per 200 units.*
- **SC-004**: On a seeded data set, the Dashboard's "To be delivered" total equals the ledger's count of undelivered units under the same platform filter, and the active alert total equals the sum of the per-type counts, in 100% of checks (video 13:52).
- **SC-005**: After the next upload, 100% of seeded order units whose delivery address matches no organization address appear as "Order unit address mismatch" alerts, and no unit with a matching address does.
- **SC-006**: A scan of every screen and export finds 0 full card numbers and 0 platform passwords.
- **SC-007**: 100% of Unit IDs, IMEIs, and phone numbers in an order-units export are unchanged after the file is opened and saved again in common spreadsheet software.
- **SC-008**: For 100% of exports within the size limit set by [NEEDS CLARIFICATION: C-9], the exported file's row count equals the total on screen.

## Assumptions

- Times are India Standard Time. Screens show dates like "Oct 21, 2026" and 24-hour times like "13:55"; CSVs use DD-MM-YYYY and HH:MM:SS (video 32:54, 36:23).
- The interface is English-only and amounts are Indian rupees.
- Column choices and Quick Save views belong to the user who saved them, not the whole organization.
- An administrator creates organizations and users outside these stories; the Admin menu was never opened.
- The CSV Downloads tab lists a user's recent exports for download again; it was never opened, and its role depends on [NEEDS CLARIFICATION: C-9] in FR-015.
- The order-units export carries only the Order Unit columns listed in ui-evidence.md; the recorded export's other columns are left out.
- The invoice-data tax columns follow the platform invoice. In the recorded file the IGST column held SGST plus CGST on intra-state rows (75,422.88 taxable, 13,576.12 IGST, 6,788.06 each SGST and CGST, video 38:10), so it may be a total-GST column; the build confirms against a real invoice before fixing the header.
- The order-unit detail (View), "Open all alerts", "View all <n> models", and the add and edit dialogs for addresses and GSTs were never opened; their content here comes from the fields the lists show.
- Order units already exist, uploaded or seeded, when the Independent Tests run.
- The status list seen at 34:12 is the starting set; other platforms' statuses are taken as reported.

## Clarifications

| Id | Decision | Options | Recommended default | Lands in |
|----|----------|---------|---------------------|----------|
| C-1 | DECIDED 2026-10-24: order units, status updates, and invoice PDFs arrive only through reports and PDFs the organization downloads from each platform and uploads; no software signs in to or acts on a platform. | Report upload | Report upload, chosen by the owner | FR-021, FR-022, FR-023, FR-060, FR-061, NFR-003 |
| C-2 | Which platforms are in v1? | One platform · The two platforms seen in the recording · Those two and a third | The two platforms seen in the recording: both have units in the recorded ledger (video 33:05) | FR-025 |
| C-3 | What does the "Internal Order" flag (a yes or no column seen in the recording) mean? | Drop the column and filter · Tick units matched to an approved purchase request in the owner's existing procurement build · Keep as a yes or no column from the uploaded reports | Tick units matched to an approved purchase request, so the ledger shows which buys were planned and approved | FR-019 |
| C-4 | DECIDED 2026-10-25: Record a request with a reason and status log for a person to action on the platform. | Record a request with a reason and status log | Record a request and status log for a person to action, chosen by the owner | FR-020 |
| C-5 | BUILT 2026-10-25 on the recommended option: same pincode, and the Address Book entry's first line found (normalised) inside the address the platform's order page shows. | Exact text match · Same pincode and normalized first address line as any organization address · Pincode only | Same pincode and normalized first line: it catches wrong buildings without flagging spelling differences | FR-044 |
| C-6 | How are alerts resolved? | Automatically when a later upload shows the condition gone · Manually with a note · Both | Both: automatic when the data clears it, manual with a note when a person accepts it | FR-046 |
| C-7 | DECIDED 2026-10-25: Admin and Operator, as built (the owner: "aligned"); operators place orders and run read-backs but change no setup. | One role for everyone · Admin (users, addresses, GSTs) and Operator (ledger, exports, invoices) · Admin, Operator, and read-only Viewer | Admin and Operator; add Viewer only if an accountant needs read-only access | FR-058 |
| C-8 | What is the target time to compile invoices? | 1 minute per 200 units · 5 minutes per 200 units · No target | 1 minute per 200 units: the recording finished 182 units in about 10 seconds | SC-003 |
| C-9 | DECIDED 2026-10-25: no limit, always direct (the owner: large exports "to be allowed"); the server streams the rows, so nothing is held in memory, and there is no CSV Downloads tab. | No limit, always direct · Direct up to 5,000 units, larger exports run in the background and land in CSV Downloads · Always in the background | Direct up to 5,000 units, background above that, so large exports never block the screen | FR-015, SC-008 |
| C-10 | Is the invoice-type list fixed, or built from the invoices present in the selection? | Fixed list per platform · Built from the invoice types found in the selection | Built from the selection, so new fee types appear without a code change; the recording shows five types and a sixth partly hidden (video 37:32) | FR-026 |
| C-11 | How long are compiled archives kept? | 30 days · 90 days · Until a user deletes them | 90 days, with a delete action; the recording lists archives a week old (video 38:42) | FR-039 |
| C-12 | What response time must a filtered ledger page meet? | 1 second · 2 seconds · 5 seconds, each at the 95th percentile | 2 seconds at the 95th percentile with a full year of the organization's order units loaded | NFR-007 |
| C-13 | DECIDED 2026-10-25: an email reset link and an 8-hour session (the owner: "okay with mail"); built, and live once the mail server's details are configured — until then an admin sets a temporary password under Users. | An admin resets passwords · Email reset link · Email reset link plus an idle timeout | Email reset link and an 8-hour session, matching the session length of the existing build | FR-059 |
