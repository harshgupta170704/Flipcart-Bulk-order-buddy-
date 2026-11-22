/**
 * The only module that talks to the server.
 *
 * Everything goes to the RELATIVE base `/api` (Vite dev-proxies it, nginx
 * proxies it in production — in both cases the `/api` prefix is stripped before
 * the request reaches FastAPI). Never hardcode a host here.
 */
import type {
  AgentsStatus,
  LoginHealth, OrderInvoice, OrderReceivedResult, OrderReportImportResult } from "./types";
import { usesCard, usesGiftCards } from "./types";
import type {
  Account,
  AccountFilterInput,
  CardPreview,
  AccountInput,
  AccountPage,
  AccountPatch,
  Address,
  AddressInput,
  AddressPatch,
  Alert,
  AlertEvaluateResult,
  AlertFilters,
  AlertSummary,
  AppSettings,
  AppUser,
  AuthOptions,
  ChangesPage,
  ChangesSeen,
  CompileStartResult,
  CompileStatus,
  CompiledArchivePage,
  CouponJobInput,
  FilterOptions,
  Gst,
  GstAssignInput,
  GstAssignResult,
  GstDeleted,
  GstInput,
  GstPatch,
  ImportResult,
  InvoiceDataDownload,
  InvoiceTypes,
  InvoiceUploadResult,
  BulkOrderRun,
  BulkOrderRunFilters,
  BalanceTotals,
  DashboardScope,
  FleetStatus,
  GiftVoucher,
  HdfcBatch,
  HdfcBatchRowInput,
  HdfcBatchSend,
  HdfcCardFilters,
  HdfcChildCard,
  HdfcFetchResult,
  HdfcPortalStatus,
  HdfcPortalSyncResult,
  HdfcPortalTestResult,
  HdfcUserId,
  OtpPhone,
  OtpPhoneBulkResult,
  OtpPhoneCreated,
  OtpPhoneMessage,
  OtpPhoneSettings,
  OrderPhoneNumber,
  ScreenerConfig,
  ScreenerItem,
  InventoryBatch,
  InventoryStockRow,
  InventoryTransfer,
  CodCardsUpdateResult,
  Job,
  JobDetail,
  JobTaskPage,
  LimitCheckInput,
  LimitUsage,
  LoginJobInput,
  LoginResponse,
  MeResponse,
  Order,
  OrderFilterOptions,
  OrderFilters,
  OrderIdsImportResult,
  OrderJobInput,
  OrderPage,
  OrderView,
  OtpInboxTestResult,
  OutForDelivery,
  PagedAccountsParams,
  PasswordChangedResponse,
  ProductPreview,
  ProductSearchAnswer,
  SettingsUpdate,
  Stats,
  SyncJobInput,
  Tag,
  TagAccountsInput,
  TagResult,
  ToBeDelivered,
  UnitLimit,
  UnitLimitInput,
  UnitLimitPatch,
  UnitRequest,
  UnitRequestCreateInput,
  UnitRequestCreateResult,
  UnitRequestFilters,
  UnitRequestPage,
  UnitRequestStatus,
  UserCreateInput,
  UserPatchInput,
} from "./types";

const BASE = "/api";

export const TOKEN_KEY = "ordering.token";

/** Dispatched on window whenever the server rejects our token (401/403). */
export const UNAUTHORIZED_EVENT = "ordering:unauthorized";

/* --- token ---------------------------------------------------------------- */

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* private mode / blocked storage: the session simply won't survive reload */
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/* --- errors --------------------------------------------------------------- */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function locToField(loc: unknown): string {
  if (!Array.isArray(loc)) return "";
  const parts = loc
    .filter((p): p is string | number => typeof p === "string" || typeof p === "number")
    .filter((p) => p !== "body" && p !== "query" && p !== "path");
  return parts.join(".");
}

/**
 * FastAPI's `detail` is either a plain string or an array of validation
 * objects. Turn either one into a sentence a person can read.
 */
export function humanizeDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim() !== "") return detail;

  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry): string => {
        if (typeof entry === "string") return entry;
        if (entry && typeof entry === "object") {
          const rec = entry as Record<string, unknown>;
          const msg = typeof rec.msg === "string" ? rec.msg : "";
          const field = locToField(rec.loc);
          if (msg && field) return `${field}: ${msg}`;
          if (msg) return msg;
        }
        return "";
      })
      .filter((s) => s !== "");
    if (parts.length > 0) return parts.join("; ");
  }

  if (detail && typeof detail === "object") {
    const rec = detail as Record<string, unknown>;
    if (typeof rec.message === "string" && rec.message.trim() !== "") return rec.message;
    if (typeof rec.msg === "string" && rec.msg.trim() !== "") return rec.msg;
  }

  return fallback;
}

/** Extract a readable message from an unknown thrown value. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "Something went wrong.";
}

/* --- core request --------------------------------------------------------- */

function authHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

/**
 * 401 means the token is missing, expired or bad — the server answers that for
 * every such case (security.py uses its own handler, not HTTPBearer's 403) —
 * so sign out. 403 means "signed in, but not allowed to do this": once routes
 * are role-gated, an operator hitting an admin-only route must stay signed in.
 */
function handleUnauthorized(status: number): void {
  if (status !== 401) return;
  clearToken();
  try {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  } catch {
    /* ignore */
  }
}

async function failureFrom(res: Response): Promise<ApiError> {
  const fallback = res.statusText ? `${res.status} ${res.statusText}` : `Request failed (${res.status})`;
  let detail: unknown;
  try {
    const text = await res.text();
    if (text) {
      try {
        const parsed: unknown = JSON.parse(text);
        detail =
          parsed && typeof parsed === "object" && "detail" in (parsed as Record<string, unknown>)
            ? (parsed as Record<string, unknown>).detail
            : parsed;
      } catch {
        // Not JSON (an nginx error page, say) — don't surface raw HTML.
        detail = text.trimStart().startsWith("<") ? undefined : text;
      }
    }
  } catch {
    /* body already consumed or unreadable */
  }
  return new ApiError(res.status, humanizeDetail(detail, fallback), detail);
}

/**
 * Core fetch. Attaches the bearer token, sets a JSON content-type only when
 * there really is a JSON body (never for FormData), parses the JSON response,
 * and throws an ApiError carrying a readable message when the call fails.
 */
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = authHeaders(init?.headers);
  const body = init?.body;
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;

  if (body !== undefined && body !== null && !isForm && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!headers.has("Accept")) headers.set("Accept", "application/json");

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check that it is running and try again.");
  }

  if (!res.ok) {
    handleUnauthorized(res.status);
    throw await failureFrom(res);
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(res.status, "The server sent a response this app could not read.");
  }
}

/* --- xlsx downloads ------------------------------------------------------- */

function filenameFrom(res: Response, fallback: string): string {
  const header = res.headers.get("Content-Disposition") ?? "";
  const utf8 = /filename\*=UTF-8''([^;\n]+)/i.exec(header);
  if (utf8?.[1]) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="?([^";\n]+)"?/i.exec(header);
  if (plain?.[1]) return plain[1];
  return fallback;
}

/**
 * A plain <a href> cannot carry the bearer token, so downloads are fetched,
 * read as a blob and handed to a throwaway object-URL anchor.
 */
async function downloadFile(path: string, fallbackName: string): Promise<void> {
  await saveAsFile(await fetchDownload(path), fallbackName);
}

/** The download's response, with the bearer attached and a failure thrown. */
async function fetchDownload(path: string): Promise<Response> {
  const headers = authHeaders();
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { headers });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check that it is running and try again.");
  }
  if (!res.ok) {
    handleUnauthorized(res.status);
    throw await failureFrom(res);
  }
  return res;
}

/** Hand the body to the browser as a file named by the response, else `fallbackName`. */
async function saveAsFile(res: Response, fallbackName: string): Promise<void> {
  let blob: Blob;
  try {
    blob = await res.blob();
  } catch {
    // The answer started but broke off: a server error part-way through a
    // streamed export, or a dropped connection. Nothing has been saved yet.
    throw new ApiError(0, "The download stopped part-way, so nothing was saved. Try again.");
  }
  const name = filenameFrom(res, fallbackName);
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    anchor.rel = "noopener";
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    // Give the browser a tick to start the download before revoking.
    window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
}

/* --- auth ----------------------------------------------------------------- */

export function login(username: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function me(): Promise<MeResponse> {
  return request<MeResponse>("/auth/me");
}

/** What the sign-in page may offer; public, so it works before any token. */
export function authOptions(): Promise<AuthOptions> {
  return request<AuthOptions>("/auth/options");
}

/**
 * A password of one's own. A wrong current password is a 403, which keeps the
 * session. Success ends every other session of the user — and the one that
 * asked — so the answer carries a fresh token to carry on with.
 */
export function changePassword(currentPassword: string, newPassword: string): Promise<PasswordChangedResponse> {
  return request<PasswordChangedResponse>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}

/** Ask for a reset link by mail. Always answers the same, whoever the email belongs to. */
export function forgotPassword(email: string): Promise<{ detail: string }> {
  return request<{ detail: string }>("/auth/forgot-password", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

/** Choose a new password with the code from the mail's link; signs the user in. */
export function resetPassword(token: string, newPassword: string): Promise<LoginResponse> {
  return request<LoginResponse>("/auth/reset-password", {
    method: "POST",
    body: JSON.stringify({ token, new_password: newPassword }),
  });
}

/* --- users (admin only) --------------------------------------------------- */

export function listUsers(): Promise<AppUser[]> {
  return request<AppUser[]>("/users");
}

export function createUser(input: UserCreateInput): Promise<AppUser> {
  return request<AppUser>("/users", { method: "POST", body: JSON.stringify(input) });
}

export function updateUser(id: number, patch: UserPatchInput): Promise<AppUser> {
  return request<AppUser>(`/users/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

/** A temporary password the user must change at their next sign-in. */
export function resetUserPassword(id: number, temporaryPassword: string): Promise<AppUser> {
  return request<AppUser>(`/users/${id}/reset-password`, {
    method: "POST",
    body: JSON.stringify({ temporary_password: temporaryPassword }),
  });
}

/* --- accounts ------------------------------------------------------------- */

/** The server's own bounds — asking for more than MAX just gets rejected. */
export const ACCOUNTS_DEFAULT_SIZE = 50;
export const ACCOUNTS_MAX_SIZE = 500;

function clampSize(size: number | undefined): number {
  if (size === undefined || !Number.isFinite(size)) return ACCOUNTS_DEFAULT_SIZE;
  return Math.min(ACCOUNTS_MAX_SIZE, Math.max(1, Math.trunc(size)));
}

function accountsQuery(params: PagedAccountsParams): string {
  const qs = new URLSearchParams();
  qs.set("page", String(Math.max(1, Math.trunc(params.page ?? 1))));
  qs.set("size", String(clampSize(params.size)));

  const q = params.q?.trim();
  if (q) qs.set("q", q);
  for (const status of params.statuses ?? []) {
    if (status) qs.append("status", status);
  }
  for (const tagId of params.tag_ids ?? []) {
    qs.append("tag_id", String(tagId));
  }
  if (params.untagged) qs.set("untagged", "true");
  for (const subscription of params.subscriptions ?? []) {
    if (subscription) qs.append("subscription", subscription);
  }
  for (const domain of params.domains ?? []) {
    if (domain) qs.append("domain", domain);
  }

  qs.set("sort", params.sort ?? "id");
  qs.set("desc", params.desc ? "true" : "false");
  return qs.toString();
}

/**
 * One page of accounts. Paged, never the whole table: the real portal holds
 * 37,062 of them, so an unpaged fetch is both a multi-megabyte response and a
 * frozen tab.
 */
export function listAccounts(params: PagedAccountsParams = {}): Promise<AccountPage> {
  return request<AccountPage>(`/accounts?${accountsQuery(params)}`);
}

/** The same narrowing, as GET query params. `ids` has no query form. */
export function filterToParams(filter: AccountFilterInput): PagedAccountsParams {
  return {
    q: filter.q,
    statuses: filter.statuses,
    tag_ids: filter.tag_ids,
    untagged: filter.include_untagged,
    subscriptions: filter.subscriptions,
    domains: filter.domains,
  };
}

/**
 * Resolve a filter down to concrete ids by paging the server.
 *
 * Only for the run pages that can carry a list of ids but not a filter. It
 * stops at `cap` so a broad filter can never pull the whole table into the
 * tab, and reports `truncated` so the caller refuses rather than quietly
 * running a partial set.
 */
export async function listAccountIds(
  filter: AccountFilterInput,
  cap: number,
): Promise<{ ids: number[]; total: number; truncated: boolean }> {
  const base = filterToParams(filter);
  const ids: number[] = [];
  let total = 0;
  let page = 1;
  // cap/size bounds the loop; the page ceiling is belt-and-braces against a
  // server that keeps reporting more pages than it fills.
  const maxPages = Math.ceil(Math.max(1, cap) / ACCOUNTS_MAX_SIZE) + 1;

  while (page <= maxPages) {
    const result = await listAccounts({ ...base, page, size: ACCOUNTS_MAX_SIZE, sort: "id" });
    total = result.total;
    for (const account of result.items) ids.push(account.id);
    if (result.items.length === 0 || ids.length >= cap || page >= result.pages) break;
    page += 1;
  }

  return { ids: ids.slice(0, cap), total, truncated: total > cap };
}

export function getFilterOptions(): Promise<FilterOptions> {
  return request<FilterOptions>("/accounts/filter-options");
}

export function createAccount(input: AccountInput): Promise<Account> {
  return request<Account>("/accounts", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function patchAccount(id: number, patch: AccountPatch): Promise<Account> {
  return request<Account>(`/accounts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/**
 * Bulk tagging. Pass `filter` for "every account matching this", `account_ids`
 * for a handful — never 37,000 ids.
 */
export function tagAccounts(input: TagAccountsInput): Promise<TagResult> {
  return request<TagResult>("/accounts/tag", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Give a selection of accounts their own GST (`gst_id: null` takes it off).
 * Like bulk tagging: `filter` for "every account matching this", `account_ids`
 * for a handful.
 */
export function assignAccountsGst(input: GstAssignInput): Promise<GstAssignResult> {
  return request<GstAssignResult>("/accounts/gst", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function importAccounts(file: File): Promise<ImportResult> {
  const form = new FormData();
  form.append("file", file);
  return request<ImportResult>("/accounts/import", { method: "POST", body: form });
}

export function downloadAccounts(): Promise<void> {
  return downloadFile("/accounts/export", "accounts.xlsx");
}

/* --- tags ----------------------------------------------------------------- */

export function listTags(): Promise<Tag[]> {
  return request<Tag[]>("/tags");
}

export function createTag(name: string): Promise<Tag> {
  return request<Tag>("/tags", { method: "POST", body: JSON.stringify({ name }) });
}

export function renameTag(id: number, name: string): Promise<Tag> {
  return request<Tag>(`/tags/${id}`, { method: "PATCH", body: JSON.stringify({ name }) });
}

/** Accounts holding the tag become untagged — they are never deleted. */
export function deleteTag(id: number): Promise<void> {
  return request<void>(`/tags/${id}`, { method: "DELETE" });
}

/* --- addresses & GSTs ----------------------------------------------------
   Two short, hand-managed lists — one row per business entity, not per order
   — so both are fetched whole. No paging here, and none is wanted: the whole
   point is that an operator can see every address a run could ship to at once.
-------------------------------------------------------------------------- */

/** Alphabetical by label, the way the server sorts them. */
export function listGsts(): Promise<Gst[]> {
  return request<Gst[]>("/gsts");
}

export function createGst(input: GstInput): Promise<Gst> {
  return request<Gst>("/gsts", { method: "POST", body: JSON.stringify(input) });
}

export function updateGst(id: number, patch: GstPatch): Promise<Gst> {
  return request<Gst>(`/gsts/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

/** Addresses and accounts linked to it are kept — they simply become
 *  unlinked — and the answer says how many were, as the server counted them. */
export function deleteGst(id: number): Promise<GstDeleted> {
  return request<GstDeleted>(`/gsts/${id}`, { method: "DELETE" });
}

export function listAddresses(): Promise<Address[]> {
  return request<Address[]>("/addresses");
}

export function createAddress(input: AddressInput): Promise<Address> {
  return request<Address>("/addresses", { method: "POST", body: JSON.stringify(input) });
}

export function updateAddress(id: number, patch: AddressPatch): Promise<Address> {
  return request<Address>(`/addresses/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function deleteAddress(id: number): Promise<void> {
  return request<void>(`/addresses/${id}`, { method: "DELETE" });
}

/* --- unit limits ---------------------------------------------------------
   A short, hand-managed list of platform caps (20 handsets / 31 days, 16 vivo
   / 31 days, …), so it is fetched whole like the addresses are. The check call
   is the one that matters: it says what the accounts of a run have left BEFORE
   the run spends their quota.
-------------------------------------------------------------------------- */

/** Sorted by scope, then value, the way the server returns them. */
export function listUnitLimits(): Promise<UnitLimit[]> {
  return request<UnitLimit[]>("/unit-limits");
}

export function createUnitLimit(input: UnitLimitInput): Promise<UnitLimit> {
  return request<UnitLimit>("/unit-limits", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateUnitLimit(id: number, patch: UnitLimitPatch): Promise<UnitLimit> {
  return request<UnitLimit>(`/unit-limits/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteUnitLimit(id: number): Promise<void> {
  return request<void>(`/unit-limits/${id}`, { method: "DELETE" });
}

/**
 * What the given accounts have left under every limit matching these product
 * facts. One row per applicable limit; an empty array means nothing caps this
 * product, so no account can be blocked by a limit.
 */
export function checkUnitLimits(input: LimitCheckInput): Promise<LimitUsage[]> {
  return request<LimitUsage[]>("/unit-limits/check", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/* --- settings ------------------------------------------------------------- */

export function getSettings(): Promise<AppSettings> {
  return request<AppSettings>("/settings");
}

export function updateSettings(patch: SettingsUpdate): Promise<AppSettings> {
  return request<AppSettings>("/settings", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

/* --- jobs ----------------------------------------------------------------- */

export function startLoginJob(input: LoginJobInput): Promise<Job> {
  return request<Job>("/jobs/login", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** Read each selected account's SuperCoins and gift card balance (read-only on Flipkart). */
export function startBalancesJob(input: LoginJobInput): Promise<Job> {
  return request<Job>("/jobs/balances", { method: "POST", body: JSON.stringify(input) });
}

export function startCouponJob(input: CouponJobInput): Promise<Job> {
  return request<Job>("/jobs/coupon", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Append a number only when there is one.
 *
 * An unset field has to be ABSENT, not empty: the server reads absence as "not
 * set" and falls back to the single-order behaviour, while an empty string is a
 * 422 on an int field.
 */
function appendIfSet(form: FormData, name: string, value: number | null | undefined): void {
  if (value === undefined || value === null || !Number.isFinite(value)) return;
  form.append(name, String(value));
}

/** Same, for the free-text facts: a blank brand is no brand at all. */
function appendIfText(form: FormData, name: string, value: string | null | undefined): void {
  const text = value?.trim() ?? "";
  if (text !== "") form.append(name, text);
}

/**
 * Multipart, because the corporate-card sheet rides along. The file is sent
 * straight through and never kept anywhere on the client.
 */
export function startOrderJob(input: OrderJobInput): Promise<Job> {
  const form = new FormData();
  // Only when it differs from the server's default, so a real "upto" run is
  // sent exactly as before these options existed.
  if (input.mode && input.mode !== "real") form.append("mode", input.mode);
  if (input.mode === "ai" && input.ai) {
    form.append("ai_max_interval_minutes", String(input.ai.max_interval_minutes));
    form.append("ai_min_attempts", String(input.ai.min_attempts));
    form.append("ai_scale_up_pct", String(input.ai.scale_up_pct));
    if (input.ai.scale_up_mode && input.ai.scale_up_mode !== "percent") {
      form.append("ai_scale_up_mode", input.ai.scale_up_mode);
    }
    if (input.ai.scale_up_count !== undefined && input.ai.scale_up_count !== null) {
      form.append("ai_scale_up_count", String(input.ai.scale_up_count));
    }
    form.append("ai_run_duration", input.ai.run_duration);
    if (input.ai.single_probe) form.append("ai_single_probe", "true");
    if (input.ai.recycle_accounts) form.append("ai_recycle_accounts", "true");
    if (input.ai.rehearsal) form.append("ai_rehearsal", "true");
  }
  if (input.qty_mode && input.qty_mode !== "upto") form.append("qty_mode", input.qty_mode);
  if (input.gst_mandatory) form.append("gst_mandatory", "true");
  appendIfSet(form, "exclude_ordered_days", input.exclude_ordered_days);
  if (input.smart_selection) form.append("smart_selection", "true");
  if (input.ref_code && input.ref_code.trim()) form.append("ref_code", input.ref_code.trim());
  if (input.execution_version) form.append("execution_version", input.execution_version);
  if (input.isolated_agents) form.append("isolated_agents", "true");
  if (input.use_supercoins) {
    form.append("use_supercoins", "true");
    appendIfSet(form, "min_supercoins", input.min_supercoins);
  }
  if (input.use_wallet) form.append("use_wallet", "true");
  if (input.apply_additional_offer) form.append("apply_additional_offer", "true");
  if (usesCard(input.payment_mode)) {
    appendIfSet(form, "card_cooldown_days", input.card_cooldown_days);
    appendIfSet(form, "card_least_used_days", input.card_least_used_days);
    appendIfSet(form, "card_max_uses", input.card_max_uses);
    appendIfSet(form, "card_exclude_failed_days", input.card_exclude_failed_days);
    appendIfSet(form, "card_smart_max_uses", input.card_smart_max_uses);
    appendIfSet(form, "card_min_remaining", input.card_min_remaining);
    if (input.card_exclude_failed_codes && input.card_exclude_failed_codes.length > 0) {
      form.append("card_exclude_failed_codes", input.card_exclude_failed_codes.join(","));
    }
    if (input.card_type && input.card_type.trim()) form.append("card_type", input.card_type.trim());
  }
  if (input.address_phones && input.address_phones.length > 0) {
    form.append("address_phones", input.address_phones.join(","));
  } else if (input.use_address_phone) {
    form.append("use_address_phone", "true");
  }
  form.append("product_link", input.product_link);
  form.append("qty", String(input.qty));
  // A run states its price cap or says on purpose that it has none, and the
  // server refuses neither and both (audit F-23): the target goes only when
  // there is one, and no_cap only when "No price cap" was ticked.
  appendIfSet(form, "target", input.target);
  if (input.no_cap) form.append("no_cap", "true");
  form.append("account_ids", input.account_ids === null ? "" : input.account_ids.join(","));
  // A filter travels as JSON text in the multipart body, and is omitted rather
  // than sent empty so the server falls through to account_ids as documented.
  if (input.account_filter !== undefined) {
    form.append("account_filter", JSON.stringify(input.account_filter));
  }
  form.append("payment_mode", input.payment_mode);
  // Omitted rather than sent empty: an absent address_id means the Address
  // Book's default (and a 422 when there is none), an absent gst_id means the
  // address's linked GST, else the default; an empty string would be a 422 on
  // an int field instead.
  if (input.address_id !== undefined && input.address_id !== null) {
    form.append("address_id", String(input.address_id));
  }
  if (input.gst_id !== undefined && input.gst_id !== null) {
    form.append("gst_id", String(input.gst_id));
  }
  // The quantity model. Every one of these is optional, and leaving
  // total_quantity out is what keeps the old one-order-per-account behaviour.
  appendIfSet(form, "total_quantity", input.total_quantity);
  appendIfSet(form, "qty_per_order", input.qty_per_order);
  appendIfSet(form, "total_attempts", input.total_attempts);
  appendIfSet(form, "cart_amount_limit", input.cart_amount_limit);
  appendIfSet(form, "max_delivery_days", input.max_delivery_days);
  appendIfText(form, "brand", input.brand);
  appendIfText(form, "category", input.category);
  appendIfText(form, "model", input.model);
  // Omitted when blank: no seller field is Flipkart's default seller, which a
  // run that chose none must keep meaning.
  appendIfText(form, "seller", input.seller);
  // Only when ticked: a person finishes the bank's step in the window the run
  // shows. The server refuses it on a Cash-on-Delivery run.
  if (input.bank_step_by_hand) form.append("bank_step_by_hand", "true");
  // Cash on Delivery involves no card, so nothing is attached in that mode.
  if (input.gift_cards_file && usesGiftCards(input.payment_mode)) {
    form.append("gift_cards_file", input.gift_cards_file);
  }
  if (input.extra_product_links && input.extra_product_links.length) {
    form.append("extra_product_links", input.extra_product_links.join("\n"));
  }
  if (input.coupon_codes && input.coupon_codes.length) form.append("coupon_codes", input.coupon_codes.join(","));
  if (input.reward_ids && input.reward_ids.length) form.append("reward_ids", input.reward_ids.join(","));
  if (input.flipkart_minutes) form.append("flipkart_minutes", "true");
  if (input.cards_file !== null) {
    form.append("cards_file", input.cards_file);
  }
  return request<Job>("/jobs/order", { method: "POST", body: form });
}

/**
 * Which card would pay for which account, before a card run starts: the
 * server reads and pairs the sheet as the run would and answers each card's
 * last four digits only. Nothing is kept; Start sends the sheet again.
 */
export function previewOrderCards(input: {
  cards_file: File;
  account_ids: number[] | null;
  account_filter?: AccountFilterInput;
  card_type?: string;
  /** As the run: HDFC child cards with less than this left are not paired. */
  card_min_remaining?: number | null;
}): Promise<CardPreview> {
  const form = new FormData();
  if (input.card_type && input.card_type.trim()) form.append("card_type", input.card_type.trim());
  appendIfSet(form, "card_min_remaining", input.card_min_remaining);
  form.append("account_ids", input.account_ids === null ? "" : input.account_ids.join(","));
  if (input.account_filter !== undefined) {
    form.append("account_filter", JSON.stringify(input.account_filter));
  }
  form.append("cards_file", input.cards_file);
  return request<CardPreview>("/jobs/order/card-preview", { method: "POST", body: form });
}

/**
 * Read order status back from the platform for these accounts.
 *
 * Plain JSON, not multipart: a sync carries no card sheet because it buys
 * nothing. It opens each order's own page and writes what it reads — tracking
 * id, platform status, delivery date, delivery OTP — onto the order rows.
 *
 * The body is assembled field by field because the server rejects unknown
 * ones: a typo here would be a 422 rather than a silently ignored key, which
 * is the trade this endpoint deliberately makes.
 */
export function startSyncJob(input: SyncJobInput): Promise<Job> {
  const body: Record<string, unknown> = { account_ids: input.account_ids };
  if (input.account_filter !== undefined) body.account_filter = input.account_filter;
  if (input.limit_per_account !== undefined && Number.isFinite(input.limit_per_account)) {
    body.limit_per_account = Math.trunc(input.limit_per_account);
  }
  if (input.import_unknown) body.import_unknown = true;
  if (input.order_ids && input.order_ids.length > 0) body.order_ids = input.order_ids;
  return request<Job>("/jobs/sync", { method: "POST", body: JSON.stringify(body) });
}

/** Probe the saved OTP inbox. Sends no password — the server uses what it has. */
export function testOtpInbox(): Promise<OtpInboxTestResult> {
  return request<OtpInboxTestResult>("/settings/otp-inbox/test", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function listJobs(limit = 50): Promise<Job[]> {
  return request<Job[]>(`/jobs?limit=${encodeURIComponent(String(limit))}`);
}

/** The Smart Bulk Order "Upload CSV": email ids to account ids. */
export function accountsByEmails(input: {
  emails: string[];
  statuses?: string[];
}): Promise<{ ids: number[]; unmatched: string[]; not_eligible: string[] }> {
  return request("/accounts/by-emails", { method: "POST", body: JSON.stringify(input) });
}

/** The codes "Exclude cards with specific failure errors" offers. */
export function listCardFailureCodes(): Promise<{ code: string; name: string }[]> {
  return request<{ code: string; name: string }[]>("/jobs/card-failure-codes");
}

/** The Orders page: one row per order run, newest first. */
export function listBulkOrders(limit = 100, filters: BulkOrderRunFilters = {}): Promise<BulkOrderRun[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (filters.since) params.set("since", filters.since);
  if (filters.until) params.set("until", filters.until);
  for (const status of filters.status ?? []) params.append("status", status);
  if (filters.mode) params.set("mode", filters.mode);
  if (filters.created_by) params.set("created_by", filters.created_by);
  if (filters.platform) params.set("platform", filters.platform);
  return request<BulkOrderRun[]>(`/jobs/bulk-orders?${params.toString()}`);
}

/** Who has started order runs: the Orders page's "Created by". */
export function listBulkOrderCreators(): Promise<string[]> {
  return request<string[]>("/jobs/bulk-orders/creators");
}

/* --- Order Units: CSV Downloads and accounting references ----------------- */

export interface ExportLogRow {
  id: number;
  kind: string;
  file_name: string;
  query: string;
  created_by: string | null;
  created_at: string;
}

export function listExports(): Promise<ExportLogRow[]> {
  return request<ExportLogRow[]>("/orders/exports");
}

/** Make the same export again (the file is never kept, only its query). */
export function downloadExportAgain(row: ExportLogRow): Promise<void> {
  return downloadFile(`/orders/export.csv${row.query ? `?${row.query}` : ""}`, row.file_name);
}

export function setOrderReferences(
  orderRowId: number,
  refs: { po_number?: string; rtgs_source?: string; rtgs_beneficiary?: string; ewb_number?: string },
): Promise<Order> {
  return request<Order>(`/orders/${orderRowId}/references`, { method: "PATCH", body: JSON.stringify(refs) });
}

/* --- CorpAssist (HDFC) ---------------------------------------------------- */

export interface HdfcUserIdInput {
  username: string;
  email: string;
  mobile?: string | null;
  /** Only its last four digits are kept. */
  parent_card?: string | null;
  /** The parent card's Alternate A/c No.; the API keeps its digits (8 to 32). */
  aan?: string | null;
  /** HDFC's own Virtual Card User ID. */
  portal_user_id?: string | null;
  active?: boolean;
}

export function listHdfcUserIds(): Promise<HdfcUserId[]> {
  return request<HdfcUserId[]>("/hdfc/user-ids");
}

export function addHdfcUserId(input: HdfcUserIdInput): Promise<HdfcUserId> {
  return request<HdfcUserId>("/hdfc/user-ids", { method: "POST", body: JSON.stringify(input) });
}

export function updateHdfcUserId(id: number, patch: Partial<HdfcUserIdInput>): Promise<HdfcUserId> {
  return request<HdfcUserId>(`/hdfc/user-ids/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function deleteHdfcUserId(id: number): Promise<void> {
  return request<void>(`/hdfc/user-ids/${id}`, { method: "DELETE" });
}

/** Read the child-card emails of these user ids, or of every active one. Reads the inbox only. */
export function fetchHdfcCards(userIds: number[] | null): Promise<HdfcFetchResult[]> {
  return request<HdfcFetchResult[]>("/hdfc/fetch", { method: "POST", body: JSON.stringify({ user_ids: userIds }) });
}

function hdfcCardQuery(filters: HdfcCardFilters): string {
  const params = new URLSearchParams();
  if (filters.user_id) params.set("user_id", String(filters.user_id));
  if (filters.status) params.set("status", filters.status);
  if (filters.min_remaining?.trim()) params.set("min_remaining", filters.min_remaining.trim());
  if (filters.created_from) params.set("created_from", filters.created_from);
  if (filters.created_to) params.set("created_to", filters.created_to);
  if (filters.search?.trim()) params.set("search", filters.search.trim());
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function listHdfcChildCards(filters: HdfcCardFilters = {}): Promise<HdfcChildCard[]> {
  return request<HdfcChildCard[]>(`/hdfc/child-cards${hdfcCardQuery(filters)}`);
}

export function downloadHdfcChildCards(filters: HdfcCardFilters = {}): Promise<void> {
  return downloadFile(`/hdfc/child-cards.csv${hdfcCardQuery(filters)}`, "hdfc_child_cards.csv");
}

export function actOnHdfcChildCards(ids: number[], action: "cancel" | "activate" | "delete"): Promise<{ changed: number }> {
  return request<{ changed: number }>("/hdfc/child-cards/action", { method: "POST", body: JSON.stringify({ ids, action }) });
}

/** Ask HDFC for new virtual cards: on the portal, or as a bulk sheet to upload by hand. */
export function createHdfcBatch(rows: HdfcBatchRowInput[], send: HdfcBatchSend): Promise<HdfcBatch> {
  return request<HdfcBatch>("/hdfc/batches", { method: "POST", body: JSON.stringify({ rows, send }) });
}

export function listHdfcBatches(): Promise<HdfcBatch[]> {
  return request<HdfcBatch[]>("/hdfc/batches");
}

export function getHdfcBatch(id: number): Promise<HdfcBatch> {
  return request<HdfcBatch>(`/hdfc/batches/${id}`);
}

/** HDFC's bulk sheet for this batch, as a CSV. */
export function downloadHdfcBatchSheet(id: number): Promise<void> {
  return downloadFile(`/hdfc/batches/${id}.csv`, `hdfc_batch_${id}.csv`);
}

export function retryHdfcBatch(id: number): Promise<HdfcBatch> {
  return request<HdfcBatch>(`/hdfc/batches/${id}/retry`, { method: "POST" });
}

export function cancelHdfcBatch(id: number): Promise<HdfcBatch> {
  return request<HdfcBatch>(`/hdfc/batches/${id}/cancel`, { method: "POST" });
}

export function getHdfcPortal(): Promise<HdfcPortalStatus> {
  return request<HdfcPortalStatus>("/hdfc/portal");
}

export function testHdfcPortalLogin(): Promise<HdfcPortalTestResult> {
  return request<HdfcPortalTestResult>("/hdfc/portal/test-login", { method: "POST" });
}

export interface HdfcCaptcha {
  /** `data:image/png;base64,...` of HDFC's captcha. */
  image: string;
  expires_in: number;
}

/** Open HDFC's sign-in page (user and password filled) and get its captcha to read. */
export function startHdfcCaptcha(): Promise<HdfcCaptcha> {
  return request<HdfcCaptcha>("/hdfc/portal/captcha", { method: "POST" });
}

/** Sign in with the captcha a person typed; `needs_otp` when HDFC then wants an OTP typed. */
export function submitHdfcCaptcha(text: string): Promise<HdfcPortalTestResult & { needs_otp: boolean }> {
  return request<HdfcPortalTestResult & { needs_otp: boolean }>("/hdfc/portal/captcha/submit", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

/** Finish the captcha sign-in with the OTP read from HDFC's message. */
export function submitHdfcOtp(code: string): Promise<HdfcPortalTestResult> {
  return request<HdfcPortalTestResult>("/hdfc/portal/otp/submit", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

/** Set this user's email on the HDFC portal to the one saved here (the shared inbox). */
export function pointHdfcUserEmail(id: number): Promise<HdfcUserId> {
  return request<HdfcUserId>(`/hdfc/user-ids/${id}/point-email`, { method: "POST" });
}

/** Import HDFC's Virtual Card Users as user ids. */
export function syncHdfcPortalUsers(): Promise<HdfcPortalSyncResult> {
  return request<HdfcPortalSyncResult>("/hdfc/portal/sync-users", { method: "POST" });
}

/* --- employee phones (SMS relay) ------------------------------------------ */

export interface OtpPhoneInput {
  label: string;
  phone_number: string;
  employee_id?: string | null;
  corporate_id?: string | null;
  active?: boolean;
}

export type OtpPhonePatch = Partial<Pick<OtpPhoneInput, "label" | "employee_id" | "corporate_id" | "active">>;

export function listOtpPhones(): Promise<OtpPhone[]> {
  return request<OtpPhone[]>("/otp-phones");
}

export function addOtpPhone(input: OtpPhoneInput): Promise<OtpPhoneCreated> {
  return request<OtpPhoneCreated>("/otp-phones", { method: "POST", body: JSON.stringify(input) });
}

export function updateOtpPhone(id: number, patch: OtpPhonePatch): Promise<OtpPhone> {
  return request<OtpPhone>(`/otp-phones/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

/** A new token for this phone; the old one stops working. */
export function newOtpPhoneToken(id: number): Promise<OtpPhoneCreated> {
  return request<OtpPhoneCreated>(`/otp-phones/${id}/token`, { method: "POST" });
}

export function deleteOtpPhone(id: number): Promise<void> {
  return request<void>(`/otp-phones/${id}`, { method: "DELETE" });
}

/** The last day's texts this phone forwarded, newest first. */
export function listOtpPhoneMessages(id: number): Promise<OtpPhoneMessage[]> {
  return request<OtpPhoneMessage[]>(`/otp-phones/${id}/messages`);
}

/* --- gift vouchers and order phone numbers --------------------------------- */

export function listGiftVouchers(): Promise<GiftVoucher[]> {
  return request<GiftVoucher[]>("/gift-vouchers");
}

export function addGiftVoucher(input: {
  label: string;
  code?: string | null;
  value: number;
  balance?: number | null;
  account_email?: string | null;
  expires_on?: string | null;
}): Promise<GiftVoucher> {
  return request<GiftVoucher>("/gift-vouchers", { method: "POST", body: JSON.stringify(input) });
}

export function updateGiftVoucher(
  id: number,
  patch: { label?: string; balance?: number; account_email?: string | null; expires_on?: string | null },
): Promise<GiftVoucher> {
  return request<GiftVoucher>(`/gift-vouchers/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function deleteGiftVoucher(id: number): Promise<void> {
  return request<void>(`/gift-vouchers/${id}`, { method: "DELETE" });
}

export function listOrderPhones(): Promise<OrderPhoneNumber[]> {
  return request<OrderPhoneNumber[]>("/order-phones");
}

export function addOrderPhone(input: { number: string; label?: string | null; active?: boolean }): Promise<OrderPhoneNumber> {
  return request<OrderPhoneNumber>("/order-phones", { method: "POST", body: JSON.stringify(input) });
}

export function updateOrderPhone(id: number, patch: { label?: string | null; active?: boolean }): Promise<OrderPhoneNumber> {
  return request<OrderPhoneNumber>(`/order-phones/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function deleteOrderPhone(id: number): Promise<void> {
  return request<void>(`/order-phones/${id}`, { method: "DELETE" });
}

/* --- the agent fleet ------------------------------------------------------ */

export function getFleet(): Promise<FleetStatus> {
  return request<FleetStatus>("/agents/fleet");
}

export function fleetAction(action: "test-all" | "enable-all" | "disable-all"): Promise<FleetStatus> {
  return request<FleetStatus>(`/agents/fleet/${action}`, { method: "POST" });
}

export function updateAgent(id: number, patch: { enabled?: boolean; isolated?: boolean }): Promise<FleetStatus> {
  return request<FleetStatus>(`/agents/fleet/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function getJob(id: number): Promise<JobDetail> {
  return request<JobDetail>(`/jobs/${id}`);
}

export function cancelJob(id: number): Promise<Job> {
  return request<Job>(`/jobs/${id}/cancel`, { method: "POST" });
}

/* --- product preview -----------------------------------------------------
   One call per pasted link, so the form can show what is about to be ordered.
   The server opens a real browser page to read it, which is why callers debounce
   this rather than firing it per keystroke.
-------------------------------------------------------------------------- */

/**
 * Read a product page's details.
 *
 * It answers 200 even when the read FAILED — with `error` set and the rest
 * mostly null — because a preview must never break the ordering form. So the
 * caller checks `error` on the result; only a transport or auth failure throws.
 */
export function previewProduct(url: string): Promise<ProductPreview> {
  return request<ProductPreview>("/products/preview", {
    method: "POST",
    body: JSON.stringify({ url: url.trim() }),
  });
}

/** Search Flipkart by name: up to twelve products, each with its canonical
 *  link, for the order form to pick from. */
export function searchProducts(query: string): Promise<ProductSearchAnswer> {
  return request<ProductSearchAnswer>(`/products/search?${new URLSearchParams({ q: query.trim() })}`);
}

/* --- orders --------------------------------------------------------------- */

/**
 * The ledger filter as query parameters. One builder for the page, the count
 * and both exports, so a download can never hold different rows from the
 * screen that offered it.
 */
export function orderParams(filters: OrderFilters = {}): URLSearchParams {
  const params = new URLSearchParams();
  const many = (key: string, values?: readonly string[] | null) => {
    for (const value of values ?? []) {
      const text = value.trim();
      if (text) params.append(key, text);
    }
  };
  if (filters.q?.trim()) params.set("q", filters.q.trim());
  if (filters.status) params.append("status", filters.status);
  many("status", filters.statuses);
  many("platform_status", filters.platform_statuses);
  many("delivery_state", filters.delivery_states);
  many("processing_stage", filters.processing_stages);
  many("error_code", filters.error_codes);
  many("order_id", filters.order_ids);
  many("email", filters.emails);
  many("tracking_id", filters.tracking_ids);
  many("platform", filters.platforms);
  if (filters.bob === true || filters.bob === false) params.set("bob", String(filters.bob));
  if (filters.card?.trim()) params.set("card", filters.card.trim());
  // Sent as typed, only trimmed: the server owns the reading (GSTINs upper-cased
  // with the spaces out; a month that is not YYYY-MM refused, never ignored).
  if (filters.billed_gst?.trim()) params.set("billed_gst", filters.billed_gst.trim());
  if (filters.invoice_number?.trim()) params.set("invoice_number", filters.invoice_number.trim());
  if (filters.invoice_gstin?.trim()) params.set("invoice_gstin", filters.invoice_gstin.trim());
  if (filters.invoice_month?.trim()) params.set("invoice_month", filters.invoice_month.trim());
  if (filters.account_id !== undefined && filters.account_id !== null) {
    params.set("account_id", String(filters.account_id));
  }
  if (filters.job_id !== undefined && filters.job_id !== null) {
    params.set("job_id", String(filters.job_id));
  }
  if (filters.order_row !== undefined && filters.order_row !== null) {
    params.set("row", String(filters.order_row));
  }
  if (filters.date_field) params.set("date_field", filters.date_field);
  if (filters.date_from) params.set("date_from", filters.date_from);
  if (filters.date_to) params.set("date_to", filters.date_to);
  return params;
}

/** One page of orders plus the full match count — the "Showing N of M" line. */
export function listOrders(filters: OrderFilters = {}): Promise<OrderPage> {
  const params = orderParams(filters);
  params.set("page", String(filters.page ?? 1));
  params.set("size", String(filters.size ?? 100));
  return request<OrderPage>(`/orders?${params.toString()}`);
}

export function orderFilterOptions(): Promise<OrderFilterOptions> {
  return request<OrderFilterOptions>("/orders/filter-options");
}

export function getOrderView(): Promise<OrderView | null> {
  return request<OrderView | null>("/orders/view");
}

export function saveOrderView(view: { columns: string[]; filters: Partial<OrderFilters> }): Promise<OrderView> {
  return request<OrderView>("/orders/view", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(view),
  });
}

export function clearOrderView(): Promise<{ cleared: boolean }> {
  return request<{ cleared: boolean }>("/orders/view", { method: "DELETE" });
}

/**
 * The owner's two spreadsheet layouts (25 Oct 2026), beside the spec's full ledger:
 * what the person at the door needs, and every order after placing.
 */
export type OrderSheet = "out_for_delivery" | "order_data";

/**
 * The spec's `order_units_<date>_<HHMM>.csv` for the current filter — or, with
 * `sheet`, one of the owner's own layouts under that name. No row cap.
 */
export function downloadOrdersCsv(filters: OrderFilters = {}, sheet?: OrderSheet): Promise<void> {
  const params = orderParams(filters);
  if (sheet) params.set("sheet", sheet);
  return downloadFile(`/orders/export.csv?${params.toString()}`, `${sheet ?? "order_units"}.csv`);
}

/** The order's invoice PDF from the store — addressed by the order row, not the unit. */
export function downloadInvoice(orderRowId: number, orderId: string | null): Promise<void> {
  return downloadFile(`/orders/${orderRowId}/invoice.pdf`, `${orderId ?? `order-${orderRowId}`}.pdf`);
}

/** Every invoice on file for an order, oldest first, with what was read off each PDF. */
export function listOrderInvoices(orderRowId: number): Promise<OrderInvoice[]> {
  return request<OrderInvoice[]>(`/orders/${orderRowId}/invoices`);
}

/** Mark the ticked ledger rows' goods as in hand, or take the mark back. */
export function markReceived(unitIds: number[], received: boolean): Promise<OrderReceivedResult> {
  return request<OrderReceivedResult>("/orders/received", {
    method: "POST",
    body: JSON.stringify({ unit_ids: unitIds, received }),
  });
}

/**
 * Every cancel / reschedule request on an order's units, open or closed,
 * newest first, each naming its unit (`unit_pk`): read once when the row
 * dialog opens, which shows its own unit's.
 */
export function listOrderRequests(orderRowId: number): Promise<UnitRequest[]> {
  return request<UnitRequest[]>(`/orders/${orderRowId}/requests`);
}

/** One particular invoice PDF of an order. */
export function downloadOrderInvoice(orderRowId: number, invoiceId: number, filename: string): Promise<void> {
  return downloadFile(`/orders/${orderRowId}/invoices/${invoiceId}.pdf`, filename);
}

/** Invoice PDFs by hand, several at once, matched to their orders by the id in each name. */
export function uploadInvoices(files: File[]): Promise<InvoiceUploadResult> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  return request<InvoiceUploadResult>("/invoices/upload", { method: "POST", body: form });
}

/* --- extract invoices data -------------------------------------------------
   The invoice types found on the orders of the units matching the ledger
   filter (spec FR-026, C-10), and the invoice-data CSV of the ticked types
   (FR-027–FR-030). The filter travels as the ledger's own query string.
-------------------------------------------------------------------------- */

export function getInvoiceTypes(filters: OrderFilters = {}): Promise<InvoiceTypes> {
  return request<InvoiceTypes>(`/invoices/types?${orderParams(filters).toString()}`);
}

function headerCount(res: Response, name: string): number {
  const value = Number(res.headers.get(name));
  return Number.isFinite(value) && value >= 0 ? Math.trunc(value) : 0;
}

/**
 * The spec's `invoice_data_<date>_<HHMM>.csv` for the ticked types. The
 * server says how many rows it wrote and how many matching units had no
 * invoice on file (FR-030) in headers, read off the response before the
 * file is handed to the browser.
 */
export async function downloadInvoiceData(filters: OrderFilters, types: string[]): Promise<InvoiceDataDownload> {
  const params = orderParams(filters);
  for (const type of types) params.append("type", type);
  const res = await fetchDownload(`/invoices/data.csv?${params.toString()}`);
  await saveAsFile(res, "invoice_data.csv");
  return { rows: headerCount(res, "X-Invoice-Rows"), units_left_out: headerCount(res, "X-Units-Left-Out") };
}

/* --- compile invoices ------------------------------------------------------
   One zip of the invoice PDFs of the units matching the ledger filter (spec
   FR-031–FR-039). The filter travels as the same query string the ledger
   page uses, so the archive covers exactly the rows on screen.
-------------------------------------------------------------------------- */

export function startCompile(filters: OrderFilters, prefix: string): Promise<CompileStartResult> {
  return request<CompileStartResult>(`/invoices/compile?${orderParams(filters).toString()}`, {
    method: "POST",
    body: JSON.stringify({ prefix }),
  });
}

export function getCompileStatus(): Promise<CompileStatus> {
  return request<CompileStatus>("/invoices/compile/status");
}

/**
 * One page of the compiled archives, newest first, at the server's page size
 * (200): the newest ones, or with `before` — the `next_before` of the page
 * before — the ones older than that page.
 */
export function listArchives(before: string | null = null): Promise<CompiledArchivePage> {
  const query = before === null ? "" : `?${new URLSearchParams({ before }).toString()}`;
  return request<CompiledArchivePage>(`/invoices/archives${query}`);
}

export function deleteArchive(id: number): Promise<void> {
  return request<void>(`/invoices/archives/${id}`, { method: "DELETE" });
}

/**
 * A short-lived link first, then a plain navigation to it: an archive can be a
 * gigabyte, which has to stream to disk rather than through a blob in memory,
 * and a navigation cannot carry the bearer header — the token in the link
 * stands in for it, for that one archive, for a couple of minutes. The link is
 * probed (HEAD) before the navigation: a refused link must become an error in
 * the app, not a bare JSON page in the app's place.
 */
export async function downloadArchive(id: number): Promise<void> {
  const grant = await request<{ url: string }>(`/invoices/archives/${id}/download-token`, { method: "POST" });
  const url = `${BASE}${grant.url}`;
  let probe: Response;
  try {
    probe = await fetch(url, { method: "HEAD" });
  } catch {
    throw new ApiError(0, "Could not reach the server. Check that it is running and try again.");
  }
  if (!probe.ok) {
    throw new ApiError(
      probe.status,
      probe.status === 401
        ? "The download link expired before it was used — try again."
        : probe.status === 404
          ? "The archive file is no longer on the server."
          : `The download was refused (${probe.status}).`,
    );
  }
  window.location.assign(url);
}

/** A sheet of order ids (CSV or XLSX) to track; the answer says what to read back. */
export function importOrderIds(file: File): Promise<OrderIdsImportResult> {
  const form = new FormData();
  form.append("file", file);
  return request<OrderIdsImportResult>("/orders/import-ids", { method: "POST", body: form });
}

/** Bring units in from an order report (one row per unit, the template's columns). */
export function importOrderReport(file: File): Promise<OrderReportImportResult> {
  const form = new FormData();
  form.append("file", file);
  return request<OrderReportImportResult>("/orders/import-report", { method: "POST", body: form });
}

/** The order report's header row, to fill in. */
export function downloadReportTemplate(): Promise<void> {
  return downloadFile("/orders/report-template.csv", "order-report-template.csv");
}

/**
 * Close an *unconfirmed* order after checking the account on Flipkart:
 * `placed` with the order id found there, or `failed` when it is not there.
 * The server refuses any other status transition.
 */
export function settleOrder(
  id: number,
  body: { status: "placed"; order_id: string } | { status: "failed" },
): Promise<Order> {
  return request<Order>(`/orders/${id}/settle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}


/** The legacy workbook layout, for the same filter as the screen. */
export function downloadOrders(filters: OrderFilters = {}): Promise<void> {
  return downloadFile(`/orders/export?${orderParams(filters).toString()}`, "orders.xlsx");
}

/* --- alerts --------------------------------------------------------------
   The "needs attention" queue. Alerts are resolved, never deleted, so there
   is no DELETE here: `resolveAlert` closes one and `reopenAlert` puts it back
   in the queue when it turns out to still need a human.
-------------------------------------------------------------------------- */

/**
 * The queue, most recently moved first (a reopened alert is news again).
 *
 * `resolved` is tri-state and always sent explicitly when the caller has one:
 * the server's default for an ABSENT resolved is the open queue, so "both" has
 * to travel as `any` rather than as silence.
 */
export function listAlerts(filters: AlertFilters = {}): Promise<Alert[]> {
  const params = new URLSearchParams();
  if (filters.severity) params.set("severity", filters.severity);
  const kind = filters.kind?.trim();
  if (kind) params.set("kind", kind);
  if (filters.resolved !== undefined) {
    params.set("resolved", filters.resolved === "any" ? "any" : String(filters.resolved));
  }
  if (filters.order) params.set("order", filters.order);
  params.set("limit", String(filters.limit ?? 100));
  return request<Alert[]>(`/alerts?${params.toString()}`);
}

/** Open counts, by severity — what the nav badge and the dashboard read. */
export function alertsSummary(): Promise<AlertSummary> {
  return request<AlertSummary>("/alerts/summary");
}

/**
 * Re-run the alert rules over the data as it stands now.
 *
 * Safe to press twice: a rule that still matches updates the alert it already
 * raised (same `key`) instead of adding a second copy.
 */
export function evaluateAlerts(): Promise<AlertEvaluateResult> {
  return request<AlertEvaluateResult>("/alerts/evaluate", { method: "POST" });
}

/** Close one. The row stays on file, so the same problem can reopen it. */
/** Close one alert, with the operator's reason when they give one. */
export function resolveAlert(id: number, note?: string | null): Promise<Alert> {
  const text = note?.trim();
  return request<Alert>(`/alerts/${id}/resolve`, {
    method: "POST",
    body: text ? JSON.stringify({ note: text }) : undefined,
  });
}

/** Close several alerts at once under one reason; answers how many it closed. */
export function resolveAlerts(ids: number[], note?: string | null): Promise<{ resolved: number; open: number }> {
  const text = note?.trim();
  return request<{ resolved: number; open: number }>("/alerts/resolve", {
    method: "POST",
    body: JSON.stringify(text ? { ids, note: text } : { ids }),
  });
}

/** Units placed and not yet delivered, by product; `olderThanDays` narrows to older orders. */
/** Units Flipkart says are out for delivery, with the delivery code, as of the last read-back. */
/** The processes that run the browsers, and whether they are up (step H). */
export function getAgents(): Promise<AgentsStatus> {
  return request<AgentsStatus>("/agents");
}

/** Accounts by login status, for the dashboard's Account logins card. */
function scopeParams(scope: DashboardScope = {}): string {
  const params = new URLSearchParams();
  if (scope.platform) params.set("platform", scope.platform);
  if (scope.tag === "untagged") params.set("untagged", "true");
  else if (typeof scope.tag === "number") params.set("tag_id", String(scope.tag));
  if (scope.domain) params.set("domain", scope.domain);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function getLoginHealth(scope: DashboardScope = {}): Promise<LoginHealth> {
  return request<LoginHealth>(`/stats/logins${scopeParams(scope)}`);
}

export function getBalanceTotals(scope: DashboardScope = {}): Promise<BalanceTotals> {
  return request<BalanceTotals>(`/stats/balances${scopeParams(scope)}`);
}

export function getOutForDelivery(): Promise<OutForDelivery> {
  return request<OutForDelivery>("/stats/out-for-delivery");
}

/**
 * Follow a run until it ends and hand back its final state — for a button
 * that starts a read-back and wants to show what it found. Gives up quietly
 * after `timeoutMs` (returning the last state seen) and stops at once when
 * `signal` aborts, say because the dialog that asked was closed.
 */
export async function waitForJob(
  id: number,
  { intervalMs = 3000, timeoutMs = 15 * 60_000, signal }: { intervalMs?: number; timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<JobDetail> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (signal?.aborted) throw new Error("Stopped waiting for the run.");
    const detail = await getJob(id);
    if (detail.status === "completed" || detail.status === "failed" || Date.now() >= deadline) return detail;
    await new Promise<void>((resolve) => window.setTimeout(resolve, intervalMs));
  }
}

export function getToBeDelivered(olderThanDays?: number | null): Promise<ToBeDelivered> {
  const params = new URLSearchParams();
  if (olderThanDays) params.set("older_than_days", String(olderThanDays));
  const query = params.toString();
  return request<ToBeDelivered>(`/stats/to-be-delivered${query ? `?${query}` : ""}`);
}

export function reopenAlert(id: number): Promise<Alert> {
  return request<Alert>(`/alerts/${id}/reopen`, { method: "POST" });
}

/* --- stats ---------------------------------------------------------------- */

export function getStats(): Promise<Stats> {
  return request<Stats>("/stats");
}

/* --- live progress -------------------------------------------------------- */

/**
 * Browsers cannot set headers on a WebSocket, and a token in the query string
 * would land in every access log on the way (the proxy's, uvicorn's), so the
 * URL carries none: the caller offers the token as the second WebSocket
 * subprotocol — `new WebSocket(jobStreamUrl(id), ["bearer", token])` — which
 * travels in the Sec-WebSocket-Protocol header, where the server reads it
 * (a JWT's characters are all legal there). The `/ws` prefix is PRESERVED by
 * both proxies.
 */
export function jobStreamUrl(jobId: number): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws/jobs/${jobId}`;
}

/* --- cancel / reschedule requests (spec FR-020, decision C-4) --------------
   Nothing here reaches Flipkart: a request is recorded for a person to action
   there by hand and then mark done under Requests.
-------------------------------------------------------------------------- */

/**
 * One request per unit. A unit that cannot take one — already delivered,
 * already asked, not found — comes back in `skipped` with the reason, and the
 * rest are still recorded. The server is the one that checks what each kind
 * needs (a reason for a cancel, a date for a reschedule).
 */
export function createUnitRequests(input: UnitRequestCreateInput): Promise<UnitRequestCreateResult> {
  const body: Record<string, unknown> = { unit_ids: input.unit_ids, kind: input.kind };
  const reason = input.reason?.trim();
  if (reason) body.reason = reason;
  if (input.requested_for) body.requested_for = input.requested_for;
  return request<UnitRequestCreateResult>("/requests", { method: "POST", body: JSON.stringify(body) });
}

/** The log, newest first, with the full count. An absent `status` is the open queue. */
export function listUnitRequests(filters: UnitRequestFilters = {}): Promise<UnitRequestPage> {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.kind) params.set("kind", filters.kind);
  params.set("page", String(filters.page ?? 1));
  params.set("size", String(filters.size ?? 50));
  return request<UnitRequestPage>(`/requests?${params.toString()}`);
}

/** Mark a request done or dropped — with an optional note — or put it back to open. */
export function updateUnitRequest(id: number, status: UnitRequestStatus, note?: string | null): Promise<UnitRequest> {
  const text = note?.trim();
  return request<UnitRequest>(`/requests/${id}`, {
    method: "PATCH",
    body: JSON.stringify(text ? { status, note: text } : { status }),
  });
}

/* --- what changed since you last looked ------------------------------------
   What the read-backs found — delivered, cancelled, returned, out for
   delivery — after the signed-in user's own "seen" mark. The mark is per
   user: clearing your list does not clear anyone else's.
-------------------------------------------------------------------------- */

/** The server's own bounds for a page of changes. */
export const CHANGES_DEFAULT_SIZE = 25;
export const CHANGES_MAX_SIZE = 200;

/** One page of the changes after your seen mark, newest first. */
export function listChanges(params: { page?: number; size?: number } = {}): Promise<ChangesPage> {
  const qs = new URLSearchParams();
  qs.set("page", String(Math.max(1, Math.trunc(params.page ?? 1))));
  const size = params.size ?? CHANGES_DEFAULT_SIZE;
  qs.set("size", String(Math.min(CHANGES_MAX_SIZE, Math.max(1, Math.trunc(size)))));
  return request<ChangesPage>(`/changes?${qs.toString()}`);
}

/**
 * Mark your changes as seen. Pass the `as_of` of the list that was shown, so a
 * change recorded after it loaded stays unseen; without it, up to now.
 */
export function markChangesSeen(through?: string | null): Promise<ChangesSeen> {
  return request<ChangesSeen>("/changes/seen", {
    method: "POST",
    body: through ? JSON.stringify({ through }) : undefined,
  });
}

/* --- Employee Phones settings & bulk CSV (BOB parity) --------------------- */

export function getOtpPhoneSettings(): Promise<OtpPhoneSettings> {
  return request<OtpPhoneSettings>("/otp-phones/settings");
}

export function setOtpPhoneSettings(assume_online: boolean): Promise<OtpPhoneSettings> {
  return request<OtpPhoneSettings>("/otp-phones/settings", {
    method: "PUT",
    body: JSON.stringify({ assume_online }),
  });
}

export function bulkImportOtpPhones(input: { corporate_id?: string | null; csv_text: string }): Promise<OtpPhoneBulkResult> {
  return request<OtpPhoneBulkResult>("/otp-phones/bulk", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/* --- Product Screener 20-slot Watchlist (BOB parity) ---------------------- */

export function getScreenerConfig(): Promise<ScreenerConfig> {
  return request<ScreenerConfig>("/screener/config");
}

export function updateScreenerConfig(patch: Partial<ScreenerConfig>): Promise<ScreenerConfig> {
  return request<ScreenerConfig>("/screener/config", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

export function listScreenerItems(): Promise<ScreenerItem[]> {
  return request<ScreenerItem[]>("/screener/items");
}

export function addScreenerItem(input: {
  product_url: string;
  product_title?: string | null;
  target_price?: number | null;
  platform?: string;
}): Promise<ScreenerItem> {
  return request<ScreenerItem>("/screener/items", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateScreenerItem(
  id: number,
  patch: { product_title?: string | null; target_price?: number | null; offer_applied?: string | null; net_price?: number | null },
): Promise<ScreenerItem> {
  return request<ScreenerItem>(`/screener/items/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteScreenerItem(id: number): Promise<void> {
  return request<void>(`/screener/items/${id}`, { method: "DELETE" });
}

export function checkScreenerItem(id: number): Promise<ScreenerItem> {
  return request<ScreenerItem>(`/screener/items/${id}/check`, { method: "POST" });
}

export function checkAllScreenerItems(): Promise<ScreenerItem[]> {
  return request<ScreenerItem[]>("/screener/check-all", { method: "POST" });
}

/* --- Inventory (BOB parity) ----------------------------------------------- */

export function listInventoryStock(tab: "active" | "completed" | "all" = "active", q = ""): Promise<InventoryStockRow[]> {
  const qs = new URLSearchParams({ tab });
  if (q.trim()) qs.set("q", q.trim());
  return request<InventoryStockRow[]>(`/inventory/stock?${qs.toString()}`);
}

export function downloadInventoryMismatchReport(): Promise<void> {
  return downloadFile("/inventory/mismatch-report.csv", "inventory_mismatch_report.csv");
}

export function updateCodCards(csv_text: string): Promise<CodCardsUpdateResult> {
  return request<CodCardsUpdateResult>("/inventory/cod-cards", {
    method: "POST",
    body: JSON.stringify({ csv_text }),
  });
}

export function listInventoryBatches(): Promise<InventoryBatch[]> {
  return request<InventoryBatch[]>("/inventory/batches");
}

export function createInventoryBatch(input: {
  name: string;
  platform?: string;
  product_name: string;
  opening_qty: number;
  received_qty: number;
  approved_qty: number;
  status?: "active" | "completed";
  notes?: string | null;
}): Promise<InventoryBatch> {
  return request<InventoryBatch>("/inventory/batches", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function updateInventoryBatch(
  id: number,
  patch: Partial<{ name: string; opening_qty: number; received_qty: number; approved_qty: number; status: "active" | "completed"; notes: string | null }>,
): Promise<InventoryBatch> {
  return request<InventoryBatch>(`/inventory/batches/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteInventoryBatch(id: number): Promise<void> {
  return request<void>(`/inventory/batches/${id}`, { method: "DELETE" });
}

export function listInventoryTransfers(): Promise<InventoryTransfer[]> {
  return request<InventoryTransfer[]>("/inventory/transfers");
}

export function createInventoryTransfer(input: {
  reference: string;
  platform?: string;
  product_name: string;
  quantity: number;
  destination?: string | null;
  notes?: string | null;
}): Promise<InventoryTransfer> {
  return request<InventoryTransfer>("/inventory/transfers", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function deleteInventoryTransfer(id: number): Promise<void> {
  return request<void>(`/inventory/transfers/${id}`, { method: "DELETE" });
}

/* --- Order Units All Tasks (BOB parity) ----------------------------------- */

export function listJobTasks(params: {
  page?: number;
  size?: number;
  email?: string;
  job_type?: string;
  status?: string;
  error_q?: string;
} = {}): Promise<JobTaskPage> {
  const qs = new URLSearchParams();
  qs.set("page", String(params.page ?? 1));
  qs.set("size", String(params.size ?? 50));
  if (params.email?.trim()) qs.set("email", params.email.trim());
  if (params.job_type?.trim()) qs.set("job_type", params.job_type.trim());
  if (params.status?.trim()) qs.set("status", params.status.trim());
  if (params.error_q?.trim()) qs.set("error_q", params.error_q.trim());
  return request<JobTaskPage>(`/jobs/tasks?${qs.toString()}`);
}

