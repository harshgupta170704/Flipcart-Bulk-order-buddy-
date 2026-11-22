# UI Evidence: Order-unit ledger, invoices, and organization settings

**Source**: Video `demo.mp4` (38:43), processed 2026-10-24

**Spec**: [spec.md](spec.md)

**Demo type**: Working-app recording. It is a recorded screen share of a live web app, driven with a real cursor: typed values, loading placeholders, download messages, and the downloaded files opened in Excel.

Timestamps like (video MM:SS) reference the source recording. Every item is
Observed (seen in a frame or narrated) unless marked Inferred. Example values
that named a business, a person, or a platform are replaced with neutral
descriptions or placeholders such as <n>.

## Screens

### S1. Sign-in screen (video 01:56)

- **Purpose**: Sign in to the app.
- **Layout**: A top bar with the logo and a dark-mode toggle, and a sign-in card with the app's name, the line "Please sign in to your account", and the form. The rest of the page is marketing content and is not part of this spec.
- **Components**: Email input (placeholder "Enter your email"), Password input (placeholder "Enter your password"), black full-width "Sign in" button. There is no password-reset link.
- **States**: Empty form seen. An error state was not shown (Inferred in FR-056).
- **Navigation**: The sign-in link carries the page to return to (`/login?from=%2Forders`). After sign-in the app opened that page (video 03:13).
- **Visual notes**: Light theme, black primary button.
- **Related**: FR-055, FR-056, FR-059, User Story 6

### S2. Header (every signed-in screen) (video 03:13 onward)

- **Purpose**: Navigation, organization switching, and the user menu.
- **Layout**: One row. Left: logo. Middle: navigation items, each with an icon; the in-scope ones are Dashboard, Order Units History, and a settings menu that holds the Addresses and GSTs tabs. Right: dark-mode toggle (moon icon), organization selector, and a user menu showing the signed-in user's name.
- **Components**: Navigation links; the active item is underlined. The organization selector is a dropdown; the recording switches from an empty organization (03:13) to a second one (04:31). The user menu and the Admin dropdown were never opened.
- **States**: Only the populated state was seen.
- **Navigation**: In scope here are Dashboard, Order Units History, and the Addresses and GSTs tabs under the settings menu. The other items are out of scope.
- **Related**: FR-057, FR-058, User Story 6

### S3. Dashboard (video 13:52, 14:05)

- **Purpose**: Deliveries and order-unit alerts for this organization.
- **Layout**: Title "Dashboard" with a subtitle, and a Refresh button at the top right. Then a row of cards that is out of scope, with a platform filter ("All Platforms") at its right. Below it, two cards side by side: "Needs attention" on the left and "To be delivered" on the right.
- **Components**:
  - Needs attention (triangle icon): the line "<active> active · <resolved> resolved"; one row per alert type with its count and an orange bar (in scope: "Order unit address mismatch" and "Order unit address change"); "Latest alerts", three entries each with type, type subtitle, and age ("6 hours ago"); an "Open all alerts" button.
  - To be delivered (truck icon): the line "<n> order units across <m> models"; an "Ordered 15+ days ago" toggle (off); rows "<Brand> · <Model>" with count and teal bar, largest first; a "View all <m> models" button.
  - Refresh button.
- **States**: Loading: cards still fetching show grey placeholder bars while Needs attention and To be delivered are already filled (video 13:52 and 14:05).
- **Navigation**: Header link Dashboard. "Open all alerts" and "View all <m> models" lead to screens never shown.
- **Visual notes**: White cards with thin borders; orange bars for alerts, teal bars for deliveries.
- **Related**: FR-040 to FR-049, User Story 4

### S4. Order Units History (video 33:05, 34:12, 35:05, 35:30, 37:32, 38:32)

- **Purpose**: The ledger of order units, with filters, exports, and invoice actions.
- **Layout**, top to bottom:
  1. Tabs: "Order Units" (active) and "CSV Downloads".
  2. An import bar. In the recording it holds Fetch, Update, and Invoices actions, a refresh icon, and a ">" overflow; in this spec these become uploads of order reports, later order reports, and invoice PDFs (FR-021 to FR-023).
  3. A date row: a date-field dropdown ("Order Date"), a range dropdown showing a preset ("Last 7 days", "Last 30 days") or the chosen range ("Sep 01, 2026 - Sep 30, 2026"), "Start Date" and "End Date" pickers, and a "×" to clear the range (seen at 34:12).
  4. A toolbar: on the left, "Showing <shown> of <total> order units"; on the right, Default View, Quick Save, Columns, Download Compiled Invoices, Extract Invoices Data, Compile Invoices, Download CSV, Reschedule Order Units (blue), and Cancel Order Units (red).
  5. A Filters box with Reset Filters and Apply Filters (black) at the top right, then the inputs: Platform (dropdown), Order ID (Multi Value Allowed, placeholder showing two example order IDs separated by a space), Platform Email (Multi Value Allowed, placeholder "abc@gmail.com xyz@gmail.com"), Order Status ("Select status" or "1 statuses selected"), Product Name ("Filter by product name"), Card Number ("Filter by card number"), Tracking ID (Multi Value Allowed, placeholder showing two example tracking IDs), and an Internal Orders toggle labelled "All". Below them sits an "Advanced Filters" dropdown, never opened.
  6. The table, with columns Platform, Order Date (date over time, "Oct 21, 08:41"), Platform Email, Status, Product, Order ID, Internal Order (tick), PO Number, Unit Amount, Actual Delivery Date, Tracking ID, Business GST No, Billing Phone, an actions cell (View, Invoice with a download icon, and a red Cancel on undelivered rows), Unit ID, and Cash on Delivery. Empty cells show a dash.
- **Components**:
  - Order Status picker (open at 34:12): three groups, each with a checkbox and chips. Confirmed: Delivered, Out for delivery, Shipped, Return started, Approved, Purchased, Ready to ship, Dispatched. Cancelled: Cancelled, Cancellation requested, Cancelled - Order confirmed but not shipped, Cancelled - Order shipped, Cancelled - Received but not confirmed/shipped, Cancelled by user - Order confirmed but not shipped, Cancelled by user - Order shipped. Others: Order Pending, Returned, Refunded, Payment not received. A chosen chip turns black (Delivered).
  - Download CSV: shows a "CSV downloaded / Your export has been downloaded." message at the bottom right (video 35:30).
  - Compile Invoices: while compiling, it reads "Compiling Invoices..." with a spinner, greyed out, and a red "× Cancel" appears beside it (video 38:32). The message "Invoice compilation started / Compiling invoices for 182 orders. This may take a few minutes." shows at the bottom right.
- **States**:
  - Populated, with the count line updating as filters change: 140 at 34:12 (a one-month range, one platform, Delivered, and a product keyword), 332 at 35:05 (one platform, Last 30 days, Out for delivery), and 182 at 37:32 and 38:32 (Oct 17 to Oct 21, 2026, one platform, one status, rows Delivered). At 33:05 the range control shows "Last 7 days" with empty Start and End dates, so it is unclear whether that preset was applied.
  - Invoice action greyed on Approved rows (33:05) and dark on Delivered and Out for delivery rows (34:12, 35:05). Approximate read.
  - An empty state was not seen here (Inferred in FR-018 from an empty list at 03:13).
- **Navigation**: Header link Order Units History. The actions open S5, S6, and S7.
- **Visual notes**: Statuses are plain text, the Internal Order tick is green, Cancel actions are red, Reschedule is blue. The Internal Order column and Internal Orders filter carry a different label in the recording; this spec renames them (C-3).
- **Related**: FR-001 to FR-025, FR-026, FR-031, FR-034, FR-038, User Stories 1, 2, 3

### S5. Extract Invoices Data dialog (video 37:32)

- **Purpose**: Choose invoice types and download the invoice-data CSV.
- **Layout**: A modal over S4 with the title "Extract Invoices Data", a close "×", and the description "Select the invoice types to include in the extracted CSV. The CSV will contain invoice data extracted from downloaded invoice PDFs." Then the heading "Invoice Types" and a scrollable checkbox list, a count line, and the buttons.
- **Components**: Checkboxes for Product (ticked and highlighted) and four fee-invoice types, with a sixth type cut off by the scroll (approximate). The line "Selected 1 invoice type". Buttons Cancel and "Download CSV" (black, download icon).
- **States**: One type ticked. The state with none ticked was not seen (Inferred in FR-029).
- **Navigation**: Opened from Extract Invoices Data in S4. Download CSV produced X2.
- **Related**: FR-026 to FR-030, User Story 2

### S6. Compile Invoices dialog (video 38:25)

- **Purpose**: Name a compilation and start it.
- **Layout**: A modal over S4, captured mid-fade, so the text is approximate: the title "Compile Invoices", the text "Enter a name prefix for the compiled invoices. This name will be used to identify the compiled invoice file.", a field "Invoice Name Prefix" with an "e.g., ..." placeholder and a hint line below it (illegible), and the buttons Cancel and Start Compilation, plus a close "×".
- **Components**: Invoice Name Prefix text input; Cancel; Start Compilation.
- **States**: Only the empty form, mid-fade, was seen. The narration has the presenter typing a short prefix (video 38:19 to 38:26).
- **Navigation**: Opened from Compile Invoices in S4. On start it closes, and S4 shows the compiling state.
- **Related**: FR-031 to FR-033, FR-037, User Story 3

### S7. Compiled Invoices dialog (video 38:42)

- **Purpose**: See whether a compilation is running and download earlier archives.
- **Layout**: A modal with the title "Compiled Invoices", the subtitle "Download previously compiled invoice files.", and a close "×". Below it, a bordered panel "Compilation Status" reading "No compilation in progress", then a scrollable list of archive cards.
- **Components**: Each card shows the archive name, its size, and "Last modified: <date time>", with a "Download" button. Seen: the new archive at the top (69.54 MB, Oct 21, 2026 15:20), then earlier archives from 63.32 MB to 1.08 GB, last modified Oct 14 to Oct 16, 2026, the last one partly visible.
- **States**: Idle with archives. The running and failed states were not seen (Inferred in FR-036 and FR-038).
- **Navigation**: Most likely opened from Download Compiled Invoices in S4. The click itself was not captured.
- **Related**: FR-036, FR-038, FR-039, User Story 3

### S8. Organization Addresses (video 05:35)

- **Purpose**: "Manage addresses for the current organization" (subtitle).
- **Layout**: Reached through the settings menu, on a tab bar where only the Addresses (active) and GSTs tabs are in scope. Title "Organization Addresses" with the subtitle, and a black "+ Add Address" button on the right. Then a table.
- **Components**: Columns Label (the label, with a bordered business-name tag under it), Name, Phone (a dash when empty), Address (two lines), Floor, City, State, Pincode, Linked GSTs (bordered tags "<GST label> - <GST number>", or "None"), Created, and Actions (edit icon, red delete icon). A typical row: a label over a business-name tag, the business name, a two-line street address, a city, a state, a 6-digit pincode, one linked GST tag, and a created date.
- **States**: Populated. The add and edit dialogs were never opened.
- **Navigation**: The settings menu in the header, then the Addresses tab.
- **Related**: FR-050, FR-052, User Story 5

### S9. Organization GSTs (video 05:48)

- **Purpose**: "Manage GST details for the current organization" (subtitle).
- **Layout**: The same tab bar as S8, with GSTs active. Title "Organization GSTs" with the subtitle, and a black "+ Add GST" button on the right. Then a table.
- **Components**: Columns Label, GST Number (in a monospace grey tag), Business Name, Created, and Actions (edit icon, red delete icon). A typical row: a label, a 15-character GSTIN, the business name, and a created date.
- **States**: Populated. The add and edit dialogs were never opened.
- **Navigation**: The settings menu in the header, then the GSTs tab.
- **Related**: FR-051, FR-053, FR-054, User Story 5

### X1. Order-units CSV opened in Excel (external, video 35:42 to 36:27)

- **Purpose**: Evidence for the order-units export format. This is Excel, not the product.
- **Layout**: Workbook `order_units_2026-10-21_1516` with a single sheet of the same name. Columns A to AY were read across 35:42, 36:23, 36:25, and 36:27. The in-scope columns are in the Order Unit data model below.
- **States**: Excel displays Unit ID as 4.39E+20, Billing Phone as 8.08E+09, and dates as "########" (column width), which is the reason for NFR-008.
- **Related**: FR-015 to FR-017, NFR-002, NFR-008, User Story 1

### X2. Invoice-data CSV opened in Excel (external, video 37:50, 38:10)

- **Purpose**: Evidence for the invoice-data export format.
- **Layout**: Workbook `invoice_data_2026-10-21_1519`, blank while loading at 37:50 and populated at 38:10, with columns A to V. The full list is in the Invoice data model below.
- **Related**: FR-027, FR-028, User Story 2

### Out of scope

Other screens in the recording, and the parts of the screens above that are not described here, are out of scope and not specified.

## Flows

### F1. Filter and export the ledger (User Story 1)

1. (video 32:56) From the header, the user opens Order Units History (S4) → the first 100 units, newest first, with the total count (33:05).
2. (video 34:12) The user sets Order Date to Sep 01, 2026 to Sep 30, 2026, picks one platform, enters a product keyword, and picks Delivered in the grouped status picker, then applies → "Showing 100 of 140 order units".
3. (video 35:05) The user changes the filters to Last 30 days and Out for delivery → "Showing 100 of 332 order units".
4. (video 35:30) The user selects Download CSV → the message "CSV downloaded / Your export has been downloaded." appears, and the browser's download icon shows.
5. (video 35:42 to 36:27) `order_units_2026-10-21_1516.csv` opens in Excel (X1).

### F2. Extract invoice data (User Story 2)

1. (video 37:09 to 37:23) The user sets the range to Oct 17, 2026 to Oct 21, 2026 with one platform and one status → "Showing 100 of 182 order units".
2. (video 37:28 to 37:32) The user selects Extract Invoices Data → S5 opens with Product ticked and "Selected 1 invoice type".
3. (video 37:39 to 38:10) The user selects Download CSV → `invoice_data_2026-10-21_1519.csv` opens in Excel (X2). Its rows include product and fee invoice types, so more types were ticked after 37:32.

### F3. Compile invoices (User Story 3)

1. (video 38:13 to 38:25) The user selects Compile Invoices → S6 asks for an Invoice Name Prefix. The user enters a name prefix (narrated) and selects Start Compilation.
2. (video 38:32) S4 shows the message "Invoice compilation started / Compiling invoices for 182 orders. This may take a few minutes.", with "Compiling Invoices..." and "× Cancel" in the toolbar.
3. (video 38:42) S7 shows "No compilation in progress" and the new archive, named with that prefix (69.54 MB, Oct 21, 2026 15:20), at the top. The taskbar clock reads 15:20 at both 38:32 and 38:42.

### F4. Check deliveries and alerts (User Story 4)

1. (video 13:40 to 14:05) The user opens the Dashboard (S3) → some cards show placeholders while Needs attention and To be delivered are filled.

### F5. Review organization addresses and GSTs (User Story 5)

1. (video 04:31 to 05:15) The user opens the settings menu; its first tab is out of scope.
2. (video 05:26 to 05:35) The user opens the Addresses tab (S8), narrated "we will save all your addresses here".
3. (video 05:48) The user opens the GSTs tab (S9), narrated "you can save your GSTs here".
4. No address or GST was added or edited on camera.

### F6. Sign in and switch organization (User Story 6)

1. (video 01:56) The Sign-in screen (S1) at `/login?from=%2Forders`.
2. (video 02:08 to 02:18) The presenter signs in, narrated "For example, I am logging in."
3. (video 03:13) The app opens in an organization with no records.
4. (video 03:54 to 04:31) The presenter switches to a second organization, narrated "switch to another organization with consent", and the screens then show that organization's records.

## Data model (Inferred)

### Organization

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Name | string | S2 (video 03:13, 04:31): two organization names in the selector |

### User

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Name | string | S2 (video 03:13): the signed-in user's name in the user menu |
| Email | string | S1 (video 01:56): Email input |
| Password | secret, stored hashed | S1 (video 01:56): Password input |
| Role | enum, values per C-7 | S2 (video 03:13): Admin menu present |

### Organization Address

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Label | string | S8 (video 05:35): label text above a business-name tag |
| Name | string | S8: the business name, also shown as a tag under the label |
| Phone | string, 10 digits, optional | S8: a 10-digit number, or a dash |
| Address | string, two lines | S8: a street line and a locality line |
| Floor | string, optional | S8: values like "3RD FLOOR" and "First Floor", or a dash |
| City | string | S8: City column |
| State | string, Indian state | S8: State column |
| Pincode | string, 6 digits | S8: Pincode column |
| Linked GSTs | list of references to Organization GST | S8: tags "<GST label> - <GST number>", or "None" |
| Created | date | S8: "Oct 15, 2026" |

### Organization GST

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Label | string | S9 (video 05:48): Label column |
| GST Number | string, 15-character GSTIN | S9: 15-character values in a monospace tag |
| Business Name | string | S9: Business Name column |
| Created | date | S9: Created column |

### Platform Account

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Platform | enum, values per C-2 | S4 (video 33:05): two platform values in the Platform column |
| Email | string | S4: Platform Email column |

### Order Unit

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Platform | enum | S4 (video 33:05): Platform column; X1 column A |
| Order Date | date and time | S4: "Oct 21, 08:41"; X1 columns B and C: "15-10-2026", "14:19:46" |
| Platform Email | reference to Platform Account | S4; X1 column D |
| Status | enum per platform | S4 status picker (video 34:12), "Order received" on a second platform's rows (33:05); X1 column E: "Out for delivery" |
| Product | string | S4: product name with its variant (colour and storage) in brackets; X1 column F |
| Order ID | string | S4: the platform's order number, in a different format on each platform; X1 column G |
| Unit ID | string of up to 21 digits | S4 (approximate, small text); X1 column H shows 4.39E+20 |
| Internal Order | boolean, meaning per C-3 | S4: green tick; X1 column N: "Yes" |
| PO Number | string, optional | S4: filled on one platform's rows only |
| Unit Amount | money (INR) | S4: "₹89,447.00"; X1 column R: "11290" |
| Quantity | integer | X1 column Y: 1 |
| Total Order Amount | money | X1 column Z: "11290" (header truncated "Total Orde") |
| Estimated Delivery | date | X1 column S (header "Estimated"; values hidden by column width) |
| Actual Delivery Date | date | S4: "Oct 20, 2026"; X1 column T |
| Tracking ID | string | S4: the courier's tracking code, in a different format per courier |
| Last Tracking Update | text with date | X1 column W: "Mon Oct 1..." (truncated) |
| Shipment Partner | string | X1 column X: the courier's name (video 36:23) |
| Business Name | string | X1 column AF (truncated) |
| Business GST No | string, GSTIN | S4: Business GST No column; X1 column AG |
| Billing Name | string | X1 column AL (truncated) |
| Billing Phone | string, 10 digits | S4: Billing Phone column; X1 column AH shows 8.08E+09 |
| Billing Pincode | string, 6 digits | X1 column AM |
| Card Number | masked string | X1 column M: "53XXXXXX" (first two digits visible) |
| RTGS Source, RTGS Beneficiary | string, optional | X1 columns P and Q, empty in every row seen |
| Cash on Delivery | boolean | S4: "No"; X1 column AV: "No" |
| EWB Number | string, optional | X1 column AN, empty in rows seen |
| Invoice on file | boolean | X1 column AO: header truncated "Is Invoice D...", values "Yes" |
| Refund Status, Refund Amount, Refund Method | string, money, string, optional | X1 columns AP to AR, empty in rows seen |
| Cancellation fields | two columns, both headed "Cancelled..." | X1 columns AS and AT, values "No"; headers truncated (approximate) |
| Platform order status text | string | X1 column AU: header truncated "Order Stat...", empty in rows seen |
| Seller Name | string | X1 column AW: the seller's business name |
| IMEI | string, 15 digits | X1 column AX |
| Received | boolean | X1 column AY: "No" |
| Delivery address | address snapshot | Inferred: the address alerts in S3 need it; not a visible column |

Other columns in the recorded export are out of scope. Columns J to AF were read at 36:25 and AE to AY at 36:27.

### Invoice

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Invoice Type | enum: Product and the fee types found, per C-10 | S5 (video 37:32); X2 column A |
| Invoice Number | string | X2 column B |
| Order ID | reference to the platform order | X2 column C |
| Invoice Date | date | X2 column D (values hidden by column width) |
| Order Date | date | X2 column E (values hidden by column width) |
| Original Invoice | string, optional | X2 column F: header truncated "Original In..." |
| Reason | string, optional | X2 column G: header truncated "Reason Of..." |
| IMEI | string | X2 column H |
| Invoice Product | string | X2 column I: the product name as printed on the invoice |
| Ledger Product | string | X2 column J: the product name as recorded on the order unit (renamed here; header truncated in the recording) |
| Qty | integer | X2 column K: 1, 2 |
| Platform Product Code | string, optional | X2 column L: the platform's own product code (renamed here) |
| HSN | string | X2 column M: "85171300", "998599" |
| Taxable Amount | money | X2 column N: "75422.88" |
| IGST | money | X2 column O: "13576.12" (equals SGST plus CGST on intra-state rows; see Assumptions in spec.md) |
| SGST | money | X2 column P: "6788.06" |
| CGST | money | X2 column Q: "6788.06" |
| Total Amount | money | X2 column R: "88999", "894" |
| Buyer Name | string | X2 column S |
| Buyer GST | string, GSTIN | X2 column T |
| Seller Name | string | X2 column U |
| Seller GST | string, GSTIN | X2 column V |

### Compiled Invoice Archive

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Name | string: prefix plus ".zip" | S7 (video 38:42): "<prefix>.zip" |
| Size | bytes, shown in MB or GB | S7: "69.54 MB", "1.08 GB" |
| Last modified | date and time | S7: "Oct 21, 2026 15:20" |

### Compilation

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Status | enum: idle, running, failed (failed is Inferred) | S7: "No compilation in progress"; S4: "Compiling Invoices..." (video 38:32) |
| Name prefix | string | S6 (video 38:25) |
| Order count | integer | S4 message: "Compiling invoices for 182 orders" (video 38:32) |

### Alert

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Type | enum: Order unit address mismatch, Order unit address change | S3 (video 13:52) |
| State | enum: active, resolved | S3: the "<active> active · <resolved> resolved" line |
| Raised at | date and time, shown as an age | S3: "6 hours ago" |
| Order Unit | reference to Order Unit | Inferred from the type names |

### Saved View

| Field | Inferred type | Evidence |
|-------|---------------|----------|
| Visible columns | list of column names | S4 (video 33:05): Columns control |
| Filters | set of filter values | S4: "Quick Save" |
| Is default | boolean | S4: "Default View" |

## Implementation hints

Stack-agnostic: the recording reveals no framework. It shows only the route paths of the in-scope screens (`/login`, `/dashboard`, `/order-units`) and the file names (`order_units_<YYYY-MM-DD>_<HHMM>.csv`, `invoice_data_<YYYY-MM-DD>_<HHMM>.csv`, `<prefix>.zip`).

Not from the recording: the owner has an existing procurement build whose README lists Next.js, NestJS, PostgreSQL, Redis, BullMQ, and an eight-hour session cookie. /speckit-plan should decide whether this feature is built there.

## Coverage notes

- **Frames reviewed**: 31 of the 40 scene-change keyframes, plus 10 frames pulled at chosen timestamps to fill gaps: 05:15 and 05:35 (Addresses), 13:52 and 14:05 (Dashboard), 33:05, 35:05, and 35:30 (ledger and export), 38:10 (invoice CSV), and 38:25 and 38:32 (compilation). Nine keyframes were skipped: 00:05, 09:09, 35:59, and 37:44, which duplicate their neighbours, and 16:08, 18:43, 21:18, 23:52, and 27:45, which fall in a stretch of the recording that is out of scope.
- **Sparse or blurry stretches**: The keyframes missed the Dashboard (13:40 to 14:20), the Addresses tab (05:26 to 05:45), and the filter change and export (34:12 to 35:42); pulled frames recovered all three. The Compile Invoices dialog (38:25) is mid-fade, so its text is approximate.
- **Narration**: Hinglish, from an existing Whisper English pass. Long stretches are noise or repeated filler (for example 04:29 to 05:23, 09:28 to 10:40, 13:03 to 15:54, and 32:19 to 35:44), so the ledger and invoice flows were reconstructed mainly from frames, and only clear lines are quoted.
- **Low-confidence reads**: Unit IDs in S4 (small text); the Invoice action's enabled versus greyed look (33:05 against 34:12); truncated CSV headers in X1 and X2; the sixth invoice type in S5; the IGST column in X2; and whether the 33:05 count reflects the "Last 7 days" preset.
