/**
 * The ledger's filter box. Nothing here fetches on its own: the page holds a
 * DRAFT that these controls edit, and only Apply sends it — a dozen inputs with
 * multi-value text in them must not refetch on every keystroke.
 *
 * The status picker groups the platform's own wordings the way the spec's
 * picker does (Confirmed / Cancelled / Others), with one checkbox per group
 * and one toggle per wording. Ticking a group ticks its wordings.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  DeliveryState,
  OrderDateField,
  OrderFilterOptions,
  OrderFilters,
  OrderOutcomeFilter,
} from "../lib/types";
import { formatCount } from "../lib/format";
import {
  CARD_MAX_LENGTH,
  GSTIN_MAX_LENGTH,
  INVOICE_NUMBER_MAX_LENGTH,
  presetRange,
  shownPreset,
  type Preset,
} from "../lib/orderView";
import { Button } from "./Button";
import { Field, Select, TextInput, type TextInputProps } from "./Field";
import { statusMeta } from "./StatusBadge";
import { useDismiss } from "./useDismiss";

// The preset arithmetic moved to lib/orderView, where the saved view needs it too.
export { presetOf, presetRange } from "../lib/orderView";
export type { Preset } from "../lib/orderView";

export interface OrderUnitsFiltersProps {
  draft: OrderFilters;
  onChange: (patch: Partial<OrderFilters>) => void;
  onApply: () => void;
  onReset: () => void;
  /**
   * True while the APPLIED filter narrows the list, so Reset stays available
   * after the draft has been cleared by hand without being applied.
   */
  appliedNarrowed: boolean;
  options: OrderFilterOptions | null;
  applying: boolean;
  idPrefix: string;
  className?: string;
}

const DATE_FIELDS: { value: OrderDateField; label: string }[] = [
  { value: "placed_at", label: "Order date" },
  { value: "created_at", label: "Recorded" },
  { value: "delivery_date", label: "Delivery date" },
  { value: "synced_at", label: "Read back" },
];

const PRESETS: { value: Preset; label: string }[] = [
  { value: "any", label: "Any time" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "month", label: "This month" },
];

const OUTCOMES: { value: OrderOutcomeFilter | ""; label: string }[] = [
  { value: "", label: "Any outcome" },
  { value: "placed", label: "Placed" },
  { value: "failed_over_target", label: "Over target" },
  { value: "unconfirmed", label: "Unconfirmed" },
  // Never sent: the run stopped before anything reached Flipkart.
  { value: "failed", label: "Failed" },
  // Taken by Flipkart, then cancelled there — by Flipkart or at the account's
  // request; each row says which. Until 26 Oct 2026 "Failed" returned these too.
  { value: "cancelled", label: "Cancelled on Flipkart" },
  { value: "pending", label: "Pending" },
];

type GroupKey = "confirmed" | "cancelled" | "others";

/** The spec's three groups, by the portal's reading of each wording. */
const GROUPS: { key: GroupKey; label: string; states: DeliveryState[] }[] = [
  { key: "confirmed", label: "Confirmed", states: ["delivered", "in_transit"] },
  { key: "cancelled", label: "Cancelled", states: ["cancelled"] },
  { key: "others", label: "Others", states: ["returned", "failed", "unknown"] },
];

/**
 * Wordings the spec's picker (ui-evidence.md S4) files differently from the
 * portal's delivery reading. The reading stays what it is — a cancellation
 * request is not yet a cancellation — but the operator finds the wording
 * where the spec's picker put it.
 */
const GROUP_OVERRIDES: Record<string, GroupKey> = {
  "cancellation requested": "cancelled",
  "order pending": "others",
  "return started": "confirmed",
};

function groupOf(wording: string, state: DeliveryState): GroupKey {
  const override = GROUP_OVERRIDES[wording.trim().toLowerCase()];
  if (override) return override;
  return GROUPS.find((group) => group.states.includes(state))?.key ?? "others";
}

/** A delivery state in the linked-filter chip's words. */
const DELIVERY_STATE_WORDS: Record<DeliveryState, string> = {
  delivered: "delivered",
  cancelled: "cancelled",
  returned: "returned",
  failed: "not delivered",
  in_transit: "on its way",
  unknown: "not known yet",
};

/** Whether a filter narrows the list at all — the same reading for draft and applied. */
export function isNarrowed(f: OrderFilters): boolean {
  return Boolean(
    f.q?.trim() ||
      f.status ||
      (f.statuses?.length ?? 0) > 0 ||
      (f.platform_statuses?.length ?? 0) > 0 ||
    (f.processing_stages?.length ?? 0) > 0 ||
    (f.error_codes?.length ?? 0) > 0 ||
      (f.delivery_states?.length ?? 0) > 0 ||
      (f.order_ids?.length ?? 0) > 0 ||
      (f.emails?.length ?? 0) > 0 ||
      (f.tracking_ids?.length ?? 0) > 0 ||
      (f.platforms?.length ?? 0) > 0 ||
      f.bob === true ||
      f.bob === false ||
      f.card?.trim() ||
      f.billed_gst?.trim() ||
      f.invoice_number?.trim() ||
      f.invoice_gstin?.trim() ||
      f.invoice_month?.trim() ||
      f.date_from ||
      f.date_to ||
      f.job_id != null ||
      f.order_row != null ||
      f.account_id != null,
  );
}

/** Multi-value fields are typed as one line; the server splits on space/comma. */
function joined(values?: string[] | null): string {
  return (values ?? []).join(" ");
}
function split(text: string): string[] {
  return text.split(/[\s,]+/).map((v) => v.trim()).filter(Boolean);
}

type MultiValueInputProps = Omit<TextInputProps, "value" | "onChange"> & {
  values?: string[] | null;
  onValues: (next: string[]) => void;
};

/**
 * One line of several values. The text is the typist's — splitting it back
 * into the draft on every keystroke and re-joining it for display would eat
 * the space or comma that starts the next value as it is typed. The draft
 * still gets the split list at once, so Apply needs nothing extra; outside
 * changes (Reset, a restored view) replace the text only when they actually
 * differ from what it already says.
 */
function MultiValueInput({ values, onValues, ...inputProps }: MultiValueInputProps) {
  const canonical = joined(values);
  const [text, setText] = useState(canonical);
  useEffect(() => {
    setText((current) => (split(current).join(" ") === canonical ? current : canonical));
  }, [canonical]);
  return (
    <TextInput
      {...inputProps}
      spellCheck={false}
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        onValues(split(event.target.value));
      }}
    />
  );
}

export function OrderUnitsFilters({
  draft,
  onChange,
  onApply,
  onReset,
  appliedNarrowed,
  options,
  applying,
  idPrefix,
  className = "",
}: OrderUnitsFiltersProps) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const picker = useRef<HTMLDivElement>(null);
  useDismiss(picker, pickerOpen, () => setPickerOpen(false));
  const preset = shownPreset(draft);

  const grouped = useMemo(() => {
    const wordings = options?.platform_statuses ?? [];
    return GROUPS.map((group) => ({
      ...group,
      wordings: wordings.filter((w) => groupOf(w.value, w.state) === group.key),
    })).filter((group) => group.wordings.length > 0);
  }, [options]);

  const ticked = new Set(draft.platform_statuses ?? []);
  const tickedCount = ticked.size;
  const setTicked = (next: Set<string>) => onChange({ platform_statuses: [...next] });
  const narrowed = isNarrowed(draft);

  return (
    <section
      aria-label="Filters"
      className={`flex flex-col gap-4 rounded-card border border-line bg-surface p-4 ${className}`}
    >
      <div className="flex flex-wrap items-end gap-3">
        <Field id={`${idPrefix}-date-field`} label="Date" className="w-full sm:w-[160px]">
          <Select
            value={draft.date_field ?? "placed_at"}
            onChange={(event) => onChange({ date_field: event.target.value as OrderDateField })}
          >
            {DATE_FIELDS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field id={`${idPrefix}-preset`} label="Range" className="w-full sm:w-[160px]">
          <Select
            value={preset}
            onChange={(event) => {
              const next = event.target.value as Preset;
              // The preset is remembered beside its dates, so Quick Save can
              // keep "Last 7 days" rather than this week's dates (spec FR-011).
              if (next !== "custom") onChange({ ...presetRange(next), date_preset: next === "any" ? null : next });
            }}
          >
            {PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
            {preset === "custom" ? <option value="custom">Custom range</option> : null}
          </Select>
        </Field>
        <Field id={`${idPrefix}-from`} label="Start date" className="w-full sm:w-[170px]">
          <TextInput
            type="date"
            value={draft.date_from ?? ""}
            onChange={(event) => onChange({ date_from: event.target.value || null, date_preset: null })}
          />
        </Field>
        <Field id={`${idPrefix}-to`} label="End date" className="w-full sm:w-[170px]">
          <TextInput
            type="date"
            value={draft.date_to ?? ""}
            onChange={(event) => onChange({ date_to: event.target.value || null, date_preset: null })}
          />
        </Field>
        {draft.date_from || draft.date_to ? (
          <Button
            size="sm"
            variant="ghost"
            aria-label="Clear the date range"
            onClick={() => onChange({ date_from: null, date_to: null, date_preset: null })}
          >
            × Clear dates
          </Button>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field id={`${idPrefix}-platform`} label="Platform">
          <Select
            value={draft.platforms?.[0] ?? ""}
            onChange={(event) => onChange({ platforms: event.target.value ? [event.target.value] : [] })}
          >
            <option value="">All platforms</option>
            <option value="flipkart">FLIPKART</option>
          </Select>
        </Field>
        <Field id={`${idPrefix}-bob`} label="BoB Orders" help="Orders this tool placed, or the ones it read back or was handed.">
          <Select
            value={draft.bob === true ? "yes" : draft.bob === false ? "no" : ""}
            onChange={(event) =>
              onChange({ bob: event.target.value === "yes" ? true : event.target.value === "no" ? false : null })
            }
          >
            <option value="">All</option>
            <option value="yes">Placed by this tool</option>
            <option value="no">Not placed by this tool</option>
          </Select>
        </Field>
        <Field id={`${idPrefix}-q`} label="Product name">
          <TextInput
            type="search"
            placeholder="Filter by product name"
            value={draft.q ?? ""}
            onChange={(event) => onChange({ q: event.target.value })}
          />
        </Field>
        <Field id={`${idPrefix}-order-ids`} label="Order ID" help="Several allowed, separated by spaces.">
          <MultiValueInput
            placeholder="OD3389… OD3390…"
            values={draft.order_ids}
            onValues={(order_ids) => onChange({ order_ids })}
          />
        </Field>
        <Field id={`${idPrefix}-emails`} label="Platform email" help="Several allowed.">
          <MultiValueInput
            placeholder="abc@gmail.com xyz@gmail.com"
            values={draft.emails}
            onValues={(emails) => onChange({ emails })}
          />
        </Field>
        <Field id={`${idPrefix}-tracking`} label="Tracking ID" help="Several allowed.">
          <MultiValueInput
            placeholder="TRK… TRK…"
            values={draft.tracking_ids}
            onValues={(tracking_ids) => onChange({ tracking_ids })}
          />
        </Field>
        {/* Sent as typed; the server matches on the digits alone, and a value with
            none matches nothing — so isNarrowed counts any non-blank value. */}
        <Field
          id={`${idPrefix}-card`}
          label="Card Number"
          help="Matched on its digits, with or without spaces — the last four, say."
        >
          <TextInput
            type="search"
            inputMode="numeric"
            placeholder="Filter by card number"
            // The server refuses a longer value (422 on load, on save): cap it
            // where it is typed, so the rule never shows up as an error.
            maxLength={CARD_MAX_LENGTH}
            value={draft.card ?? ""}
            onChange={(event) => onChange({ card: event.target.value.slice(0, CARD_MAX_LENGTH) })}
          />
        </Field>
        <Field id={`${idPrefix}-processing-stage`} label="Processing stage" help="Checkout step where the attempt completed or stopped.">
          <Select
            id={`${idPrefix}-processing-stage`}
            value={draft.processing_stages?.[0] ?? ""}
            onChange={(event) =>
              onChange({ processing_stages: event.target.value ? [event.target.value] : [] })
            }
          >
            <option value="">All stages</option>
            {(options?.processing_stages ?? []).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.value} ({opt.count})
              </option>
            ))}
          </Select>
        </Field>
        <Field id={`${idPrefix}-error-code`} label="Error code" help="Structured error code when an attempt failed or needed manual help.">
          <Select
            id={`${idPrefix}-error-code`}
            value={draft.error_codes?.[0] ?? ""}
            onChange={(event) =>
              onChange({ error_codes: event.target.value ? [event.target.value] : [] })
            }
          >
            <option value="">All error codes</option>
            {(options?.error_codes ?? []).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.value} ({opt.count})
              </option>
            ))}
          </Select>
        </Field>
        <Field id={`${idPrefix}-outcome`} label="Outcome" help="What this tool recorded for the attempt. Failed: never reached Flipkart.">
          <Select
            value={draft.status ?? ""}
            onChange={(event) => onChange({ status: event.target.value as OrderOutcomeFilter | "" })}
          >
            {OUTCOMES.map((o) => (
              <option key={o.value || "any"} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          id={`${idPrefix}-status`}
          label="Order status"
          help="Flipkart's own wording, grouped."
          className="lg:col-span-2"
        >
          {() => (
            <div ref={picker} className="relative">
              <button
                type="button"
                id={`${idPrefix}-status`}
                aria-haspopup="true"
                aria-expanded={pickerOpen}
                onClick={() => setPickerOpen((open) => !open)}
                className="flex h-[38px] w-full items-center justify-between rounded-control border border-line bg-surface px-3 text-left text-[13px] text-ink"
              >
                <span>
                  {tickedCount === 0
                    ? "Select status"
                    : `${tickedCount} ${tickedCount === 1 ? "status" : "statuses"} selected`}
                </span>
                <span aria-hidden="true" className="text-[16px] leading-none text-muted">
                  ▾
                </span>
              </button>
              {pickerOpen ? (
                <div
                  role="group"
                  aria-label="Order status wordings"
                  className="absolute left-0 top-[42px] z-20 w-full min-w-[320px] rounded-card border border-line bg-surface p-3 shadow-lg"
                >
                  {grouped.length === 0 ? (
                    <p className="text-[12px] text-ink-2">
                      No platform status has been read back yet. Run a read-back first.
                    </p>
                  ) : (
                    grouped.map((group) => {
                      const values = group.wordings.map((w) => w.value);
                      const all = values.every((v) => ticked.has(v));
                      const some = !all && values.some((v) => ticked.has(v));
                      return (
                        <fieldset key={group.key} className="mb-3 last:mb-0">
                          <legend className="sr-only">{group.label}</legend>
                          <label className="flex items-center gap-2.5 text-[13px] font-semibold text-ink">
                            <input
                              type="checkbox"
                              className="size-4 accent-accent"
                              checked={all}
                              // The native mixed state: browsers derive it from
                              // `indeterminate`, which has no attribute form.
                              ref={(el) => {
                                if (el) el.indeterminate = some;
                              }}
                              onChange={() => {
                                const next = new Set(ticked);
                                if (all) values.forEach((v) => next.delete(v));
                                else values.forEach((v) => next.add(v));
                                setTicked(next);
                              }}
                            />
                            {group.label}
                          </label>
                          <div className="mt-1.5 flex flex-wrap gap-1.5 pl-6">
                            {group.wordings.map((w) => {
                              const on = ticked.has(w.value);
                              const meta = statusMeta(
                                w.state === "delivered"
                                  ? "done"
                                  : w.state === "cancelled" || w.state === "returned" || w.state === "failed"
                                    ? "failed"
                                    : "pending",
                              );
                              return (
                                <button
                                  key={w.value}
                                  type="button"
                                  aria-pressed={on}
                                  onClick={() => {
                                    const next = new Set(ticked);
                                    if (on) next.delete(w.value);
                                    else next.add(w.value);
                                    setTicked(next);
                                  }}
                                  className={`rounded-full border px-2.5 py-1 text-[12px] ${
                                    on
                                      ? "border-ink bg-ink text-surface"
                                      : "border-line bg-page text-ink hover:border-ink"
                                  }`}
                                  title={`${formatCount(w.count)} unit${w.count === 1 ? "" : "s"} · read as ${meta.label}`}
                                >
                                  {w.value}{" "}
                                  <span className={on ? "opacity-70" : "text-muted"}>({formatCount(w.count)})</span>
                                </button>
                              );
                            })}
                          </div>
                        </fieldset>
                      );
                    })
                  )}
                  <div className="mt-2 flex justify-end">
                    <Button size="sm" onClick={() => setPickerOpen(false)}>
                      Done
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </Field>

        {/* Finding orders by GST and by invoice. Sent as typed, capped at the
            server's limits; the server reads GSTINs upper-cased with spaces out. */}
        <Field
          id={`${idPrefix}-billed-gst`}
          label="Billed GST"
          help="GSTIN or its start — the one the order was billed under."
        >
          <TextInput
            type="search"
            placeholder="Filter by GSTIN"
            spellCheck={false}
            autoCapitalize="characters"
            maxLength={GSTIN_MAX_LENGTH}
            value={draft.billed_gst ?? ""}
            onChange={(event) => onChange({ billed_gst: event.target.value.slice(0, GSTIN_MAX_LENGTH) })}
          />
        </Field>
        <Field id={`${idPrefix}-invoice-number`} label="Invoice number" help="Any part of the number.">
          <TextInput
            type="search"
            placeholder="Filter by invoice number"
            spellCheck={false}
            maxLength={INVOICE_NUMBER_MAX_LENGTH}
            value={draft.invoice_number ?? ""}
            onChange={(event) =>
              onChange({ invoice_number: event.target.value.slice(0, INVOICE_NUMBER_MAX_LENGTH) })
            }
          />
        </Field>
        <Field id={`${idPrefix}-invoice-gstin`} label="Invoice GSTIN" help="Seller's or buyer's GSTIN, or its start.">
          <TextInput
            type="search"
            placeholder="Filter by GSTIN"
            spellCheck={false}
            autoCapitalize="characters"
            maxLength={GSTIN_MAX_LENGTH}
            value={draft.invoice_gstin ?? ""}
            onChange={(event) => onChange({ invoice_gstin: event.target.value.slice(0, GSTIN_MAX_LENGTH) })}
          />
        </Field>
        {/* A month picker where the browser has one; elsewhere a text box that
            says what to type. A wrong month is refused by the server, not ignored. */}
        <Field id={`${idPrefix}-invoice-month`} label="Invoice month" help="Orders with an invoice dated that month.">
          <TextInput
            type="month"
            placeholder="YYYY-MM"
            pattern="\d{4}-\d{2}"
            value={draft.invoice_month ?? ""}
            onChange={(event) => onChange({ invoice_month: event.target.value || null })}
          />
        </Field>
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2">
        {draft.job_id != null || draft.order_row != null || (draft.delivery_states?.length ?? 0) > 0 ? (
          <div className="mr-auto flex flex-wrap items-center gap-2">
            {/* One order, as an alert links to it: said here, with the way back. */}
            {draft.order_row != null ? (
              <span className="text-[12px] text-ink-2">
                Showing one order only ·{" "}
                <button type="button" className="underline" onClick={() => onChange({ order_row: null })}>
                  show all orders
                </button>
              </span>
            ) : null}
            {draft.job_id != null ? (
              <span className="text-[12px] text-ink-2">
                Showing run #{draft.job_id} only ·{" "}
                <button type="button" className="underline" onClick={() => onChange({ job_id: null })}>
                  show all runs
                </button>
              </span>
            ) : null}
            {/* The delivery states have no control of their own: they arrive
                with the dashboard's "To be delivered" link (or a saved view),
                so they show here, where they can be taken off. */}
            {draft.delivery_states?.length ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-page px-2.5 py-1 text-[12px] text-ink">
                Delivery: {draft.delivery_states.map((state) => DELIVERY_STATE_WORDS[state] ?? state).join(" or ")}
                <button
                  type="button"
                  aria-label="Remove the delivery filter"
                  className="text-[14px] leading-none text-muted hover:text-ink"
                  onClick={() => onChange({ delivery_states: undefined })}
                >
                  ×
                </button>
              </span>
            ) : null}
          </div>
        ) : null}
        <Button size="sm" variant="secondary" onClick={onReset} disabled={!narrowed && !appliedNarrowed}>
          Reset Filters
        </Button>
        <Button size="sm" variant="primary" onClick={onApply} loading={applying}>
          Apply Filters
        </Button>
      </div>
    </section>
  );
}
