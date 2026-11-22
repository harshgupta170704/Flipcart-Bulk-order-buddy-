/**
 * What the selected accounts have left under the unit limits, shown BEFORE the
 * run starts.
 *
 * This is the whole point of checking: finding out afterwards means burnt
 * accounts — orders rejected, accounts flagged, and a run that reports failures
 * for a reason nobody asked about. So as soon as there is a brand or a category
 * and a selection, this asks the server and says plainly how many accounts are
 * already at a cap and how many units the selection can still absorb.
 *
 * It never blocks the run. A failed check is reported as a check that failed,
 * not as a reason to refuse: the server enforces the limits again when the run
 * actually starts.
 */
import { useEffect, useState } from "react";
import { checkUnitLimits, errorMessage } from "../lib/api";
import { countLabel, formatCount } from "../lib/format";
import type { AccountSelection, LimitCheckInput, LimitUsage } from "../lib/types";
import { selectionCount } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import {
  blockedAccounts,
  headroom,
  limitName,
  limitRule,
  remainingUnits,
} from "./LimitsData";
import { Spinner } from "./Spinner";

/** Brand and category are typed by hand, so wait for the typing to settle. */
const CHECK_DEBOUNCE_MS = 450;

type CheckState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; rows: LimitUsage[] };

const IDLE: CheckState = { kind: "idle" };

export interface LimitUsageSummaryProps {
  /** The product facts the limits are matched on. Blank ones are left out. */
  brand: string;
  category: string;
  model?: string;
  /** The run's chosen seller, for seller-scoped limits ("" for the default). */
  seller?: string;
  /** Who the run would spend — ids or a filter, exactly as the job gets it. */
  selection: AccountSelection;
  /** Units this run is asking for. 0 skips the "not enough" comparison. */
  requestedUnits: number;
  className?: string;
}

export function LimitUsageSummary({
  brand,
  category,
  model = "",
  seller = "",
  selection,
  requestedUnits,
  className = "",
}: LimitUsageSummaryProps) {
  const [state, setState] = useState<CheckState>(IDLE);
  const [nonce, setNonce] = useState(0);

  const accounts = selectionCount(selection);
  const facts = {
    brand: brand.trim(),
    category: category.trim(),
    model: model.trim(),
    seller: seller.trim(),
  };
  const hasFacts = facts.brand !== "" || facts.category !== "" || facts.model !== "" || facts.seller !== "";

  /* One string standing for the whole request. The effect depends on it rather
     than on the selection object, so a parent re-render that hands over an
     equal-but-new selection does not re-ask the server. */
  const key =
    hasFacts && accounts > 0
      ? JSON.stringify([
          facts,
          selection.mode === "ids" ? { ids: selection.ids } : { filter: selection.filter },
        ])
      : "";

  useEffect(() => {
    if (key === "") {
      setState(IDLE);
      return;
    }

    let live = true;
    const timer = window.setTimeout(() => {
      const body: LimitCheckInput = {};
      if (selection.mode === "ids") body.account_ids = selection.ids;
      else body.filter = selection.filter;
      if (facts.brand !== "") body.brand = facts.brand;
      if (facts.category !== "") body.category = facts.category;
      if (facts.model !== "") body.model = facts.model;
      if (facts.seller !== "") body.seller = facts.seller;

      setState({ kind: "loading" });
      checkUnitLimits(body).then(
        (rows) => {
          if (!live) return;
          setState({ kind: "ready", rows: Array.isArray(rows) ? rows : [] });
        },
        (err: unknown) => {
          if (!live) return;
          setState({ kind: "error", message: errorMessage(err) });
        },
      );
    }, CHECK_DEBOUNCE_MS);

    return () => {
      // A newer key (or an unmount) retires this request: `live` drops the
      // reply that is already in the air so a stale one cannot land.
      live = false;
      window.clearTimeout(timer);
    };
    // `key` covers every value the request is built from, so the closure can
    // never read a stale brand, category or selection; `nonce` is the retry.
  }, [key, nonce]);

  if (state.kind === "idle") return null;

  if (state.kind === "loading") {
    return (
      <p
        aria-live="polite"
        className={`flex items-center gap-2 text-[12px] text-ink-2 ${className}`}
      >
        <Spinner size={13} />
        Checking unit limits for the selected accounts…
      </p>
    );
  }

  if (state.kind === "error") {
    return (
      <div className={`flex flex-col items-start gap-2 ${className}`}>
        <p role="status" className="text-[12px] text-ink-2">
          Could not check the unit limits: {state.message} The run can still be started — the
          server checks the limits again before it spends an account.
        </p>
        <Button size="sm" onClick={() => setNonce((n) => n + 1)}>
          Check again
        </Button>
      </div>
    );
  }

  const rows = state.rows;

  if (rows.length === 0) {
    return (
      <p role="status" className={`text-[12px] text-ink-2 ${className}`}>
        No unit limit matches {describeFacts(facts)} — nothing in this run will be skipped for being
        over a cap.
      </p>
    );
  }

  const room = headroom(rows);
  const available = room.units;
  const short = available !== null && requestedUnits > 0 && available < requestedUnits;

  return (
    <div className={`flex flex-col gap-2 ${className}`} aria-live="polite">
      <div className="rounded-control border border-line bg-page px-3.5 py-3">
        <p className="text-[13px] leading-relaxed text-ink">
          <span className="font-semibold">
            {available === null
              ? "No cap applies"
              : `${countLabel(available, "unit")} available`}
          </span>
          <span className="text-ink-2">
            {" "}
            across the {formatCount(accounts)} selected{" "}
            {accounts === 1 ? "account" : "accounts"}
            {room.blocked > 0
              ? `, and at least ${countLabel(room.blocked, "account")} ${
                  room.blocked === 1 ? "has" : "have"
                } nothing left — ${room.blocked === 1 ? "it" : "they"} will be skipped rather than spent.`
              : " — none of them is at a cap yet."}
          </span>
        </p>

        <ul className="mt-2 flex flex-col gap-1">
          {rows.map((row) => (
            <li key={row.limit_id} className="text-[12px] leading-snug text-ink-2">
              <span className="font-medium text-ink">{limitName(row.scope, row.value)}</span>{" "}
              <span className="text-muted">({limitRule(row.max_units, row.window_days)})</span> —{" "}
              {formatCount(remainingUnits(row))} left, {formatCount(row.used)} already used
              {blockedAccounts(row) > 0
                ? `, ${countLabel(blockedAccounts(row), "account")} blocked`
                : ""}
            </li>
          ))}
        </ul>
      </div>

      {short ? (
        <Alert tone="warning" title="Fewer units are available than this run asks for">
          The selection can take {countLabel(available ?? 0, "unit")} before a limit stops it, but
          the run is asking for {countLabel(requestedUnits, "unit")}. Start it as it stands and the
          last {countLabel(requestedUnits - (available ?? 0), "unit")} cannot be ordered — select
          more accounts, lower the total, or raise the limit that is in the way.
        </Alert>
      ) : null}
    </div>
  );
}

/** "this brand and category" — named so the no-limits line is not abstract. */
function describeFacts(facts: { brand: string; category: string; model: string }): string {
  const parts: string[] = [];
  if (facts.brand !== "") parts.push(`brand “${facts.brand}”`);
  if (facts.category !== "") parts.push(`category “${facts.category}”`);
  if (facts.model !== "") parts.push(`model “${facts.model}”`);
  if (parts.length === 0) return "this product";
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

export default LimitUsageSummary;
