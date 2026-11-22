/**
 * The quantity controls on the ordering form, and the arithmetic behind them.
 *
 * There are two ways to spend a run and they must never be mistaken for each
 * other, because they cost different things:
 *
 *  - Total quantity BLANK — one order per selected account. Spending is per
 *    account: pick 300 accounts and 300 orders are attempted.
 *  - Total quantity SET — orders of "units per order" are placed until the
 *    total is reached. Spending is to a total: accounts are used until the
 *    units are bought, and the rest are never touched.
 *
 * So the derived line is always on screen: whichever mode is active says, in
 * words, exactly how many orders of how many units it will attempt. The
 * alternative is an operator reading "300" as a total and burning 300 accounts.
 */
import type { ReactNode } from "react";
import { formatCount } from "../lib/format";
import { Field, NumberInput, Select, TextInput } from "./Field";

/** Every control here is a text box, so the draft is all strings. */
export interface QuantityDraft {
  totalQuantity: string;
  qtyPerOrder: string;
  totalAttempts: string;
  cartAmountLimit: string;
  maxDeliveryDays: string;
  brand: string;
  category: string;
}

/** A fresh form: single-order mode, one unit per order — today's behaviour. */
export const EMPTY_QUANTITY: QuantityDraft = {
  totalQuantity: "",
  qtyPerOrder: "1",
  totalAttempts: "",
  cartAmountLimit: "",
  // Required, and 3 unless changed: a slow delivery is refused before any
  // payment step (BulkOrderBuddy's default; audit of 19 Nov 2026).
  maxDeliveryDays: "3",
  brand: "",
  category: "",
};

export type QuantityErrors = Partial<Record<keyof QuantityDraft, string>>;

/** A whole number at or above `min`, or null when the text is not one. */
function wholeAtLeast(text: string, min: number): number | null {
  if (text.trim() === "") return null;
  const value = Number(text);
  if (!Number.isInteger(value) || value < min) return null;
  return value;
}

export function validateQuantity(draft: QuantityDraft): QuantityErrors {
  const errors: QuantityErrors = {};

  if (draft.qtyPerOrder.trim() === "") {
    errors.qtyPerOrder = "Enter how many units each order is for.";
  } else if (wholeAtLeast(draft.qtyPerOrder, 1) === null) {
    errors.qtyPerOrder = "Units per order is a whole number, 1 or more.";
  }

  if (draft.totalQuantity.trim() !== "" && wholeAtLeast(draft.totalQuantity, 1) === null) {
    errors.totalQuantity =
      "A whole number of units, 1 or more. Leave it blank for one order per account.";
  }

  if (draft.totalAttempts.trim() !== "") {
    const attempts = wholeAtLeast(draft.totalAttempts, 1);
    const total = wholeAtLeast(draft.totalQuantity, 1);
    const perOrder = wholeAtLeast(draft.qtyPerOrder, 1);
    if (attempts === null) {
      errors.totalAttempts =
        "A whole number of accounts, 1 or more. Leave it blank to try every selected account.";
    } else if (total !== null && perOrder !== null && attempts < Math.ceil(total / perOrder)) {
      errors.totalAttempts = `At least ${Math.ceil(total / perOrder)} attempts are needed to buy ${total} units at ${perOrder} per order.`;
    }
  }

  if (draft.cartAmountLimit.trim() !== "") {
    const amount = Number(draft.cartAmountLimit);
    if (!Number.isFinite(amount)) {
      errors.cartAmountLimit = "Enter the cart limit as a number, or leave it blank.";
    } else if (amount <= 0) {
      errors.cartAmountLimit = "A cart limit of zero or less would stop every order.";
    }
  }

  if (draft.maxDeliveryDays.trim() === "") {
    errors.maxDeliveryDays = "Enter the most days a delivery may take (3 is usual).";
  } else if (wholeAtLeast(draft.maxDeliveryDays, 1) === null) {
    errors.maxDeliveryDays = "A whole number of days, 1 or more.";
  }

  return errors;
}

/* --- what the run will actually do ---------------------------------------- */

export interface OrderPlan {
  /** "total" spends to a total across accounts; "account" spends per account. */
  mode: "total" | "account";
  /** Units in each order. 0 while the field is empty or invalid. */
  unitsPerOrder: number;
  /** The total being bought. null in per-account mode — there is no total. */
  totalUnits: number | null;
  /** Orders the plan needs. In per-account mode, one per selected account. */
  orders: number;
  /** Units in the final order, when the total does not divide evenly. */
  lastOrderUnits: number | null;
  /** Units the whole run is asking for — what a limit check is compared with. */
  requestedUnits: number;
}

/**
 * Read the draft the way the server will: `total_quantity` unset means one
 * order per account, set means keep ordering until the total is reached.
 */
export function orderPlan(draft: QuantityDraft, accountCount: number): OrderPlan {
  const perOrder = wholeAtLeast(draft.qtyPerOrder, 1) ?? 0;
  const total = wholeAtLeast(draft.totalQuantity, 1);

  if (total === null) {
    return {
      mode: "account",
      unitsPerOrder: perOrder,
      totalUnits: null,
      orders: accountCount,
      lastOrderUnits: null,
      requestedUnits: perOrder * accountCount,
    };
  }

  if (perOrder === 0) {
    return {
      mode: "total",
      unitsPerOrder: 0,
      totalUnits: total,
      orders: 0,
      lastOrderUnits: null,
      requestedUnits: total,
    };
  }

  const orders = Math.ceil(total / perOrder);
  const remainder = total % perOrder;
  return {
    mode: "total",
    unitsPerOrder: perOrder,
    totalUnits: total,
    orders,
    lastOrderUnits: remainder === 0 ? null : remainder,
    requestedUnits: total,
  };
}

function units(count: number): string {
  return `${formatCount(count)} ${count === 1 ? "unit" : "units"}`;
}

function orders(count: number): string {
  return `${formatCount(count)} ${count === 1 ? "order" : "orders"}`;
}

/** "7 orders of 3 units · last order: 1 unit" — the whole plan in one line. */
export function describePlan(plan: OrderPlan): string {
  if (plan.unitsPerOrder === 0) return "";
  // A total smaller than one batch is a single short order, so saying "1 order
  // of 5 units · last order: 2 units" would describe an order that never runs.
  if (plan.orders === 1 && plan.lastOrderUnits !== null) {
    return `1 order of ${units(plan.lastOrderUnits)}`;
  }
  const head = `${orders(plan.orders)} of ${units(plan.unitsPerOrder)}`;
  if (plan.lastOrderUnits === null) return head;
  return `${head} · last order: ${units(plan.lastOrderUnits)}`;
}

/* --- the section ---------------------------------------------------------- */

/** The fewest attempts that can buy the total: one account places one order. */
export function minAttempts(draft: Pick<QuantityDraft, "totalQuantity" | "qtyPerOrder">): number | null {
  const total = wholeAtLeast(draft.totalQuantity, 1);
  const perOrder = wholeAtLeast(draft.qtyPerOrder, 1);
  if (total === null || perOrder === null) return null;
  return Math.ceil(total / perOrder);
}

/**
 * Which slice of the controls to draw. The order form spreads them over its
 * numbered sections the way the operator reads a run: "quantity" in
 * 1. Product, "facts" (what unit limits match on) in 2. Email IDs, and
 * "limits" (cart amount, delivery days) in 4. Order Details.
 */
export type QuantityPart = "quantity" | "limits" | "facts";

export interface QuantityFieldsProps {
  draft: QuantityDraft;
  /** Only the errors that should be visible — the host decides when. */
  errors: QuantityErrors;
  disabled?: boolean;
  /**
   * Accounts the run will actually try — the selection, trimmed by the attempt
   * ceiling. It is the per-account mode's multiplier, so it has to be the
   * effective number rather than the number of rows that were ticked.
   */
  accountCount: number;
  onChange: (key: keyof QuantityDraft, value: string) => void;
  /** Which controls to draw; all of them when omitted. */
  part?: QuantityPart;
  /** Minimum or Upto, drawn beside the quantities when given. */
  qtyMode?: "minimum" | "upto";
  onQtyModeChange?: (mode: "minimum" | "upto") => void;
  /** Rendered after the controls (the unit-limit summary). */
  footer?: ReactNode;
  /** Drawn between the two cart/delivery limits: the order form's Final Amount Limit. */
  middle?: ReactNode;
  className?: string;
}

function RupeeInput(props: Parameters<typeof NumberInput>[0]) {
  return (
    <div className="relative">
      <NumberInput {...props} />
    </div>
  );
}

export function QuantityFields({
  draft,
  errors,
  disabled,
  accountCount,
  onChange,
  part,
  qtyMode,
  onQtyModeChange,
  footer,
  middle,
  className = "",
}: QuantityFieldsProps) {
  const plan = orderPlan(draft, accountCount);
  const planLine = describePlan(plan);
  const noFacts = draft.brand.trim() === "" && draft.category.trim() === "";
  const least = minAttempts(draft);
  const show = (which: QuantityPart) => part === undefined || part === which;

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      {show("quantity") ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field
              label="Total Quantity"
              error={errors.totalQuantity}
              hint="Total units"
              help="Units to order in total, spread across accounts. The run stops as soon as the total is reached, so accounts it never needed are left untouched. Leave it blank for one order per account."
            >
              <NumberInput
                name="total_quantity"
                min={1}
                step={1}
                inputMode="numeric"
                placeholder="One order per account"
                value={draft.totalQuantity}
                disabled={disabled}
                onChange={(event) => onChange("totalQuantity", event.target.value)}
              />
            </Field>

            <Field
              label="Quantity Per Order"
              required
              error={errors.qtyPerOrder}
              hint="Units per order"
              help="Units in each single order — the batch a total is bought in, and the size of the one order per account when no total is set. Flipkart caps some products (phones among them) at one unit per order; the run then orders one and records Flipkart's reason on that order."
            >
              <NumberInput
                name="qty_per_order"
                min={1}
                step={1}
                inputMode="numeric"
                value={draft.qtyPerOrder}
                disabled={disabled}
                onChange={(event) => onChange("qtyPerOrder", event.target.value)}
              />
            </Field>

            {qtyMode !== undefined && onQtyModeChange ? (
              <Field
                label="Mode"
                hint="Mode"
                help={
                  qtyMode === "minimum"
                    ? "Minimum: every order must carry all its units. If Flipkart allows fewer per order, that order is refused before payment."
                    : "Upto: if Flipkart allows fewer units per order than asked, the order goes ahead with what it allows."
                }
              >
                <Select
                  name="qty_mode"
                  value={qtyMode}
                  disabled={disabled}
                  onChange={(event) => onQtyModeChange(event.target.value as "minimum" | "upto")}
                >
                  <option value="upto">Upto</option>
                  <option value="minimum">Minimum</option>
                </Select>
              </Field>
            ) : null}

            <Field
              label="Total Attempts"
              error={errors.totalAttempts}
              hint={least !== null ? `Min: ${formatCount(least)}` : "Accounts to try"}
              help="A ceiling on how many ACCOUNTS this run will try at all — not on units. Each account places one order, so buying a total needs at least total ÷ per order attempts. Blank means every selected account is fair game."
            >
              <NumberInput
                name="total_attempts"
                min={least ?? 1}
                step={1}
                inputMode="numeric"
                placeholder="No ceiling"
                value={draft.totalAttempts}
                disabled={disabled}
                onChange={(event) => onChange("totalAttempts", event.target.value)}
              />
            </Field>
          </div>

          {/* The mode, in words, every time either number changes. */}
          <p aria-live="polite" className="text-[12px] leading-snug text-ink-2">
            {plan.mode === "total" ? (
              planLine === "" ? null : (
                <>
                  <span className="font-semibold text-ink">{planLine}</span> — {units(plan.totalUnits ?? 0)} in
                  total, from up to {formatCount(accountCount)} {accountCount === 1 ? "account" : "accounts"}.
                  Accounts it does not need are never used.
                </>
              )
            ) : (
              <>
                <span className="font-semibold text-ink">One order per account</span>: one order of{" "}
                {units(plan.unitsPerOrder)} for each of the {formatCount(accountCount)}{" "}
                {accountCount === 1 ? "account" : "accounts"} it will try — {units(plan.requestedUnits)} in all.
              </>
            )}
          </p>
        </>
      ) : null}

      {show("limits") ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field
            label="Cart Amount Limit (per unit)"
            error={errors.cartAmountLimit}
            help="Checked on the checkout before any payment step: an order whose cart total per unit is above this, or cannot be read, is not placed. The Final Amount Limit still caps what is paid."
          >
            <RupeeInput
              name="cart_amount_limit"
              min={0}
              step="0.01"
              placeholder="No limit"
              value={draft.cartAmountLimit}
              disabled={disabled}
              onChange={(event) => onChange("cartAmountLimit", event.target.value)}
            />
          </Field>

          {middle}

          <Field
            label="Max Delivery Days"
            required
            error={errors.maxDeliveryDays}
            help="Checked on the checkout before any payment step: an order whose delivery promise is further out than this many days, or cannot be read, is not placed."
          >
            <NumberInput
              name="max_delivery_days"
              min={1}
              step={1}
              inputMode="numeric"
              placeholder="3"
              value={draft.maxDeliveryDays}
              disabled={disabled}
              onChange={(event) => onChange("maxDeliveryDays", event.target.value)}
            />
          </Field>
        </div>
      ) : null}

      {show("facts") ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="Brand"
              error={errors.brand}
              help="Filled from the product. Saved on every order in the run, which is how later limit checks count it."
            >
              <TextInput
                name="brand"
                autoComplete="off"
                spellCheck={false}
                maxLength={128}
                placeholder="vivo"
                value={draft.brand}
                disabled={disabled}
                onChange={(event) => onChange("brand", event.target.value)}
              />
            </Field>

            <Field
              label="Category"
              error={errors.category}
              help="Filled from the product, e.g. Mobile / Handset. Matched against the category limits the same way."
            >
              <TextInput
                name="category"
                autoComplete="off"
                spellCheck={false}
                maxLength={128}
                placeholder="Mobile"
                value={draft.category}
                disabled={disabled}
                onChange={(event) => onChange("category", event.target.value)}
              />
            </Field>
          </div>
          {noFacts ? (
            <p className="text-[12px] leading-snug text-ink-2">
              With Brand and Category blank, per-brand and per-category limits cannot be matched to this run.
            </p>
          ) : null}
        </>
      ) : null}

      {footer}
    </div>
  );
}

export default QuantityFields;
