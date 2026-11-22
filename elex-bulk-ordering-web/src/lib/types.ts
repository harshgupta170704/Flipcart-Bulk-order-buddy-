/**
 * The API contract, verbatim. Every shape the backend returns lives here and
 * nowhere else.
 */

/* --- auth ----------------------------------------------------------------- */

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  /** An admin gave this user a temporary password: change it before anything else. */
  must_change_password: boolean;
}

/** Spec FR-058 / C-7: admins manage users, addresses, GSTs, unit limits and
 *  settings; operators do everything that moves orders. Both read everything. */
export type UserRole = "admin" | "operator";

export interface MeResponse {
  /** The email, under the name the first version of the API used. */
  username: string;
  email: string;
  name: string;
  role: UserRole;
  must_change_password: boolean;
  /** The configured admin: the password set on the server stays a second key. */
  is_env_admin: boolean;
  /** When the token expires — the session countdown reads this (spec C-13). */
  expires_at: string | null;
}

/** `POST /auth/change-password`: who you are now, plus a fresh token — the
 *  change ends every other session of this user, including the one that asked. */
export interface PasswordChangedResponse extends MeResponse {
  access_token: string;
  expires_in: number;
}

/** What the sign-in page may offer (public). */
export interface AuthOptions {
  /** A mail server is configured: "Forgot password?" can send a reset link. */
  password_reset_by_email: boolean;
}

/* --- users ---------------------------------------------------------------- */

export interface AppUser {
  id: number;
  email: string;
  name: string;
  role: UserRole;
  active: boolean;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
  /** The configured admin: cannot be deactivated or demoted. */
  is_env_admin: boolean;
}

export interface UserCreateInput {
  email: string;
  name: string;
  role: UserRole;
  temporary_password: string;
}

export interface UserPatchInput {
  name?: string;
  role?: UserRole;
  active?: boolean;
}

/* --- accounts ------------------------------------------------------------- */

/** Every status the server's AccountStatus enum can report. */
export const ACCOUNT_STATUSES = [
  "new",
  "session_valid",
  "session_invalid",
  "logged_out",
  "not_registered",
  "multiple_attempts",
  "blocked",
  "other",
] as const;

/** The exact set. Use this when a value must be one of the eight. */
export type AccountStatusValue = (typeof ACCOUNT_STATUSES)[number];

/**
 * Open on purpose.
 *
 * The run pages keep exhaustive `Record<AccountStatus, …>` maps that spell out
 * only the original three statuses; widening this union to all eight would
 * break them, and those files are out of scope for this phase. Listing just
 * the three literals keeps those maps valid (the rest fall under the implied
 * string index) while still accepting every status the server now sends.
 * Reach for `AccountStatusValue` wherever the precise set is wanted.
 */
export type AccountStatus = "new" | "session_valid" | "session_invalid" | (string & {});

/** A tag as it rides along on an account. */
export interface TagRef {
  id: number;
  name: string;
}

export interface Account {
  id: number;
  email: string;
  phone: string | null;
  status: AccountStatus;
  /** Free text from the platform, e.g. "ACTIVE-PREMIUM-GOLD". */
  subscription_type: string | null;
  /** One tag per account; null reads as "Untagged". */
  tag: TagRef | null;
  /**
   * The account's own GST: its orders bill under it whatever GST the run
   * picks. null means "the run's GST". Absent from an older server.
   */
  gst?: GstRef | null;
  /** The phone its orders are placed with; null means its own `phone`. */
  billing_phone?: string | null;
  /** Last read from Flipkart (Accounts → Read balances). */
  supercoins_balance?: number | null;
  gift_voucher_balance?: number | null;
  balances_read_at?: string | null;
  /** Parent / child accounts: a child names its parent's email. */
  is_parent?: boolean | null;
  parent_email?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AccountInput {
  email: string;
  phone?: string;
}

/**
 * PATCH /accounts/{id} — any subset. `tag_id: null` clears the tag,
 * `gst_id: null` puts the account back on the run's GST, and
 * `billing_phone: null` back on its own phone.
 */
export interface AccountPatch {
  phone?: string;
  status?: AccountStatus;
  subscription_type?: string | null;
  tag_id?: number | null;
  gst_id?: number | null;
  billing_phone?: string | null;
}

/** The server's width for a billing phone (`MAX_BILLING_PHONE_LENGTH`). */
export const BILLING_PHONE_MAX_LENGTH = 20;

export interface ImportResult {
  created: number;
  updated: number;
  skipped: number;
  /** Rows the import refused, each with its reason (no email address, say). */
  rejected?: { row: number; reason: string }[];
}

/* --- account filters -----------------------------------------------------
   At 37,062 accounts a selection is expressed as a FILTER, never as a list of
   ids: the same filter drives the table, bulk tagging and the start of a run.
-------------------------------------------------------------------------- */

/** The request body shape shared by /accounts/tag and every job-start call. */
export interface AccountFilterInput {
  /** Substring over email OR phone. */
  q?: string;
  statuses?: string[];
  tag_ids?: number[];
  /** OR'd with tag_ids. */
  include_untagged?: boolean;
  subscriptions?: string[];
  ids?: number[];
  /** Email domains ("soniclip.in"); an account matches any of them. */
  domains?: string[];
}

/** The server's sort whitelist. Anything else is rejected. */
export const ACCOUNT_SORT_FIELDS = [
  "id",
  "email",
  "phone",
  "status",
  "subscription_type",
  "created_at",
  "updated_at",
] as const;

export type AccountSortField = (typeof ACCOUNT_SORT_FIELDS)[number];

/** Query string for the paged GET /accounts. */
export interface PagedAccountsParams {
  page?: number;
  size?: number;
  q?: string;
  statuses?: string[];
  tag_ids?: number[];
  untagged?: boolean;
  subscriptions?: string[];
  domains?: string[];
  sort?: AccountSortField;
  desc?: boolean;
}

/** GET /accounts — the paged envelope that replaced the bare list. */
export interface AccountPage {
  items: Account[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

export interface StatusCount {
  value: AccountStatus;
  count: number;
}

export interface TagCount {
  id: number;
  name: string;
  count: number;
}

/** GET /accounts/filter-options — what the filter bar offers, with counts. */
export interface FilterOptions {
  statuses: StatusCount[];
  subscriptions: string[];
  tags: TagCount[];
  total: number;
  untagged: number;
  /** Email domains present, most accounts first. Absent from older servers. */
  domains?: { value: string; count: number }[];
}

/** POST /accounts/tag — exactly one of account_ids / filter. */
export interface TagAccountsInput {
  /** null clears the tag. */
  tag_id: number | null;
  account_ids?: number[];
  filter?: AccountFilterInput;
}

export interface TagResult {
  updated: number;
}

/**
 * POST /accounts/gst — one GST for a selection, exactly one of account_ids /
 * filter (at most 5,000 ids; past that, send the filter).
 */
export interface GstAssignInput {
  /** null takes the accounts' own GST off: they bill under the run's again. */
  gst_id: number | null;
  account_ids?: number[];
  filter?: AccountFilterInput;
}

export interface GstAssignResult {
  updated: number;
}

/* --- tags ----------------------------------------------------------------- */

export interface Tag {
  id: number;
  name: string;
  account_count: number;
}

/* --- selecting accounts for a run (client-side shape) --------------------
   Not an API body: it is what a picker hands its host, which then sends
   either `account_ids` or `account_filter`.
-------------------------------------------------------------------------- */

export type AccountSelection =
  | { mode: "ids"; ids: number[] }
  | { mode: "filter"; filter: AccountFilterInput; count: number };

/** How many accounts a selection will actually run. */
export function selectionCount(selection: AccountSelection): number {
  return selection.mode === "ids" ? selection.ids.length : selection.count;
}

/**
 * Mirrors the server's `max_accounts_per_run`, which is the hard ceiling for
 * one run. It is duplicated here because GET /settings does not expose it yet
 * (see integration notes) — the UI uses it only to stop offering a selection
 * the server would refuse with a 422, never to decide anything on its own.
 */
export const RUN_ACCOUNT_CEILING = 5000;

/** Page sizes the accounts table offers. */
export const ACCOUNT_PAGE_SIZES = [25, 50, 100, 200] as const;

/* --- addresses & GSTs ----------------------------------------------------
   The delivery address and the GST an order bills under used to be four flat
   `default_*` settings, so every order shipped to one place under one GSTIN.
   They are managed lists now, and the only source: a run ships to the chosen
   address, else the default, and is refused with neither; it bills under the
   chosen GST, else the address's linked GST, else the default, else none.
-------------------------------------------------------------------------- */

/** A GST as it rides along on an address — enough to show, not to edit. */
export interface GstRef {
  id: number;
  label: string;
  gstin: string;
  business_name: string;
}

/** GET /gsts — one business entity and the GSTIN it bills under. */
export interface Gst extends GstRef {
  is_default: boolean;
  /** Addresses linked to it. Exactly what a delete would unlink. */
  address_count: number;
  /** Accounts that have it as their own GST; a delete puts them back on their runs' GST. */
  account_count?: number;
  created_at: string;
}

/** DELETE /gsts/{id} — how many addresses and accounts the delete unlinked,
 *  counted by the server as it did it: the list on the page may be out of date. */
export interface GstDeleted {
  unlinked_addresses: number;
  unlinked_accounts: number;
}

/** GET /addresses — where a run's orders are delivered. */
export interface Address {
  id: number;
  label: string;
  name: string;
  phone: string | null;
  line1: string;
  floor: string | null;
  city: string | null;
  state: string | null;
  pincode: string;
  is_default: boolean;
  /** null when this address has not been linked to a GST yet. */
  gst: GstRef | null;
  created_at: string;
}

/** POST /gsts. `is_default: true` clears it on every other GST. */
export interface GstInput {
  label: string;
  gstin: string;
  business_name: string;
  is_default?: boolean;
}

/** PATCH /gsts/{id} — any subset of the same fields. */
export type GstPatch = Partial<GstInput>;

/**
 * POST /addresses. `gst_id: null` leaves the address unlinked.
 *
 * The four optional text fields take null as well as a string, because that is
 * what their columns hold when empty and the only value that can clear one
 * again on a PATCH.
 */
export interface AddressInput {
  label: string;
  name: string;
  phone?: string | null;
  line1: string;
  floor?: string | null;
  city?: string | null;
  state?: string | null;
  pincode: string;
  gst_id?: number | null;
  is_default?: boolean;
}

/** PATCH /addresses/{id} — any subset of the same fields. */
export type AddressPatch = Partial<AddressInput>;

/** The server's own lengths, checked client-side so a 422 is the rare case. */
export const PINCODE_LENGTH = 6;
export const GSTIN_LENGTH = 15;

/* --- unit limits ---------------------------------------------------------
   Platforms cap how many units ONE account may buy of a category / brand /
   model in a rolling window (20 handsets per 31 days, 16 vivo per 31 days, 5
   of one model per 30 days). Exceeding a cap gets the order rejected or the
   account flagged, so a run checks what an account has left BEFORE it spends
   that account's quota.
-------------------------------------------------------------------------- */

/** What a limit counts, mirroring the server's LimitScope enum. */
export const LIMIT_SCOPES = ["category", "brand", "model", "variant", "seller"] as const;

export type LimitScope = (typeof LIMIT_SCOPES)[number];

export interface UnitLimit {
  id: number;
  scope: LimitScope;
  /**
   * The thing being limited, e.g. "vivo". An empty string means EVERY value in
   * this scope — which is why it is never rendered as a blank cell: see
   * `limitValueLabel`, which spells it out as "Any brand".
   */
  value: string;
  max_units: number;
  window_days: number;
  /** A disabled limit is kept for reference but not enforced. */
  enabled: boolean;
  created_at: string;
}

/** POST /unit-limits. A duplicate (scope, value) pair comes back as a 409. */
export interface UnitLimitInput {
  scope: LimitScope;
  /** Left out or "" means "every value in this scope". */
  value?: string;
  max_units: number;
  window_days?: number;
  enabled?: boolean;
}

/** PATCH /unit-limits/{id} — any subset of the same fields. */
export type UnitLimitPatch = Partial<UnitLimitInput>;

/**
 * POST /unit-limits/check — which accounts to check, and what is being ordered.
 *
 * Exactly one of `account_ids` / `filter`, the same way every job start
 * resolves its accounts. The product facts decide which limits even apply:
 * with none of them set, a per-brand or per-category limit cannot match.
 */
export interface LimitCheckInput {
  account_ids?: number[];
  filter?: AccountFilterInput;
  brand?: string;
  category?: string;
  model?: string;
  /** The seller the run buys from; matches seller-scoped limits. */
  seller?: string;
}

/**
 * One limit that applies to what is being ordered, totalled over the accounts
 * that were checked: `used` and `remaining` are unit sums across them and
 * `blocked` is how many of those accounts have nothing left under this limit.
 */
export interface LimitUsage {
  limit_id: number;
  scope: LimitScope;
  value: string;
  max_units: number;
  window_days: number;
  used: number;
  remaining: number;
  blocked: number;
}

/* --- settings ------------------------------------------------------------- */

/** A numbered OTP inbox from the server's settings (never its password). */
export interface ExtraOtpInbox {
  number: number;
  user: string;
  host: string;
  /** The account emails (or "@domain"s) whose login codes are read from it. */
  serves: string[];
}

/** A numbered OTP inbox the server's settings name but cannot be used. */
export interface UnusedOtpInbox {
  number: number;
  user: string;
  /** Why, naming the setting to fix. */
  problem: string;
}

export interface AppSettings {
  max_concurrency: number;
  browser_headless: boolean;
  proxy_server: string;
  /** Automatic read-backs every N hours; 0 = only when someone presses the button. */
  sync_every_hours: number;
  otp_imap_host: string;
  otp_imap_port: number;
  otp_imap_user: string;
  otp_imap_mailbox: string;
  otp_imap_use_ssl: boolean;
  otp_poll_interval: number;
  otp_fetch_timeout: number;
  /** The OTP password is never returned — only whether one is on file. */
  otp_imap_password_set: boolean;
  /** Inboxes 2, 3, … from the server's settings file; read-only here. */
  otp_extra_inboxes?: ExtraOtpInbox[];
  /** Numbered inboxes the server's settings file names but cannot use. */
  otp_unused_inboxes?: UnusedOtpInbox[];
}

/** PUT /settings takes any subset of the fields, plus the write-only password. */
export type SettingsUpdate = Partial<Omit<AppSettings, "otp_imap_password_set">> & {
  otp_imap_password?: string;
};

/* --- jobs ----------------------------------------------------------------- */

/** "sync" reads order status back from the platform — it places nothing;
 *  "compile" zips the invoice PDFs of a ledger selection. */
export type JobType = "login" | "coupon" | "order" | "sync" | "compile";
export type JobStatus = "queued" | "running" | "completed" | "failed";
/** `unconfirmed`: an order was sent for this account but never confirmed — it
 *  may exist. Check the account on Flipkart before running it again. */
export type JobItemStatus = "pending" | "running" | "done" | "failed" | "skipped" | "unconfirmed";

export interface Job {
  id: number;
  type: JobType;
  status: JobStatus;
  progress: number;
  total_items: number;
  done_items: number;
  failed_items: number;
  /** Accounts passed over (no session, no card, already done, cancelled).
   *  Tracked apart from failed_items so a healthy run never reads as red. */
  skipped_items: number;
  /** Orders sent but never confirmed — they may exist. Neither ok nor failed. */
  unconfirmed_items: number;
  cancel_requested: boolean;
  payload: Record<string, unknown> | null;
  started_at: string;
  finished_at: string | null;
}

/** The Orders page's filters (GET /jobs/bulk-orders). */
export interface BulkOrderRunFilters {
  since?: string;
  until?: string;
  status?: string[];
  mode?: string;
  created_by?: string;
  platform?: string;
}

/** One agent of the fleet (GET /agents/fleet). */
export interface FleetAgent {
  id: number;
  name: string;
  kind: "api" | "worker" | string;
  host: string | null;
  state: "online" | "offline" | "disabled";
  enabled: boolean;
  isolated: boolean;
  last_seen_at: string | null;
  last_test_at: string | null;
  last_test_ok: boolean | null;
}

export interface FleetStatus {
  agents: FleetAgent[];
  total: number;
  online: number;
  disabled: number;
  offline: number;
}

/** One order run as the Orders page lists it (GET /jobs/bulk-orders). */
export interface BulkOrderRun extends Job {
  platform: string;
  /** "real", "mock" or "ai". */
  mode: string;
  product_link: string | null;
  product_name: string | null;
  total_quantity: number | null;
  qty_per_order: number | null;
  orders_placed: number;
  units_placed: number;
  orders_unconfirmed: number;
  /** Orders that did not go through. */
  orders_failed: number;
  amount_placed: number;
  /** The card the run paid with, masked ("54XX…824803"). */
  parent_card: string | null;
  execution_version: string | null;
  /** The run used Flipkart Minutes. */
  flipkart_minutes: boolean;
  payment_mode: string | null;
  card_type: string | null;
  ref_code: string | null;
  started_by: string | null;
  address_label: string | null;
  gst_label: string | null;
  target: number | null;
}

export interface JobItem {
  account_id: number;
  email: string;
  status: JobItemStatus;
  message: string | null;
  updated_at: string;
  /** Per-account timing: when the run started on this account, when it
   *  stopped, and the seconds between — null until both ends are known. */
  started_at?: string | null;
  finished_at?: string | null;
  seconds?: number | null;
}

export interface JobDetail extends Job {
  items: JobItem[];
}

/* --- the invoices of one order (GET /orders/{row}/invoices) ----------------- */

/** One line of an invoice as the reader took it off the PDF. */
export interface OrderInvoiceLine {
  title: string | null;
  fsn: string | null;
  hsn_sac: string | null;
  imei: string | null;
  qty: number | null;
  gross: number | null;
  discount: number | null;
  taxable: number | null;
  cgst: number | null;
  sgst: number | null;
  igst: number | null;
  total: number | null;
}

/** One invoice on file for an order: what was read off the PDF, and where the PDF is served. */
export interface OrderInvoice {
  id: number;
  kind: "product" | "fee" | "credit_note" | "unknown" | string;
  invoice_type: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  order_date: string | null;
  seller_name: string | null;
  seller_gstin: string | null;
  buyer_name: string | null;
  buyer_gstin: string | null;
  grand_total: number | null;
  original_invoice: string | null;
  reason: string | null;
  lines: OrderInvoiceLine[];
  source: string;
  fetched_at: string | null;
  size: number;
  parse_error: string | null;
  download_path: string;
}

/* --- compile invoices ----------------------------------------------------
   A compilation is a job of type "compile" (spec FR-031–FR-039). Its payload
   carries archive_name, orders, units and units_without_invoice from the
   start, and outcome ("completed" | "cancelled" | "failed"), error and
   archive_id once it has ended.
-------------------------------------------------------------------------- */

export interface CompileStartResult {
  job: Job;
  /** `<prefix>.zip`, numbered when the prefix was already taken. */
  archive_name: string;
  orders: number;
  units: number;
  /** Units whose order has no invoice on file — left out (FR-030). */
  units_without_invoice: number;
}

/** What "Compilation Status" shows: the run in progress, and the last one. */
export interface CompileStatus {
  active: Job | null;
  last: Job | null;
  /** How long archives are kept; 0 means until deleted. */
  archive_retention_days: number;
}

export interface CompiledArchive {
  id: number;
  name: string;
  /** Bytes. */
  size: number;
  orders: number;
  units: number;
  /** PDFs written into the zip. */
  invoices: number;
  /** Units left out: their order had no invoice on file, or its file was
   *  missing from the store when the zip was built. */
  left_out: number;
  /** Invoice rows whose PDF was missing from the store at build time. */
  missing_files: number;
  created_at: string;
  job_id: number | null;
}

/** One page of the archive list, newest first (GET /invoices/archives). */
export interface CompiledArchivePage {
  items: CompiledArchive[];
  /** Every archive, on every page: the "Showing 200 of 230". */
  total: number;
  /** What to ask the next, older page with — it names this page's last
   *  archive, so archives deleted from the list meanwhile skip nothing; null
   *  on the page that reaches the oldest. */
  next_before: string | null;
}

/* Every start endpoint resolves in the same order: `account_filter` if present,
   else a non-empty `account_ids`, else every account. */

export interface LoginJobInput {
  account_ids: number[] | null;
  account_filter?: AccountFilterInput;
  force: boolean;
}

export interface CouponJobInput {
  account_ids: number[] | null;
  account_filter?: AccountFilterInput;
  coupon_code: string;
}

/**
 * How the order is paid for. COD needs no card at all. "unknown" and "prepaid"
 * only ever come back from the server: "unknown" for an order the tool did not
 * place (handed in by id to track, read back) when nothing says how it was
 * paid; "prepaid" when an order report says it was not Cash on Delivery,
 * without saying how it was paid — never a card.
 */
export type PaymentMode =
  | "card"
  | "cod"
  | "cod_card"
  | "gv"
  | "gv_card"
  | "rtgs"
  | "imps"
  | "unknown"
  | "prepaid";

/** The modes that pay (part) by card, and so need the card sheet. */
export function usesCard(mode: PaymentMode | string | null | undefined): boolean {
  return mode === "card" || mode === "gv_card" || mode === "cod_card";
}

/** The modes that pay with gift cards, and so need the gift card sheet. */
export function usesGiftCards(mode: PaymentMode | string | null | undefined): boolean {
  return mode === "gv" || mode === "gv_card";
}

/** The modes the order form offers. COD + Card, RTGS, IMPS and Gift Voucher
 *  only stay hidden until they are mapped on a live payment page (the server
 *  refuses them too). */
export const OFFERED_PAYMENT_MODES: PaymentMode[] = ["card", "cod", "gv_card"];

/** How the order form names each mode. */
export const PAYMENT_MODE_LABELS: Record<string, string> = {
  card: "Card",
  cod: "Cash on Delivery",
  cod_card: "COD + Card (advance payment)",
  gv: "Gift Voucher only",
  gv_card: "GV + card",
  rtgs: "RTGS",
  imps: "IMPS",
};

/** One product a Flipkart search found (GET /products/search). */
export interface ProductSearchResult {
  /** The exact variant, colour and storage included. */
  title: string;
  /** The canonical product link: https://www.flipkart.com/<slug>/p/itm…?pid=PID. */
  link: string;
  pid: string;
  price: number | null;
  rating: number | null;
  image: string | null;
}

export interface ProductSearchAnswer {
  query: string;
  results: ProductSearchResult[];
  /** Plain words when the search could not be made; null otherwise. */
  error: string | null;
}

/** One selected account and the card that would pay for it — the card by its
 *  last four digits only; the bank password as a yes or no, never its value. */
export interface CardPairing {
  account_id: number;
  email: string;
  /** null: no card in the sheet pays for this account, and the run skips it. */
  card_last4: string | null;
  has_password: boolean | null;
}

/** POST /jobs/order/card-preview: the pairing a card run would use. */
export interface CardPreview {
  accounts: CardPairing[];
  paired: number;
  without_card: number;
  /** "email" when the sheet has an email column; "position" for one account without. */
  paired_by: string;
  /** Rows of the sheet that could not be a card, by row number and reason. */
  rows_set_aside: { row: number; reason: string }[];
}

/** "real" places orders; "mock" runs everything up to the browser and makes
 *  up each order's outcome — no browser, nothing bought, no order rows. */
export type RunMode = "real" | "mock" | "ai";

/** AI mode's pacing, as sent to the server. */
export interface AiPacingInput {
  /** Minutes, 1-60: the longest wait before the next batch. */
  max_interval_minutes: number;
  /** 1-30: the first batch's size, and what a slow batch resets to. */
  min_attempts: number;
  /** 1-90: % of a batch that must place for the next to start at once, doubled. */
  scale_up_pct: number;
  /** "12h", "90m", "2d": between 1h and 2d. */
  run_duration: string;
  /** Pace for real but make up each outcome: nothing is bought. */
  rehearsal: boolean;
}
/** "upto": an order may carry fewer units when Flipkart caps the quantity per
 *  order. "minimum": all of them, or that order is refused before payment. */
export type QtyMode = "upto" | "minimum";

export interface OrderJobInput {
  /** Left out means "real". */
  mode?: RunMode;
  /** Left out means "upto". */
  qty_mode?: QtyMode;
  /** Only with mode "ai". */
  ai?: AiPacingInput;
  /** Refuse an order whose checkout does not show the GST it bills under. */
  gst_mandatory?: boolean;
  /** Delivery phone numbers (10 digits), used by the accounts in turn. */
  address_phones?: string[];
  /** Every order carries the address book's phone instead of the account's. */
  use_address_phone?: boolean;
  /** Pass over accounts with an order bought in the last N days (1–365). */
  exclude_ordered_days?: number | null;
  /** Try never-ordered accounts first, then those that ordered longest ago. */
  smart_selection?: boolean;
  /* Card pool rules — card runs only. */
  /** Skip a card that paid for an order in the last N days. */
  card_cooldown_days?: number | null;
  /** Try accounts whose card was used least in the last N days first. */
  card_least_used_days?: number | null;
  /** One card pays for at most N orders in this run. */
  card_max_uses?: number | null;
  /** Skip a card that failed at the payment step in the last N days. */
  card_exclude_failed_days?: number | null;
  /** A card pays for at most N orders in any 30 days (from order history). */
  card_smart_max_uses?: number | null;
  /** Pass over a CorpAssist HDFC child card with less than this many rupees left. */
  card_min_remaining?: number | null;
  /** Only failures with these codes count against a card. */
  card_exclude_failed_codes?: string[];
  /** Use only the sheet's rows of this Card Type (and record it on the run). */
  card_type?: string | null;
  /** A free-text reference written on every order of the run. */
  ref_code?: string | null;
  /** "normal" | "normal_mcc" | "high" | "high_mcc" — how fast the run goes. */
  execution_version?: string;
  /** Run only on agents marked isolated (and keep them from other work). */
  isolated_agents?: boolean;
  /** Gift cards for a "gv" / "gv_card" run: Email, Voucher Number, Voucher PIN. Never stored. */
  gift_cards_file?: File | null;
  /** More product links bought in the same order. */
  extra_product_links?: string[];
  coupon_codes?: string[];
  reward_ids?: string[];
  flipkart_minutes?: boolean;
  /** Take the account's SuperCoins off the total; only accounts with at least min_supercoins. */
  use_supercoins?: boolean;
  min_supercoins?: number | null;
  /** Take the account's gift card / wallet balance off the total. */
  use_wallet?: boolean;
  apply_additional_offer?: boolean;
  product_link: string;
  qty: number;
  /** The max payable per unit. null only together with `no_cap: true`: the
   *  server refuses a run that states neither, and one that states both
   *  (audit F-23). A blank target no longer means "no cap". */
  target: number | null;
  /** true says, on purpose, that this run has no price cap: every order is
   *  placed at whatever its final amount comes to. Recorded on the job. */
  no_cap: boolean;
  /** null means "every account" — the API treats it the same way. */
  account_ids: number[] | null;
  /** Wins over account_ids. Sent as a JSON string in the multipart body. */
  account_filter?: AccountFilterInput;
  payment_mode: PaymentMode;
  /** Required for "card", must be null for "cod". Used for this run only and
   *  never stored by the backend. */
  cards_file: File | null;
  /** Which saved address every order in this run ships to. Left out — or null
   *  — and the server uses the Address Book's default address; with none on
   *  file the run is refused (422) rather than aimed anywhere else. */
  address_id?: number | null;
  /** Which GST the run bills under. Left out and the server uses the chosen
   *  address's linked GST, else the default GST, else none. */
  gst_id?: number | null;

  /* --- the quantity model ------------------------------------------------
     All optional, and all omitted from the request when unset. With
     `total_quantity` unset the run behaves exactly as it always has: one order
     of `qty` units per selected account. Set it and the run instead keeps
     placing orders of `qty_per_order` units until the total is reached.
  ---------------------------------------------------------------------- */

  /** Units to order in total, spread across accounts. */
  total_quantity?: number | null;
  /** Units in each single order. The server defaults it to 1. */
  qty_per_order?: number | null;
  /** A ceiling on how many ACCOUNTS the run tries — not on units. */
  total_attempts?: number | null;
  /** Checked on the checkout before payment: a total above it, or unreadable, stops the order. */
  cart_amount_limit?: number | null;
  /** Checked on the checkout before payment: a promise further out, or unreadable, stops the order. */
  max_delivery_days?: number | null;
  /** What is being bought, so unit limits can match it and later count it. */
  brand?: string | null;
  category?: string | null;
  /** The product's title as the preview read it: a model limit ("T5x 5G")
   *  applies when the title contains it. */
  model?: string | null;
  /** Buy from this seller, as the product's sellers page names it (any case),
   *  instead of whoever Flipkart shows by default. Left out or blank: Flipkart's
   *  default. At most 100 characters (`MAX_SELLER_CHARS`). A seller the page
   *  does not list stops that order before anything is pressed. */
  seller?: string | null;
  /** A person at the computer the tool runs on finishes the bank's step after
   *  Pay: the run shows its browser window, takes one account at a time and
   *  types nothing on the bank's page. Card runs only; sent only when true. */
  bank_step_by_hand?: boolean;
}

/** The longest seller name the server accepts on an order run. */
export const MAX_SELLER_CHARS = 100;

/** The largest card sheet the server reads (its MAX_CARD_FILE_BYTES, 1 MB).
 *  A bigger file is never sent, not even for the pairing preview: the
 *  server's form reader would write it to disk before refusing it. */
export const MAX_CARD_FILE_BYTES = 1024 * 1024;

/**
 * POST /jobs/sync — read order status back from the platform.
 *
 * Resolves its accounts exactly like every other start endpoint. A sync opens
 * each account's own order pages and writes what it reads onto the matching
 * Order rows: it places no order, changes nothing on the platform and spends
 * no account's unit quota.
 *
 * The server REJECTS unknown fields on this body, so nothing may be sent here
 * that is not one of the four below.
 */
export interface SyncJobInput {
  /** null means "every account" — the API treats it the same way. */
  account_ids: number[] | null;
  /** Wins over account_ids. */
  account_filter?: AccountFilterInput;
  /**
   * How many of each account's most recent orders to read. Each one costs a
   * navigation to its own detail page — that is where the ids and the OTP live
   * — so this is the knob that decides how long a run takes.
   */
  limit_per_account?: number;
  /**
   * Also record orders this tool never placed, so what was ordered by hand or
   * through the old portal shows up in the same table. Off by default: on an
   * account with years of personal history it would bury this tool's own
   * orders, and those rows then count towards the platform unit limits.
   */
  import_unknown?: boolean;
  /**
   * Read exactly these orders, by id, instead of each account's recent list —
   * for ids handed in to track (`importOrderIds`) or older than that list's
   * window. With `account_ids: null` the server picks the accounts holding them.
   */
  order_ids?: string[] | null;
}

/** POST /orders/import-ids: what a sheet of order ids did, and what to read back. */
export interface OrderIdsImportResult {
  created: number;
  existing: number;
  rejected: { row: number; reason: string }[];
  account_ids: number[];
  order_ids: string[];
}

/** POST /orders/import-report: what an order report did. */
export interface OrderReportImportResult {
  created: number;
  updated: number;
  unchanged: number;
  /** Rows that said otherwise than Flipkart's own page: its reading was kept. */
  kept_platform: number;
  rejected: { row: number; reason: string }[];
  /** Columns the file carried that the ledger keeps nowhere yet. */
  not_kept: string[];
}

/** POST /invoices/upload: what happened to each PDF handed in. */
export interface InvoiceUploadResult {
  stored: number;
  files: {
    filename: string;
    order_id: string | null;
    /** `mismatch`: the file name claims one order while the PDF prints
     *  another; the server attaches it to neither and the reason names both.
     *  `not_saved`: the database refused its row; nothing of it was kept. */
    status: "stored" | "duplicate" | "no_match" | "mismatch" | "not_pdf" | "too_large" | "not_saved";
    /** For a file this upload put on file: `not_read` when the reader could
     *  not make an invoice of it — kept all the same, with why in `reason`,
     *  but in no invoice-data CSV until it can be read. */
    reading?: "read" | "not_read" | null;
    reason: string | null;
  }[];
}

/** One entry of the Extract Invoices Data list: a type found on file (spec C-10). */
export interface InvoiceTypeCount {
  /** "Product", the fee's own name ("Secure Packaging Fee"), "Credit Note", … */
  type: string;
  /** Invoices of this type on the matching units' orders. */
  invoices: number;
  /** Matching units whose order has an invoice of this type. */
  units: number;
}

/** GET /invoices/types?<ledger filter>: the types found, with counts (FR-026, FR-030). */
export interface InvoiceTypes {
  /** "Product" first, then the rest by name. */
  types: InvoiceTypeCount[];
  /** Units matching the filter. */
  units: number;
  /** Of those, how many have no invoice the tool could read — left out of the CSV. */
  units_without_invoice: number;
}

/** What the invoice-data download reported in its headers (FR-027, FR-030). */
export interface InvoiceDataDownload {
  /** Rows written: one per invoice line of the ticked types. */
  rows: number;
  /** Matching units with no readable invoice of the ticked types. */
  units_left_out: number;
}

/** The server's own default and ceiling for `limit_per_account`. */
export const SYNC_ORDERS_PER_ACCOUNT = 20;
export const SYNC_ORDERS_PER_ACCOUNT_MAX = 200;

/* --- product preview -----------------------------------------------------
   POST /products/preview reads the product page itself — name, image, price,
   stock — so the operator can see what a run is about to buy BEFORE it spends
   a single account on it.
-------------------------------------------------------------------------- */

/**
 * What the server read off the page. Everything but `url` and `currency` is
 * nullable, because every one of them is scraped and any one of them can be
 * missing from a page that still orders perfectly well.
 */
export interface ProductPreview {
  /** The link that was read, echoed back. Always present. */
  url: string;
  name: string | null;
  brand: string | null;
  category: string | null;
  color: string | null;
  /** A remote CDN image. It may 404, so it is never trusted to render. */
  image: string | null;
  sku: string | null;
  price: number | null;
  /** ISO code; the server falls back to "INR" rather than sending null. */
  currency: string;
  /** The raw schema.org URL, e.g. "https://schema.org/InStock". */
  availability: string | null;
  /**
   * Tri-state, and the third state matters: `null` means the page did not say,
   * which is NOT the same as out of stock and must never be rendered as such.
   * Only `false` is a reason to warn about stock.
   */
  in_stock: boolean | null;
  rating: number | null;
  rating_count: number | null;
  seller: string | null;
  /**
   * Set when the preview could not be read. The response is still HTTP 200 in
   * that case — a preview must never fail the form — so this field, not the
   * status code, is what says the read failed.
   */
  error: string | null;
  /** The product page's "Seller: X": who an order buys from when no seller is
   *  chosen. Optional, like the two below: a preview without them is simply a
   *  preview from before sellers were read. */
  default_seller?: string | null;
  /** Every seller the product's sellers page lists; one entry for a product
   *  with a single seller, none when they could not be read. */
  sellers?: SellerOffer[];
  /** Why the sellers could not be read, when they could not. The rest of the
   *  preview stands, and a seller's name can still be typed. */
  sellers_error?: string | null;
}

/** One seller of a product, as its sellers page lists it. */
export interface SellerOffer {
  name: string;
  price: number | null;
  rating: number | null;
  /** "Cash On Delivery available" on the row; null when the page did not say. */
  cod: boolean | null;
  /** Whether the row offers BUY NOW, the button a chosen seller is bought
   *  through; null when unknown. A row with only Add to cart cannot be chosen
   *  for now — the run stops before anything is pressed. */
  buy_now: boolean | null;
}

/* --- orders --------------------------------------------------------------- */

/**
 * `unconfirmed`: submitted to Flipkart but never confirmed by it, so the order
 * may exist. Someone must check the account before it is retried.
 */
export type OrderStatus =
  | "pending"
  | "placed"
  | "failed"
  | "failed_over_target"
  | "unconfirmed";

/**
 * The ledger's Outcome filter: an order status, or `cancelled` — orders
 * Flipkart took and then cancelled (by Flipkart, or at the account's request;
 * each row says which). `failed` there means never sent to Flipkart.
 */
export type OrderOutcomeFilter = OrderStatus | "cancelled";

/** Who created a unit row: an order run, a read-back, or an uploaded id list. */
export type UnitSource = "run" | "sync" | "upload";

/**
 * One row of the ledger: a UNIT of an order, with its order's fields alongside.
 * `id` is the unit row's; `order_row_id` is the order's, which is what settling
 * and any other order-level action addresses. An order of three units is three
 * rows that share everything but the unit fields.
 */
export interface Order {
  id: number;
  order_row_id: number | null;
  /** 1-based place of this unit in its order, and how many units the order has. */
  unit_position: number;
  units_in_order: number;
  source: UnitSource;
  /** The run's Ref Code, and accounting references filled in on the ledger. */
  ref_code?: string | null;
  po_number?: string | null;
  rtgs_source?: string | null;
  rtgs_beneficiary?: string | null;
  ewb_number?: string | null;
  /** The platform the unit was bought on: "flipkart" for every unit for now. Absent from an older server. */
  platform?: string;
  /** The account the order was placed on — what a read-back of it is started with. */
  account_id?: number;
  account_email: string;
  product_link: string | null;
  qty: number | null;
  model: string | null;
  color: string | null;
  pin: string | null;
  amount: number | null;
  target: number | null;
  order_id: string | null;
  gst_name: string | null;
  /** The GSTIN the run billed the order under, the "Business GST No" beside the
   *  business name; null when the run chose no GST, and for rows read back or uploaded. */
  gstin?: string | null;
  /** The phone the order was placed with (the account's billing phone, else its
   *  own, else the address's); null for rows read back or uploaded. */
  billing_phone?: string | null;
  status: OrderStatus;
  /** Which of the 25 checkout stages the attempt reached, and its classified error code. */
  processing_stage?: string | null;
  error_code?: string | null;
  payment_mode: PaymentMode;
  /** The card the order was paid with, in full (the owner's decision of
   *  25 Oct 2026); null for Cash on Delivery and for rows read back or uploaded. */
  card_number: string | null;
  /** The order page's address block as the read-back saw it (type · address ·
   *  name · phone), and the pincode in it; null until a read-back opened the page. */
  delivery_address: string | null;
  delivery_pincode: string | null;
  /** When a read-back last saw that address replaced by a different one (what
   *  raises the address-change alert); null until that happens. */
  address_changed_at?: string | null;
  /** The GST the order page says the order was billed under (GSTIN and business
   *  name); the GST-mismatch alert compares it with the GST the run chose. */
  billed_gstin: string | null;
  billed_business_name: string | null;
  /** What the order's product invoices say of the unit: its IMEI, and how it
   *  was told — "unit" (the order's only unit), "position" (one of as many
   *  IMEIs as units, by position; the invoice does not name the unit) or
   *  "order" (every IMEI of the order) — and the name the order was billed
   *  to. Null until a product invoice is on file. */
  imei?: string | null;
  imei_scope?: "unit" | "position" | "order" | null;
  billing_name?: string | null;
  /** The newest step of the order page that had happened when it was read. */
  last_tracking_update?: string | null;
  last_tracking_step?: string | null;
  /** An order report's own last tracking update, as written ("25-10-2026
   *  Cancelled"); shown while the page's timeline dates no step. Absent from an older server. */
  last_tracking_text?: string | null;
  /** The courier, as an order report names it (the CSV's "Shipment Partner"). */
  shipment_partner?: string | null;
  /** The refund, as the CSV writes it: its status — Flipkart's own words for a
   *  cancelled unit's refund, else an order report's — and the amount and the
   *  method an order report gave; null when nothing said. */
  refund_status?: string | null;
  refund_amount?: number | null;
  refund_method?: string | null;
  /** The order's amount shared over its units. */
  unit_amount?: number | null;
  /** When a person marked the unit's goods as in hand, and who; null until
   *  someone does (POST /orders/received). */
  received_at?: string | null;
  received_by?: string | null;
  placed_at: string | null;
  /** When this tool first wrote the row — the "Recorded" date. */
  created_at: string | null;
  brand: string | null;
  category: string | null;
  /** The run that attempted it; null for rows a sync imported. */
  job_id: number | null;
  /** What the run said about this attempt, without its verdict ("Cash on
   *  Delivery is not available for this product — nothing was charged"); null
   *  for a placed order and for a row no run recorded. */
  run_message?: string | null;
  /**
   * The server's reading of `platform_status` — delivered, cancelled, returned,
   * failed, in transit or unknown. The badge colours this, never the raw text,
   * so the table, the dashboard and the alerts cannot disagree.
   */
  delivery_state: DeliveryState;

  /* --- filled by a sync run (Phase 10) -----------------------------------
     Every one of these is null until an order has actually been read back, so
     `synced_at` is the field that says whether the others mean "nothing
     happened" or "we have not looked yet" — which is why the table never
     renders them as bare empty cells.
  ---------------------------------------------------------------------- */

  /** The platform's own ids for this unit, parsed from the order's own link. */
  item_id: string | null;
  unit_id: string | null;
  seller: string | null;
  tracking_id: string | null;
  /** Flipkart's own wording: "Delivered", "Out for Delivery", … Kept raw. */
  platform_status: string | null;
  /** The one date Flipkart shows: the promise while on its way, the delivery day once delivered. */
  delivery_date: string | null;
  /** The two kept apart (26 Oct 2026): the day Flipkart promised — the latest
   *  promise read while on its way, kept once delivered — and the day it says
   *  the unit was delivered. Absent from an older server. */
  promised_date?: string | null;
  delivered_date?: string | null;
  /** The first promise read, which Flipkart's later promises for a slipping
   *  delivery do not replace; null on a row read before it was kept, where
   *  `promised_date` is the earliest promise known. */
  first_promised_date?: string | null;
  /** Days the delivery came after the first promise: 0 on time; null while either date is unknown. */
  days_late?: number | null;
  /** How the unit is paid, and its order page's "Total amount": paid, or to be
   *  paid on delivery — or never, for Cash on Delivery that was cancelled. */
  payment_method?: string | null;
  amount_paid?: number | null;
  /** The code the courier asks for at the door. */
  delivery_otp: string | null;
  /** Best effort: older orders show no invoice link at all. */
  invoice_url: string | null;
  /** A PDF is in the invoice store for this order: GET /orders/{order_row_id}/invoice.pdf. */
  /** The order page's timeline as last read back: steps in page order, date null for one still to come. */
  timeline?: { status: string | null; date: string | null }[] | null;
  /** Why the platform cancelled this unit, in its own words ("Sorry, your order
   *  was cancelled as per seller policy restrictions applicable to this brand.");
   *  null until a read-back saw one. */
  cancel_reason?: string | null;
  /** Who cancelled the order, when Flipkart took it and it was cancelled after
   *  (failed, with an order id): "user" when this unit's wording says the
   *  account asked for it, "flipkart" otherwise; null for every other order. */
  cancelled_by?: "flipkart" | "user" | null;
  invoice_on_file: boolean;
  /** How many invoices the order has on file (product, fees, transport…); the row dialog lists them. */
  invoices_on_file?: number;
  /** When this row was last read back. null means never. */
  synced_at: string | null;
  /** Kinds of open cancel / reschedule request on this unit (spec FR-020); empty when none. */
  open_request_kinds: UnitRequestKind[];
}

/** The date a range filter applies to, by the server's field name. */
export type OrderDateField = "placed_at" | "created_at" | "delivery_date" | "synced_at";

/** A Range preset that is a span relative to today: Last 7 days, Last 30 days,
 *  This month. A saved view keeps it (`DATE_PRESETS` in backend/app/api/orders.py). */
export type DatePreset = "7d" | "30d" | "month";

/**
 * The ledger's filter. Every field narrows (AND); lists inside are OR. It is
 * the same filter behind the page, its total, and both exports, so what is
 * downloaded is exactly what was counted.
 */
export interface OrderFilters {
  /** Substring of the product name. */
  q?: string;
  /** The tool's own outcomes. (`status` is the single-value form kept for callers that set one.) */
  status?: OrderOutcomeFilter | "";
  statuses?: OrderOutcomeFilter[];
  /** Exact platform wordings, as ticked in the grouped status picker. */
  platform_statuses?: string[];
  delivery_states?: DeliveryState[];
  /** Checkout pipeline stages reached by the attempt, and classified error codes. */
  processing_stages?: string[];
  error_codes?: string[];
  order_ids?: string[];
  emails?: string[];
  tracking_ids?: string[];
  /** Platforms the unit was bought on ("flipkart"). */
  platforms?: string[];
  /** "BoB Orders": true for only this tool's own orders, false for only the others, unset for both. */
  bob?: boolean | null;
  /** Part of the card number an order was paid with, e.g. its last four digits (spec FR-004). */
  card?: string;
  /**
   * A GSTIN, or its first characters: the GST the order was billed under, as
   * the run chose it or as Flipkart's order page shows it. The server reads it
   * upper-cased with the spaces taken out.
   */
  billed_gst?: string;
  /**
   * Any part of the number of an invoice on file. The three invoice filters,
   * given together, must all hold on one invoice; a matching order brings
   * every one of its units.
   */
  invoice_number?: string;
  /** A GSTIN, or its first characters: the seller or the buyer on an invoice. */
  invoice_gstin?: string;
  /** YYYY-MM: an invoice dated in that month. */
  invoice_month?: string | null;
  account_id?: number | null;
  /** Only the rows one run produced — the deep link from a finished run. */
  job_id?: number | null;
  /** Only one order, by the ledger's own row id — the deep link from an alert
   *  about an order that has no Flipkart order id. */
  order_row?: number | null;
  date_field?: OrderDateField;
  /** YYYY-MM-DD in IST; `date_to` is inclusive. */
  date_from?: string | null;
  date_to?: string | null;
  /**
   * The Range preset the dates were picked with, when they were. Never sent
   * with a request — the dates are the filter — but Quick Save keeps it, so
   * the view reopens on the same span of the day it is opened (spec FR-011).
   */
  date_preset?: DatePreset | null;
  /** 1-based page; 100 rows per page unless `size` says otherwise (max 500). */
  page?: number;
  size?: number;
}

export interface CountedValue {
  value: string;
  count: number;
}

export interface PlatformStatusOption extends CountedValue {
  /** The portal's reading of this wording — what the picker groups by. */
  state: DeliveryState;
}

/** GET /orders/filter-options: the values present, with counts. */
export interface OrderFilterOptions {
  statuses: CountedValue[];
  platform_statuses: PlatformStatusOption[];
  delivery_states: CountedValue[];
  processing_stages?: CountedValue[];
  error_codes?: CountedValue[];
  total: number;
}

/** The saved ledger view: visible columns and the filters applied. */
export interface OrderView {
  columns: string[];
  filters: Partial<OrderFilters>;
  saved_at: string | null;
}

/** One page of orders plus the FULL match count, for "Showing N of M". */
export interface OrderPage {
  items: Order[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

/** The portal's one reading of a platform status (server-computed). */
export type DeliveryState =
  | "delivered"
  | "cancelled"
  | "returned"
  | "failed"
  | "in_transit"
  | "unknown";

/* --- alerts --------------------------------------------------------------
   The queue of things needing a human, modelled on the operator's existing
   "Needs attention — 443 open" list. An alert is raised once under a stable
   `key`, and is RESOLVED rather than deleted: the same problem coming back
   reopens the same row instead of arriving as new.
-------------------------------------------------------------------------- */

export const ALERT_SEVERITIES = ["info", "warning", "critical"] as const;

export type AlertSeverity = (typeof ALERT_SEVERITIES)[number];

export interface Alert {
  id: number;
  /** Stable identity, e.g. "address_mismatch:order:412". Unique. */
  key: string;
  /** The rule that raised it, e.g. "address_mismatch". Free text. */
  kind: string;
  severity: AlertSeverity;
  message: string;
  account_id: number | null;
  /**
   * Filled by GET /alerts from one lookup over the page's account ids, so a
   * row names the account. Optional because the account row can be gone, in
   * which case the UI falls back to "Account #12".
   */
  account_email?: string | null;
  /** What the operator wrote when closing it by hand; null for a rule's close.
   *  Cleared when the alert reopens. */
  resolution_note?: string | null;
  /** Who closed it: "user" or "rule". A user's close is an acceptance — the
   *  rules leave it closed while the problem persists, and reopen it only once
   *  the problem has gone away and come back. */
  resolved_by?: "user" | "rule" | null;
  /** When a rules pass last reported the problem. `updated_at` moves only when
   *  something about the alert changed. */
  last_seen_at?: string | null;
  order_id: number | null;
  /** Whatever the rule recorded — shape is per-kind, so it stays unknown. */
  details: Record<string, unknown> | null;
  resolved: boolean;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
}

/** GET /alerts — the queue, most recently moved first. */
export interface AlertFilters {
  severity?: AlertSeverity | "";
  kind?: string;
  /**
   * false is the open queue, true the closed rows, "any" both.
   *
   * Leaving it out is NOT "both": the server's own default is the open queue,
   * because that is the only question the page is opened to ask. "any" has to
   * be asked for.
   */
  resolved?: boolean | "any";
  /** "updated" (the default): most recently changed first. "created": most
   *  recently raised first — the dashboard's "latest". */
  order?: "updated" | "created";
  limit?: number;
}

/**
 * GET /alerts/summary — how many are open, and how bad.
 *
 * Both breakdowns count OPEN alerts only; `resolved` is the lifetime total of
 * closed rows. `by_severity` carries all three severities today, zeros
 * included — it is still read through `openCount()` so a severity that ever
 * stops being zero-filled cannot turn a count into NaN. `by_kind` carries only
 * the kinds that actually have something open, because kind is free text.
 */
export interface AlertSummary {
  /** Open (unresolved) alerts, every severity together. */
  open: number;
  /** Closed rows, for all time. */
  resolved: number;
  by_severity: Partial<Record<AlertSeverity, number>>;
  by_kind: Record<string, number>;
}

/** POST /alerts/evaluate — re-run every rule now. */
export interface AlertEvaluateResult {
  /**
   * Open alerts per rule kind once the pass finished — every registered kind,
   * zeros included, so a stable set of counters can be rendered.
   */
  counts: Record<string, number>;
  /**
   * Open alerts in the whole queue afterwards. Not the sum of `counts`: an
   * ad-hoc alert raised by a sync job has no rule kind of its own.
   */
  open: number;
}

/* --- stats ---------------------------------------------------------------- */

/** GET /stats/to-be-delivered: units placed and not yet delivered, by product. */
export interface ToBeDeliveredModel {
  brand: string | null;
  model: string | null;
  units: number;
  orders: number;
  oldest_ordered_at: string | null;
}

/** GET /stats/out-for-delivery: one unit Flipkart says is on the courier's van. */
export interface OutForDeliveryUnit {
  unit_pk: number;
  order_row_id: number;
  order_id: string | null;
  unit_id: string | null;
  position: number;
  account_id: number;
  account_email: string | null;
  model: string | null;
  color: string | null;
  platform_status: string | null;
  /** The code the courier asks for at the door; null until a read-back saw it. */
  delivery_otp: string | null;
  delivery_date: string | null;
  delivery_address: string | null;
  delivery_pincode: string | null;
  tracking_id: string | null;
  synced_at: string | null;
}

export interface OutForDelivery {
  total: number;
  units: OutForDeliveryUnit[];
  /** What "Read back now" starts a read-back over: the accounts holding these units. */
  account_ids: number[];
  order_ids: string[];
  last_read_back: string | null;
}

export interface ToBeDelivered {
  total_units: number;
  total_orders: number;
  models: ToBeDeliveredModel[];
  older_than_days: number | null;
}

export interface Stats {
  accounts: number;
  sessions_valid: number;
  coupons_applied: number;
  orders_placed: number;
  orders_over_target: number;
  /** Failed before anything reached Flipkart: the run's own failures only. */
  orders_failed: number;
  /** Taken by Flipkart (they carry its order id) and cancelled there later,
   *  by Flipkart or its seller. */
  orders_cancelled: number;
  /** Taken by Flipkart and cancelled at the account's own request (its
   *  wording says so, or the tool holds a cancel request for it). */
  orders_cancelled_by_user?: number;
  orders_unconfirmed: number;
  jobs_running: number;
}

/* --- live progress (WebSocket /ws/jobs/{id}) ------------------------------ */

export type JobStreamMessageType =
  | "snapshot"
  | "job_started"
  | "item_started"
  | "item_finished"
  | "job_finished";

export interface JobStreamMessage {
  type: JobStreamMessageType;
  job_id?: number;
  /** present on "snapshot" */
  job?: Job;
  /** present on "snapshot" */
  items?: JobItem[];
  account_id?: number;
  ok?: boolean;
  /** present on "item_finished": the account was passed over, not failed. */
  skipped?: boolean;
  /** present on "item_finished": an order was sent but never confirmed. */
  unconfirmed?: boolean;
  /** present on "item_finished": seconds the run spent on the account, or
   *  null for one written off without being started. */
  seconds?: number | null;
  /** present on "job_finished": how the run ended — "completed" or "failed". */
  status?: string;
  message?: string | null;
  progress?: number;
}

/** Result of probing the OTP inbox — see POST /settings/otp-inbox/test. */
export interface OtpInboxTestResult {
  ok: boolean;
  message: string;
  message_count: number | null;
}

/* --- cancel / reschedule requests (spec FR-020, decision C-4) --------------
   A request is a row for a person to action on Flipkart by hand; the app
   sends nothing to the platform. A unit holds at most one OPEN request per
   kind; a done or dropped one can be followed by a fresh request.
-------------------------------------------------------------------------- */

export type UnitRequestKind = "cancel" | "reschedule";
export type UnitRequestStatus = "open" | "done" | "dropped";

/** One step of a request's life: the status it left it at, who, when, the note. */
export interface UnitRequestStep {
  status: UnitRequestStatus;
  actor: string;
  at: string;
  note: string | null;
}

export interface UnitRequest {
  id: number;
  /** The ledger row (`Order.id`) the request is about. */
  unit_pk: number;
  kind: UnitRequestKind;
  /** Why. A cancel always has one; a reschedule may. */
  reason: string | null;
  /** The wanted new delivery date (YYYY-MM-DD); a reschedule always has one. */
  requested_for: string | null;
  status: UnitRequestStatus;
  /** The signed-in user who asked, by email. */
  requested_by: string;
  requested_at: string;
  closed_by: string | null;
  closed_at: string | null;
  /** What the person wrote when marking it done or dropping it. */
  note: string | null;
  /** Every step so far, oldest first: opened, marked done or dropped, opened
   *  again (spec C-4's status log). `closed_by`/`closed_at`/`note` say only the last. */
  history?: UnitRequestStep[];

  /* --- the unit's, so a row reads on its own ----------------------------- */
  order_row_id: number | null;
  order_id: string | null;
  /** The platform's unit number, when a read-back has seen it. */
  unit_id: string | null;
  /** The order's product name. */
  product: string | null;
  platform_status: string | null;
  delivery_state: DeliveryState;
  account_email: string | null;
}

/** POST /requests: the same request on every unit in the list. */
export interface UnitRequestCreateInput {
  /** Ledger row ids (`Order.id`), at most 500. */
  unit_ids: number[];
  kind: UnitRequestKind;
  /** Required for a cancel. */
  reason?: string;
  /** YYYY-MM-DD; required for a reschedule. */
  requested_for?: string;
}

/** What a bulk action did: the rows recorded, and each unit passed over with the reason. */
export interface UnitRequestCreateResult {
  created: UnitRequest[];
  skipped: { unit_id: number; why: string }[];
}

/** GET /requests. An absent `status` is the open queue on the server; "all" has to be asked for. */
export interface UnitRequestFilters {
  status?: UnitRequestStatus | "all";
  kind?: UnitRequestKind | "";
  page?: number;
  size?: number;
}

export interface UnitRequestPage {
  items: UnitRequest[];
  total: number;
}

/* --- what changed since you last looked (GET /changes) ----------------------
   One row per change a read-back saw in a unit's delivery state. The list is
   per user: what came after the signed-in user's own "seen" mark — the last
   7 days without one, never more than 30 days back.
-------------------------------------------------------------------------- */

/** What happened to the unit. */
export type ChangeKind = "delivered" | "out_for_delivery" | "cancelled" | "returned" | "failed";

/** A unit's reading on either side of a change: a delivery state, or on the courier's van. */
export type ChangeState = DeliveryState | "out_for_delivery";

export interface UnitChange {
  id: number;
  /** When the read-back saw it. */
  at: string;
  kind: ChangeKind;
  /** null for a unit's first reading: an order this tool placed, first seen already settled. */
  from_state: ChangeState | null;
  to_state: ChangeState;
  /** The ledger row (`Order.id`) the change is about. */
  unit_pk: number;
  unit_position: number | null;
  unit_id: string | null;
  /** The order row (`Order.order_row_id`), and the platform's order id the ledger link filters by. */
  order_row_id: number;
  order_id: string | null;
  model: string | null;
  color: string | null;
  account_id: number | null;
  account_email: string | null;
}

export interface ChangesPage {
  items: UnitChange[];
  /** Every change in the window, across all pages: the nav badge's number. */
  total: number;
  page: number;
  size: number;
  pages: number;
  /** The window's changes by kind, every kind listed (zeros included). */
  by_kind: Record<string, number>;
  /** Where the window starts: your seen mark, else 7 days back. */
  since: string;
  /** Your seen mark; null until you first mark the list as seen. */
  seen_at: string | null;
  /** True when the window was cut at 30 days back. */
  capped: boolean;
  /** The server's clock when this was read — what "Mark all as seen" sends back,
   *  so a change that arrives while the page is open stays unseen. */
  as_of: string;
}

/** POST /changes/seen: your mark as it now stands. */
export interface ChangesSeen {
  seen_at: string;
}

/** POST /orders/received: how many units the mark changed, and ids no unit has. */
export interface OrderReceivedResult {
  changed: number;
  missing: number[];
}

/** GET /stats/logins: accounts by login status (step G). */
export interface LoginHealth {
  platform: string;
  total: number;
  logged_in: number;
  /** Every status, in a fixed order, at zero when no account has it. */
  statuses: { status: string; count: number }[];
  /** One row per platform: the dashboard's login table. Absent from an older server. */
  platforms?: {
    platform: string;
    logged_in: number;
    logged_out: number;
    not_registered: number;
    multiple_attempts: number;
    blocked: number;
    other: number;
    total: number;
  }[];
}

/** The dashboard's account scope: "All Platforms", "Custom tags", "Domains". */
export interface DashboardScope {
  platform?: string;
  tag?: number | "untagged" | null;
  domain?: string;
}

/** A gift voucher on the register (never its full code). */
export interface GiftVoucher {
  id: number;
  label: string;
  code_last4: string | null;
  value: number;
  balance: number;
  account_id: number | null;
  account_email: string | null;
  expires_on: string | null;
  created_at: string;
}

/** An HDFC CorpAssist user id (GET /hdfc/user-ids). */
export interface HdfcUserId {
  id: number;
  username: string;
  email: string;
  mobile: string | null;
  parent_card_last4: string | null;
  active: boolean;
  last_fetched_at: string | null;
  last_error: string | null;
  card_count: number;
  created_at: string;
}

/** One user id's Fetch result. */
export interface HdfcFetchResult {
  user_id: number;
  username: string;
  emails: number;
  added: number;
  error: string | null;
}

export type HdfcCardStatus = "active" | "cancelled";

/** A child card read from an HDFC email. Used is from this tool's own orders. */
export interface HdfcChildCard {
  id: number;
  user_id: number;
  username: string;
  email: string;
  card_number: string;
  parent_card_last4: string | null;
  opening_amount: number | null;
  used: number;
  remaining: number | null;
  status: HdfcCardStatus;
  error: string | null;
  issued_at: string | null;
  created_at: string;
}

export interface HdfcCardFilters {
  user_id?: number | null;
  status?: HdfcCardStatus | "";
  min_remaining?: string;
  created_from?: string;
  created_to?: string;
  search?: string;
}

/** A number a run may put on the delivery address. */
export interface OrderPhoneNumber {
  id: number;
  number: string;
  label: string | null;
  active: boolean;
  created_at: string;
}

/** GET /stats/balances. */
export interface BalanceTotals {
  supercoins_balance: number;
  gift_voucher_balance: number;
  accounts_read: number;
  supercoins_orders_delivered: number;
  /** Rupees SuperCoins took off delivered orders. Older servers leave it out. */
  supercoins_applied_amount?: number;
}

/** GET /agents (step H). */
export interface AgentsStatus {
  api: {
    online: boolean;
    max_concurrency: number;
    contexts_per_browser: number;
    jobs_running: number;
    jobs_queued: number;
  };
  queue: {
    configured: boolean;
    reachable: boolean | null;
    health: string | null;
    error: string | null;
  };
}
