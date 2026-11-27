import type { CardPreviewLeaveOut, RunCheckLeaveOut } from "./types";

/**
 * What the card preview sends as `leave_out`, from the form's run check.
 * Only with Smart ID on, and not in an AI run, does the run set IDs aside for
 * the unit limits before it starts, so only then do the product facts, the
 * quantity and the limit changes matter; otherwise only "Skip IDs that
 * already bought this product" and "Exclude IDs" do. Sending just what
 * matters keeps the sheet from being read again each time a fact that
 * cannot change the answer is typed. null while no ID is selected.
 */
export interface RunSwitches {
  /** A mock run signs nobody in: every ID without a saved session is passed over. */
  mock?: boolean;
  /** "Spread orders across accounts": never-ordered IDs are tried first. */
  spreadOrders?: boolean;
  /** Total Attempts: IDs past it in the run's order are not tried. */
  totalAttempts?: number | null;
  /** "Use SuperCoins" with a minimum: IDs below it (or never read) are passed over. */
  useSupercoins?: boolean;
  minSupercoins?: number | null;
}

/** The run switches as the server takes them; only the ones that are on. */
function switches(run: RunSwitches): Partial<CardPreviewLeaveOut> {
  return {
    ...(run.mock ? { mock: true } : {}),
    ...(run.spreadOrders ? { smart_selection: true } : {}),
    ...(run.totalAttempts ? { total_attempts: run.totalAttempts } : {}),
    ...(run.useSupercoins ? { use_supercoins: true, ...(run.minSupercoins ? { min_supercoins: run.minSupercoins } : {}) } : {}),
  };
}

export function cardLeaveOut(
  check: RunCheckLeaveOut | null,
  smartId: boolean,
  aiRun: boolean,
  run: RunSwitches = {},
): CardPreviewLeaveOut | null {
  if (check === null) return null;
  if (smartId && !aiRun) return { ...check, smart_id: true, ai_run: false, ...switches(run) };
  return {
    qty: 1,
    qty_mode: "upto",
    total_quantity: null,
    exclude_ordered_days: check.exclude_ordered_days,
    ...(check.skip_bought_product ? { skip_bought_product: true, product_link: check.product_link } : {}),
    smart_id: smartId,
    ai_run: aiRun,
    ...switches(run),
  };
}
