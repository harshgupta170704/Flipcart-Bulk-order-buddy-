/**
 * The saved ledger view, both ways: what Quick Save keeps of the applied
 * filter, and what a saved view is allowed to put back. Pure functions, so the
 * contract is testable without a page. The Range presets live here too: a
 * view keeps a relative one and works its dates out again when restored.
 */
import type { DatePreset, DeliveryState, OrderDateField, OrderFilters, OrderOutcomeFilter } from "./types";

/** The server's outcomes (`OUTCOMES` in core/order_filter.py). */
export const OUTCOMES: readonly string[] = ["placed", "failed", "failed_over_target", "unconfirmed", "pending", "cancelled"];
export const DELIVERY_STATES: readonly string[] = ["delivered", "cancelled", "returned", "failed", "in_transit", "unknown"];
const DATE_FIELDS: readonly string[] = ["placed_at", "created_at", "delivery_date", "synced_at"];
const DAY = /^\d{4}-\d{2}-\d{2}$/;
/** The server's limit on a card filter (`CARD_MAX_LENGTH` in core/order_filter.py):
 *  the input caps itself there, and a longer one saved by an older client is
 *  dropped on restore rather than refused on every load. */
export const CARD_MAX_LENGTH = 32;
/** The server's limits on the GSTIN and invoice-number filters (`GSTIN_MAX_LENGTH`,
 *  `INVOICE_NUMBER_MAX_LENGTH` in core/order_filter.py), kept the same way. */
export const GSTIN_MAX_LENGTH = 32;
export const INVOICE_NUMBER_MAX_LENGTH = 64;
/** An invoice month as the server takes it. */
const MONTH = /^\d{4}-(0[1-9]|1[0-2])$/;

/* --- the Range presets ------------------------------------------------------ */

/** The Range select's choices: no range, a span relative to today, or dates typed by hand. */
export type Preset = "any" | DatePreset | "custom";

/** The spans relative to today, in the Range select's order: the presets a saved view keeps. */
export const DATE_PRESETS: readonly DatePreset[] = ["7d", "30d", "month"];

function isDatePreset(value: unknown): value is DatePreset {
  return typeof value === "string" && (DATE_PRESETS as readonly string[]).includes(value);
}

function isoDay(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** The from/to a preset means, computed when it is chosen. */
export function presetRange(preset: Preset, now = new Date()): { date_from: string | null; date_to: string | null } {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (preset === "7d" || preset === "30d") {
    const from = new Date(today);
    from.setDate(today.getDate() - (preset === "7d" ? 6 : 29));
    return { date_from: isoDay(from), date_to: isoDay(today) };
  }
  if (preset === "month") {
    return { date_from: isoDay(new Date(today.getFullYear(), today.getMonth(), 1)), date_to: isoDay(today) };
  }
  return { date_from: null, date_to: null };
}

/**
 * The preset the draft's dates amount to, from the dates alone. Not what the
 * Range select shows (shownPreset): dates typed by hand stay a custom range
 * even on a day they equal a preset.
 */
export function presetOf(draft: Pick<OrderFilters, "date_from" | "date_to">, now = new Date()): Preset {
  if (!draft.date_from && !draft.date_to) return "any";
  for (const preset of DATE_PRESETS) {
    const range = presetRange(preset, now);
    if (range.date_from === draft.date_from && range.date_to === draft.date_to) return preset;
  }
  return "custom";
}

/**
 * The preset the Range select shows, and so the one Quick Save keeps: the one
 * picked from the select, while the dates are still what it means today; else
 * "Custom range" over any dates and "Any time" over none. Dates typed by hand
 * are never read as a preset, even on a day they equal one: they are the days
 * the operator chose, and a view saved with them must reopen on those days,
 * not on a later week's (review of 27 Oct 2026). Two presets can share their
 * dates as well — "This month" on the 7th is the same seven days as "Last 7
 * days" — and a view saved that day must reopen as the one picked.
 */
export function shownPreset(
  filters: Pick<OrderFilters, "date_from" | "date_to" | "date_preset">,
  now = new Date(),
): Preset {
  if (filters.date_preset) {
    const range = presetRange(filters.date_preset, now);
    if (range.date_from === filters.date_from && range.date_to === filters.date_to) return filters.date_preset;
  }
  return filters.date_from || filters.date_to ? "custom" : "any";
}

/* --- the saved view ---------------------------------------------------------- */

/**
 * What Quick Save keeps: the applied filter without a run's deep link (a run
 * is not a view) and without empty values, so the saved view is exactly what
 * narrows the list and nothing else. A range relative to today, picked from
 * the Range select, keeps its preset beside the dates (spec FR-011): restored
 * on a later day it means that day's span. A range typed by hand stays its
 * fixed dates, even when they equal a preset on the day it is saved.
 */
export function viewFilters(applied: OrderFilters, now = new Date()): Partial<OrderFilters> {
  const out: Partial<OrderFilters> = {};
  if (applied.q?.trim()) out.q = applied.q.trim();
  if (applied.status) out.status = applied.status;
  if (applied.statuses?.length) out.statuses = applied.statuses;
  if (applied.platform_statuses?.length) out.platform_statuses = applied.platform_statuses;
  if (applied.delivery_states?.length) out.delivery_states = applied.delivery_states;
  if (applied.processing_stages?.length) out.processing_stages = applied.processing_stages;
  if (applied.error_codes?.length) out.error_codes = applied.error_codes;
  if (applied.order_ids?.length) out.order_ids = applied.order_ids;
  if (applied.emails?.length) out.emails = applied.emails;
  if (applied.tracking_ids?.length) out.tracking_ids = applied.tracking_ids;
  if (applied.product_ids?.length) out.product_ids = applied.product_ids;
  if (applied.card?.trim()) out.card = applied.card.trim();
  if (applied.billed_gst?.trim()) out.billed_gst = applied.billed_gst.trim();
  if (applied.invoice_number?.trim()) out.invoice_number = applied.invoice_number.trim();
  if (applied.invoice_gstin?.trim()) out.invoice_gstin = applied.invoice_gstin.trim();
  if (applied.invoice_month?.trim()) out.invoice_month = applied.invoice_month.trim();
  if (applied.account_id != null) out.account_id = applied.account_id;
  if (applied.date_field && applied.date_field !== "placed_at") out.date_field = applied.date_field;
  if (applied.date_from) out.date_from = applied.date_from;
  if (applied.date_to) out.date_to = applied.date_to;
  // The dates stay beside the preset: a page from before presets were kept
  // still puts back the range as it was on the day it was saved.
  const preset = shownPreset(applied, now);
  if (isDatePreset(preset)) out.date_preset = preset;
  return out;
}

/** One line of text a saved view may put back: non-blank, within the server's limit. */
function oneText(value: unknown, max: number): string | undefined {
  return typeof value === "string" && value.trim() !== "" && value.length <= max ? value.trim() : undefined;
}

function strings(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (Array.isArray(value)) {
    return value
      .filter((item): item is string => typeof item === "string" && item.trim() !== "")
      .map((item) => item.trim());
  }
  return [];
}

/**
 * A saved view's filters, kept only where the page could apply them. The
 * server validates on save, but a view saved by an older client — or by hand
 * — must never be able to break every load until Default View; anything
 * unreadable is dropped, not trusted. A relative preset's dates are worked
 * out again for `now`: "Last 7 days" saved a week ago means the last seven
 * days today, not then.
 */
export function restoredFilters(raw: unknown, now = new Date()): Partial<OrderFilters> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const v = raw as Record<string, unknown>;
  const out: Partial<OrderFilters> = {};

  if (typeof v.q === "string" && v.q.trim()) out.q = v.q.trim();

  const outcomes = [...strings(v.status), ...strings(v.statuses)].filter((s) => OUTCOMES.includes(s)) as OrderOutcomeFilter[];
  if (outcomes.length === 1) out.status = outcomes[0];
  else if (outcomes.length > 1) out.statuses = [...new Set(outcomes)];

  const platform = strings(v.platform_statuses);
  if (platform.length) out.platform_statuses = platform;
  const states = strings(v.delivery_states).filter((s) => DELIVERY_STATES.includes(s)) as DeliveryState[];
  if (states.length) out.delivery_states = states;
  const stages = strings(v.processing_stages ?? v.processing_stage);
  if (stages.length) out.processing_stages = stages;
  const codes = strings(v.error_codes ?? v.error_code);
  if (codes.length) out.error_codes = codes;
  const ids = strings(v.order_ids);
  if (ids.length) out.order_ids = ids;
  const emails = strings(v.emails);
  if (emails.length) out.emails = emails;
  const tracking = strings(v.tracking_ids);
  if (tracking.length) out.tracking_ids = tracking;
  const productIds = strings(v.product_ids);
  if (productIds.length) out.product_ids = productIds;
  if (typeof v.card === "string" && v.card.trim() && v.card.length <= CARD_MAX_LENGTH) out.card = v.card.trim();
  const billed = oneText(v.billed_gst, GSTIN_MAX_LENGTH);
  if (billed) out.billed_gst = billed;
  const number = oneText(v.invoice_number, INVOICE_NUMBER_MAX_LENGTH);
  if (number) out.invoice_number = number;
  const invoiceGstin = oneText(v.invoice_gstin, GSTIN_MAX_LENGTH);
  if (invoiceGstin) out.invoice_gstin = invoiceGstin;
  // The pattern is the limit here: anything but YYYY-MM would be refused on every load.
  const month = oneText(v.invoice_month, 16);
  if (month && MONTH.test(month)) out.invoice_month = month;

  if (typeof v.account_id === "number" && Number.isInteger(v.account_id) && v.account_id > 0) {
    out.account_id = v.account_id;
  }
  if (typeof v.date_field === "string" && DATE_FIELDS.includes(v.date_field)) {
    out.date_field = v.date_field as OrderDateField;
  }
  if (typeof v.date_from === "string" && DAY.test(v.date_from)) out.date_from = v.date_from;
  if (typeof v.date_to === "string" && DAY.test(v.date_to)) out.date_to = v.date_to;
  if (isDatePreset(v.date_preset)) {
    // The saved dates were that day's; the preset's are today's.
    out.date_preset = v.date_preset;
    Object.assign(out, presetRange(v.date_preset, now));
  }
  return out;
}
