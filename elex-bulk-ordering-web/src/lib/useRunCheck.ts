/**
 * The order form's room check, POST /unit-limits/run-check, asked as the form
 * changes: which IDs cannot buy (no room left, too little room for one order,
 * ordered recently), the units the rest can take, the limits that apply with
 * this order's changes, and the saved limits that apply before any change.
 *
 * One check feeds both the order preview's summary and the form's "Limits for
 * this order". It is asked once there is a selection and a product fact (or
 * an "Exclude IDs" window, or "Skip IDs that already bought this product" with
 * a link), after the typing has settled; a newer question retires an answer
 * still in the air. With that box ticked, IDs that already bought the product
 * are taken out first, as the run does, and counted apart.
 */
import { useCallback, useEffect, useState } from "react";
import { checkRun, errorMessage } from "./api";
import type {
  AccountSelection,
  ApplicableLimit,
  LimitOverride,
  QtyMode,
  RunCheckInput,
  RunCheckLeaveOut,
  RunCheckResult,
} from "./types";
import { selectionCount } from "./types";

/** Brand and category are typed by hand, so wait for the typing to settle. */
export const RUN_CHECK_DEBOUNCE_MS = 450;

export interface RunCheckArgs {
  /** The product facts the limits are matched on. Blank ones are left out. */
  brand: string;
  category: string;
  model?: string;
  /** The run's chosen seller, for seller-scoped limits ("" for the default). */
  seller?: string;
  /** The order's variant id, the link's pid; null when it names none. */
  variant?: string | null;
  /** Who the run would spend — ids or a filter, exactly as the job gets it. */
  selection: AccountSelection;
  /** Units in each order. */
  unitsPerOrder?: number;
  qtyMode?: QtyMode;
  /** Units to order in total; null for one order per account. */
  totalUnits?: number | null;
  /** The "Exclude IDs" window in days; null when it is off. */
  excludeDays?: number | null;
  /** "Skip IDs that already bought this product" is ticked. */
  skipBought?: boolean;
  /** The product link the order will use: it tells who already bought it. */
  productLink?: string;
  /**
   * This order's changes to the limits; none (or empty) for the saved ones.
   * Given as a function, it is handed the saved limits that apply as last
   * read for this product (null until then), so edits to a limit that no
   * longer applies can be left out.
   */
  limitOverrides?: LimitOverride[] | null | ((applicable: ApplicableLimit[] | null) => LimitOverride[] | null);
}

export type RunCheckState =
  | { kind: "idle" }
  | { kind: "loading" }
  /** `refusedChanges`: the server refused this order's limit changes (a 422
   *  whose detail names limit_overrides, see `refusesChanges`), so the run
   *  would be refused the same way. */
  | { kind: "error"; message: string; refusedChanges?: boolean }
  | { kind: "ready"; run: RunCheckResult };

/** The HTTP status an API error carries, when it carries one. */
function statusOf(err: unknown): number | null {
  const status = (err as { status?: unknown } | null)?.status;
  return typeof status === "number" ? status : null;
}

/**
 * Whether a 422 is about this order's limit changes. The server says so: its
 * refusal of a change comes as `{ message, field: "limit_overrides" }`, and a
 * malformed change as a validation error whose `loc` names the field. Any
 * other 422 (a selection over the per-run limit, say) is not blamed on them.
 */
export function refusesChanges(err: unknown): boolean {
  if (statusOf(err) !== 422) return false;
  const detail = (err as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    return detail.some((entry) => {
      const loc = (entry as { loc?: unknown } | null)?.loc;
      return Array.isArray(loc) && loc.includes("limit_overrides");
    });
  }
  return !!detail && typeof detail === "object" && (detail as { field?: unknown }).field === "limit_overrides";
}

export interface RunCheck {
  state: RunCheckState;
  /** The latest answer about this same product, kept while a newer check (a
   *  change typed, another selection) is on its way; null for another product. */
  last: RunCheckResult | null;
  /** Ask again, after a failed check. */
  retry: () => void;
  /** The check as it is asked, less its selection: what decides who the run
   *  passes over before it tries anyone (already bought, "Exclude IDs", the
   *  unit limits). The card preview sends it, so a pool card sheet pairs as
   *  the run will. null while no ID is selected. */
  leaveOut: RunCheckLeaveOut | null;
}

const IDLE: RunCheckState = { kind: "idle" };

export function useRunCheck({
  brand,
  category,
  model = "",
  seller = "",
  variant = null,
  selection,
  unitsPerOrder = 1,
  qtyMode = "upto",
  totalUnits = null,
  excludeDays = null,
  skipBought = false,
  productLink = "",
  limitOverrides = null,
}: RunCheckArgs): RunCheck {
  const [state, setState] = useState<RunCheckState>(IDLE);
  const [last, setLast] = useState<{ product: string; run: RunCheckResult } | null>(null);
  const [nonce, setNonce] = useState(0);

  const accounts = selectionCount(selection);
  const facts = {
    brand: brand.trim(),
    category: category.trim(),
    model: model.trim(),
    seller: seller.trim(),
    variant: variant?.trim() || null,
  };
  const hasFacts =
    facts.brand !== "" || facts.category !== "" || facts.model !== "" || facts.seller !== "" || facts.variant !== null;
  /* What one order needs, as the run reads it: a blank or unreadable size is
     the server's default of 1 (the form says the field needs fixing). */
  const qty = Number.isInteger(unitsPerOrder) && unitsPerOrder >= 1 ? unitsPerOrder : 1;
  const excludeWindow = excludeDays ?? null;
  /* With "Skip IDs that already bought this product" ticked, the link tells
     who bought it; without a link nobody can be told apart. */
  const boughtLink = skipBought ? productLink.trim() : "";
  /* With no product facts no limit can match, but "Exclude IDs" still stops
     IDs, so the check is worth asking for either way. */
  const worthAsking = accounts > 0 && (hasFacts || excludeWindow !== null || boughtLink !== "");

  /* What the applicable saved limits depend on: an answer about another
     product is never shown as this one's. */
  const product = JSON.stringify(facts);
  const lastRun = last !== null && last.product === product ? last.run : null;
  const given =
    typeof limitOverrides === "function"
      ? limitOverrides(lastRun && Array.isArray(lastRun.applicable_limits) ? lastRun.applicable_limits : null)
      : limitOverrides;
  const overrides = given && given.length > 0 ? given : null;
  /* The question less its selection, built once for the check and for the
     card preview, so the two can never ask about different runs. Built for
     any selection, even when the check itself is not worth asking: with
     Smart ID on, a limit that covers every value in its scope can set IDs
     aside with no product fact at all, and the pairing must know. */
  const leaveOut: RunCheckLeaveOut | null =
    accounts > 0
    ? {
        qty,
        qty_mode: qtyMode,
        total_quantity: totalUnits,
        exclude_ordered_days: excludeWindow,
        ...(facts.brand !== "" ? { brand: facts.brand } : {}),
        ...(facts.category !== "" ? { category: facts.category } : {}),
        ...(facts.model !== "" ? { model: facts.model } : {}),
        ...(facts.seller !== "" ? { seller: facts.seller } : {}),
        ...(facts.variant !== null ? { variant: facts.variant } : {}),
        ...(overrides !== null ? { limit_overrides: overrides } : {}),
        ...(boughtLink !== "" ? { skip_bought_product: true, product_link: boughtLink } : {}),
      }
    : null;
  /* One string standing for the whole request. The effect depends on it rather
     than on the selection object, so a parent re-render that hands over an
     equal-but-new selection does not re-ask the server. A filter carries its
     count: accounts that sign in while the form is open change how many the
     same filter matches, and an answer about the old number would say units
     are short that are not (24 Nov 2026: 542 answered, 544 ticked). */
  const key = worthAsking
    ? JSON.stringify([
        product,
        selection.mode === "ids"
          ? { ids: selection.ids }
          : { filter: selection.filter, count: selection.count },
        { qty, qtyMode, totalUnits, excludeWindow, boughtLink },
        overrides,
      ])
    : "";

  useEffect(() => {
    if (key === "") {
      setState(IDLE);
      return;
    }

    let live = true;
    const timer = window.setTimeout(() => {
      // Asked only when worth asking, so there is a selection: never null here.
      const body: RunCheckInput = {
        ...(leaveOut as RunCheckLeaveOut),
        ...(selection.mode === "ids" ? { account_ids: selection.ids } : { filter: selection.filter }),
      };

      setState({ kind: "loading" });
      checkRun(body).then(
        (run) => {
          if (!live) return;
          setState({ kind: "ready", run });
          setLast({ product, run });
        },
        (err: unknown) => {
          if (!live) return;
          setState({
            kind: "error",
            message: errorMessage(err),
            refusedChanges: overrides !== null && refusesChanges(err),
          });
        },
      );
    }, RUN_CHECK_DEBOUNCE_MS);

    return () => {
      // A newer key (or an unmount) retires this request: `live` drops the
      // reply that is already in the air so a stale one cannot land.
      live = false;
      window.clearTimeout(timer);
    };
    // `key` covers every value the request is built from, so the closure can
    // never read a stale fact, selection, quantity or change; `nonce` is the
    // retry.
  }, [key, nonce]);

  const retry = useCallback(() => setNonce((n) => n + 1), []);

  return { state, last: lastRun, retry, leaveOut };
}
