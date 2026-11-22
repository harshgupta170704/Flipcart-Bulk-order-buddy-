# Order report format (import into the Order Units ledger)

**Status**: proposed 2026-10-24, for phase 3 of [audit-and-plan.md](audit-and-plan.md); **built 27 Oct 2026** — Order Units → "Upload order report" (`POST /orders/import-report`, the reader in `backend/app/io/order_report.py`), with "Download the template" beside it. Flipkart rows only for now; an `elex` row is refused as not supported yet. A unit the read-back has already seen on Flipkart only has its empty fields filled from a report, and an empty cell never blanks a stored value; the answer counts the units whose row said otherwise than Flipkart's page (another state, day, tracking id, seller or address) and says Flipkart's reading was kept. Since 27 Oct 2026 the refund (status, amount, method), the courier (`shipment_partner`), the report's own last tracking update and `cancelled_by_user` are kept per unit and written to the ledger CSV and the row dialog, and a `cash_on_delivery` of `No` is kept as paid in advance (`prepaid`), never as a card. The answer names the columns the ledger still has no place for (IMEI, billing name, EWB number, PO number) as not kept.
**Template**: [order-report-template.csv](order-report-template.csv) — open it, keep the header row, add one row per unit.

Flipkart buyer accounts offer no order-report download (only sellers get one), so this format is the portal's own. Any system that can produce these columns can feed the ledger: an export from Elex Ordering, a spreadsheet kept by hand, another tool's CSV.

## File

- **CSV** (UTF-8, comma-separated, one header row) or **XLSX** (first sheet, same header row).
- **One row per unit.** An order of three units is three rows sharing the same `order_id` with three different `unit_id`s.
- Header names are matched **case-insensitively**, and spaces, hyphens and underscores are interchangeable (`Order ID`, `order_id` and `order-id` are the same column). Columns the ledger does not know are ignored, so extra columns do no harm.
- A later upload containing a `(platform, unit_id)` the ledger already holds **updates** that unit; it never creates a second row (FR-024).
- Rows missing a required column, or with a value the ledger cannot read, are **rejected individually** with the row number and the reason; the rest of the file still loads (FR-060). A value longer than the ledger keeps is one of these: a `status` over 64 characters, a `billing_phone` over 20, a `billing_pincode` over 16, a `delivery_pincode` that is not 6 digits.
- A cell in a spreadsheet's text form, `="439123456789012345678"`, is read as the text inside it. That is how the ledger's own CSV export writes long ids and phones so Excel keeps every digit, so that export can be handed back as a report.

## Values

| Kind | Write it as | Example |
|---|---|---|
| Dates | `DD-MM-YYYY` | `15-10-2026` |
| Times | `HH:MM:SS`, 24-hour, India Standard Time | `14:19:46` |
| Money | plain number, two decimals allowed, no ₹ and no thousands separators | `17707.00` |
| Yes/No | `Yes` or `No` | `No` |
| Long identifiers (`unit_id`, `order_id`, `imei`, phones, tracking ids) | **text**. In Excel, format the column as *Text* before typing or pasting, or the sheet turns `439123456789012345678` into `4.39E+20` and the digits are lost for good. A `unit_id` is the platform's id alone: digits and letters, no spaces, dashes or formulas. | `OD338967210239333100` |
| Card | masked only — the first two digits and `X`s. Never a full card number. The ledger never reads this column, whatever it holds. | `53XXXXXX` |

## Columns

**Required** (a row without these is rejected):

| Column | Meaning |
|---|---|
| `platform` | `flipkart` or `elex` (more can be added) |
| `order_id` | The platform's order number |
| `unit_id` | The platform's id for this one unit (unique within the platform) |
| `order_date` | Date the order was placed |
| `status` | The status exactly as the platform words it (`Delivered`, `Out for delivery`, `Cancelled - Order shipped`, …). The ledger groups it; it does not rewrite it. |
| `product` | Product name, with the variant in brackets if any: `vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)` |

**Recommended** (the dashboard, invoices and reconciliation need them):

| Column | Meaning |
|---|---|
| `order_time` | Time the order was placed |
| `platform_email` | The platform account that bought it |
| `unit_amount` | Price of this one unit (prices the order when `total_order_amount` is blank) |
| `quantity` | Units in the order this row belongs to (blank: as many as the order has rows) |
| `total_order_amount` | The whole order's total |
| `estimated_delivery` | Date the platform promised |
| `actual_delivery_date` | Date it was delivered (blank until then) |
| `tracking_id`, `shipment_partner`, `last_tracking_update` | Courier details |
| `seller_name` | The seller on the platform |
| `imei` | For phones |

**Billing and GST**:

| Column | Meaning |
|---|---|
| `business_name`, `business_gstin` | The GST entity the order was billed under (15-character GSTIN) |
| `billing_name`, `billing_phone`, `billing_pincode` | Billing details as entered on the platform |
| `card_masked` | Masked card reference, e.g. `53XXXXXX` |
| `cash_on_delivery` | `Yes` / `No` |
| `ewb_number` | E-way bill number, if any |

**Delivery address** (needed for the address-mismatch and address-change alerts). Taken only when `delivery_line1` and `delivery_pincode` are both filled. Only another first line or pincode counts as an address change. The same place written with fewer parts leaves the address held as it is; written with more, or with a part corrected (a flat, a phone), a unit never read back takes the row's text, still without counting it as an address change.

| Column | Meaning |
|---|---|
| `delivery_name` | Name on the delivery address |
| `delivery_line1` | House / flat / building |
| `delivery_line2` | Locality / street |
| `delivery_city`, `delivery_state`, `delivery_pincode` | — |
| `delivery_phone` | — |

**Status details**:

| Column | Meaning |
|---|---|
| `refund_status`, `refund_amount`, `refund_method` | Blank unless a refund exists |
| `cancelled` | `Yes` / `No` |
| `cancelled_by_user` | `Yes` / `No` |
| `received` | `Yes` once the goods are physically in hand |
| `invoice_on_file` | `Yes` if the invoice PDF has been uploaded (the ledger sets this itself when a PDF is attached; the column is accepted for completeness) |
| `po_number` | Internal purchase-order reference, if any |
| `source` | Where the unit came from: `run` (placed by this tool), `sync` (read back from the platform), `upload`. Set automatically on import; accepted if present. |

## Open points

- `platform = elex`: confirm what Elex Ordering is (the existing Next.js/NestJS build?) and whether it can export this file directly or expose an API — a direct connection would make uploads unnecessary for it.
- Statuses: the status words for a second platform are taken exactly as reported (FR-009); the grouping into Confirmed / Cancelled / Others is extended as new words appear.
