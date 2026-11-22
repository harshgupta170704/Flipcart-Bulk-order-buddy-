/**
 * Loading the unit limits, plus the display helpers the limits screen and the
 * ordering form both read.
 *
 * `limitValueLabel` is the load-bearing one. A limit with an empty value caps
 * EVERY value in its scope — the 20-handsets-a-month rule — and rendering that
 * as a blank cell is how someone deletes the rule that was protecting them. So
 * a blank value is always spelled out as "Any brand", never left empty.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, listUnitLimits } from "../lib/api";
import type { LimitScope, LimitUsage, UnitLimit } from "../lib/types";

export interface UnitLimitsState {
  /** null until a load has succeeded — "not loaded yet" is not "empty". */
  rows: UnitLimit[] | null;
  loading: boolean;
  /** Set on a failed load. `rows` keeps whatever last arrived. */
  error: string | null;
  reload: () => void;
}

/**
 * The whole list, fetched whole: it is one row per platform rule, a handful at
 * most, and the point of the screen is to read every rule at once.
 */
export function useUnitLimits(): UnitLimitsState {
  const [rows, setRows] = useState<UnitLimit[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* Saving a row reloads the list, so two loads can be in the air at once.
     Only the newest may land, or a slow earlier reply overwrites the fresh
     list with what the table looked like before the edit. */
  const ticket = useRef(0);

  const reload = useCallback(() => {
    ticket.current += 1;
    const mine = ticket.current;
    setLoading(true);
    listUnitLimits().then(
      (next) => {
        if (!alive.current || ticket.current !== mine) return;
        setRows(next);
        setError(null);
        setLoading(false);
      },
      (err: unknown) => {
        if (!alive.current || ticket.current !== mine) return;
        setError(errorMessage(err));
        setLoading(false);
      },
    );
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return { rows, loading, error, reload };
}

/* --- display -------------------------------------------------------------- */

const SCOPE_LABELS: Record<LimitScope, string> = {
  category: "Category",
  brand: "Brand",
  model: "Model",
  variant: "Variant",
  seller: "Seller",
};

const SCOPE_WORDS: Record<LimitScope, string> = {
  category: "category",
  brand: "brand",
  model: "model",
  variant: "variant",
  seller: "seller",
};

/** "Brand". Falls back to the raw value, so an unknown scope still reads. */
export function scopeLabel(scope: LimitScope): string {
  const label: string | undefined = SCOPE_LABELS[scope];
  return label ?? String(scope);
}

/** "brand" — the lower-case noun, for mid-sentence use. */
export function scopeWord(scope: LimitScope): string {
  const word: string | undefined = SCOPE_WORDS[scope];
  return word ?? String(scope);
}

/**
 * "vivo", or the explicit "Any brand" for the wildcard. Never an empty string:
 * a rule that applies to everything must not look like a half-filled row.
 */
export function limitValueLabel(scope: LimitScope, value: string): string {
  const trimmed = value.trim();
  return trimmed === "" ? `Any ${scopeWord(scope)}` : trimmed;
}

/** "Brand · vivo", or plain "Any brand" — one line naming a limit in prose. */
export function limitName(scope: LimitScope, value: string): string {
  const trimmed = value.trim();
  if (trimmed === "") return limitValueLabel(scope, trimmed);
  return `${scopeLabel(scope)} · ${trimmed}`;
}

/** "16 units / 31 days" — the rule itself, short enough for a table cell. */
export function limitRule(maxUnits: number, windowDays: number): string {
  return `${maxUnits} ${maxUnits === 1 ? "unit" : "units"} / ${windowDays} ${
    windowDays === 1 ? "day" : "days"
  }`;
}

/* --- reading a usage row -------------------------------------------------- */

function wholeCount(raw: unknown): number {
  if (typeof raw === "number" && Number.isFinite(raw)) return Math.max(0, Math.trunc(raw));
  // A `blocked` that arrives as a flag rather than a count still has to mean
  // something: one blocked account is the smallest truthful reading of `true`.
  if (typeof raw === "boolean") return raw ? 1 : 0;
  return 0;
}

/** How many of the checked accounts have nothing left under this limit. */
export function blockedAccounts(usage: LimitUsage): number {
  return wholeCount(usage.blocked);
}

/** Units still available across the checked accounts under this limit. */
export function remainingUnits(usage: LimitUsage): number {
  return wholeCount(usage.remaining);
}

export interface LimitHeadroom {
  /**
   * Units this selection can still order before a limit stops it — the FEWEST
   * any single limit allows, which is how the server computes an allowance.
   * null means no limit applies at all.
   */
  units: number | null;
  /**
   * Accounts already at a limit. The largest single limit's count rather than
   * the sum: one account can be blocked by several limits at once, and adding
   * those up would invent accounts that do not exist.
   */
  blocked: number;
}

/** Fold the per-limit rows into the two numbers an operator needs up front. */
export function headroom(rows: LimitUsage[]): LimitHeadroom {
  if (rows.length === 0) return { units: null, blocked: 0 };
  let units = Number.POSITIVE_INFINITY;
  let blocked = 0;
  for (const row of rows) {
    units = Math.min(units, remainingUnits(row));
    blocked = Math.max(blocked, blockedAccounts(row));
  }
  return { units: Number.isFinite(units) ? units : null, blocked };
}

export default useUnitLimits;
