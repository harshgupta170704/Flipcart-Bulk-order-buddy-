/**
 * What the selected accounts have left under the unit limits, shown BEFORE the
 * run starts, in the order preview.
 *
 * This is the whole point of checking: finding out afterwards means burnt
 * accounts — orders rejected, accounts flagged, and a run that reports failures
 * for a reason nobody asked about. The order form asks the server the run's
 * own question (see useRunCheck): which IDs cannot buy (no room left under a
 * limit, too little room for one order, or ordered recently) and how many
 * units the rest can really still take, with this order's limit changes.
 *
 * The answer holds in every mode: with Smart ID Selection on those IDs are set
 * aside before the run starts, so this says how many; with it off, and in an
 * AI run, the same IDs are skipped when their turn comes, so this says that
 * instead. The units available, and a warning when the run asks for more, are
 * shown either way.
 *
 * It never blocks the run. A failed check is reported as a check that failed,
 * not as a reason to refuse: the server enforces the limits again when the run
 * actually starts.
 */
import { countLabel, formatCount } from "../lib/format";
import type { RunCheckResult } from "../lib/types";
import type { RunCheckState } from "../lib/useRunCheck";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { limitName, limitRule, remainingUnits } from "./LimitsData";
import { Spinner } from "./Spinner";

const SMART_ID_OFF_LINE = "Smart ID Selection is off: each ID is checked when its turn comes.";
const AI_RUN_LINE = "In an AI run nothing is set aside before it starts: each ID is checked when its turn comes.";

export interface LimitUsageSummaryProps {
  /** The form's room check, as it stands. */
  check: RunCheckState;
  /** Ask again, after a failed check. */
  onRetry: () => void;
  /** Units this run is asking for. 0 skips the "not enough" comparison. */
  requestedUnits: number;
  /** The order form's Smart ID Selection switch. */
  smartId: boolean;
  /** An AI run keeps the check at each ID's turn, Smart ID on or off. */
  aiRun?: boolean;
  className?: string;
}

/** "6 IDs will be set aside (2 at their unit limit, 3 with too little room for
 *  one order, 1 ordered recently)." Reasons with no ID are left out. */
export function setAsideLine(
  run: Pick<RunCheckResult, "set_aside" | "set_aside_at_limit" | "set_aside_short" | "set_aside_ordered_recently">,
): string {
  if (!(run.set_aside > 0)) return "No ID will be set aside.";
  const why: string[] = [];
  if (run.set_aside_at_limit > 0) {
    why.push(`${formatCount(run.set_aside_at_limit)} at ${run.set_aside_at_limit === 1 ? "its" : "their"} unit limit`);
  }
  if (run.set_aside_short > 0) {
    why.push(`${formatCount(run.set_aside_short)} with too little room for one order`);
  }
  if (run.set_aside_ordered_recently > 0) {
    why.push(`${formatCount(run.set_aside_ordered_recently)} ordered recently`);
  }
  return `${countLabel(run.set_aside, "ID")} will be set aside${why.length ? ` (${why.join(", ")})` : ""}.`;
}

export function LimitUsageSummary({
  check,
  onRetry,
  requestedUnits,
  smartId,
  aiRun = false,
  className = "",
}: LimitUsageSummaryProps) {
  /* The run sets IDs aside before it starts only with Smart ID on, and never
     in an AI run, which checks each ID at its turn. Only what is said
     depends on it: the check, and its numbers, are the same either way. */
  const setAside = smartId && !aiRun;

  /* Said whenever no ID is set aside before the run, whatever the check is doing. */
  const turnLine = setAside ? null : (
    <p className="text-[12px] leading-snug text-ink-2">{smartId && aiRun ? AI_RUN_LINE : SMART_ID_OFF_LINE}</p>
  );

  if (check.kind === "idle") {
    return turnLine ? <div className={`flex flex-col gap-2 ${className}`}>{turnLine}</div> : null;
  }

  if (check.kind === "loading") {
    return (
      <div className={`flex flex-col gap-2 ${className}`}>
        {turnLine}
        <p aria-live="polite" className="flex items-center gap-2 text-[12px] text-ink-2">
          <Spinner size={13} />
          Checking unit limits for the selected accounts…
        </p>
      </div>
    );
  }

  if (check.kind === "error" && check.refusedChanges) {
    // The order would be refused the same way, so it is not said to be able
    // to start: the change has to be put right under Limits for this order.
    return (
      <div className={`flex flex-col gap-2 ${className}`}>
        {turnLine}
        <p role="alert" className="text-[12px] text-ink">
          This order&rsquo;s limit changes were refused: {check.message}
        </p>
      </div>
    );
  }

  if (check.kind === "error") {
    return (
      <div className={`flex flex-col items-start gap-2 ${className}`}>
        {turnLine}
        <p role="status" className="text-[12px] text-ink-2">
          Could not check the unit limits: {check.message} The run can still be started — the
          server checks the limits again before it spends an account.
        </p>
        <Button size="sm" onClick={onRetry}>
          Check again
        </Button>
      </div>
    );
  }

  const { run } = check;
  const rows = Array.isArray(run.limits) ? run.limits : [];
  const available = typeof run.units_available === "number" ? run.units_available : null;
  /* Taken out first by "Skip IDs that already bought this product", with
     Smart ID on or off; the lines below count only the rest. */
  const bought = typeof run.already_bought === "number" ? run.already_bought : 0;
  const short = available !== null && requestedUnits > 0 && available < requestedUnits;
  /* With nothing set aside before the run, "Exclude IDs" still passes these
     over, at their turn: said here, as the set-aside line is not shown then. */
  const recentAtTurn = !setAside && run.set_aside_ordered_recently > 0 ? run.set_aside_ordered_recently : 0;
  /* The IDs the run will not try at all. With nothing set aside before the
     run, IDs at their unit limit are still tried (and skipped at their turn),
     so they count as tried; "Units available" says what can be ordered. */
  const leftOut = bought + (setAside ? run.set_aside : run.set_aside_ordered_recently);
  const selected = typeof run.accounts === "number" ? run.accounts : null;

  return (
    <div className={`flex flex-col gap-2 ${className}`} aria-live="polite">
      {turnLine}
      <div className="flex flex-col gap-1 rounded-control border border-line bg-page px-3.5 py-3">
        {bought > 0 ? (
          <p className="text-[13px] font-semibold leading-relaxed text-ink">
            {countLabel(bought, "ID")} already bought this product and will be passed over.
          </p>
        ) : null}
        {setAside ? <p className="text-[13px] font-semibold leading-relaxed text-ink">{setAsideLine(run)}</p> : null}
        {recentAtTurn > 0 ? (
          <p className="text-[13px] font-semibold leading-relaxed text-ink">
            {countLabel(recentAtTurn, "ID")} ordered recently (Exclude IDs) and will be passed over.
          </p>
        ) : null}
        {leftOut > 0 && selected !== null ? (
          <p className="text-[13px] leading-relaxed text-ink">
            IDs the run will try: {formatCount(Math.max(0, selected - leftOut))} of {formatCount(selected)} selected
          </p>
        ) : null}
        {available !== null ? (
          <p
            className={
              setAside ? "text-[12px] leading-snug text-ink-2" : "text-[13px] font-semibold leading-relaxed text-ink"
            }
          >
            Units available: {formatCount(available)}
          </p>
        ) : null}
        {run.limits_apply === false ? (
          <p className="text-[12px] leading-snug text-ink-2">No unit limit applies to this product</p>
        ) : null}
        {rows.length > 0 ? (
          <ul className="mt-1 flex flex-col gap-1">
            {rows.map((row, index) => (
              <li key={`${row.limit_id ?? "added"}-${index}`} className="text-[12px] leading-snug text-ink-2">
                <span className="font-medium text-ink">{limitName(row.scope, row.value)}</span>{" "}
                <span className="text-muted">({limitRule(row.max_units, row.window_days)})</span>
                {row.limit_id === null ? <span className="text-muted">, this order only</span> : null} —{" "}
                {formatCount(remainingUnits(row))} left, {formatCount(row.used)} already used
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {short ? (
        <Alert tone="warning" title="Fewer units are available than this run asks for">
          The selected IDs can take {countLabel(available ?? 0, "unit")}, but the run asks for{" "}
          {countLabel(requestedUnits, "unit")}, so the last {countLabel(requestedUnits - (available ?? 0), "unit")}{" "}
          cannot be ordered. Select more IDs or ask for fewer units.
        </Alert>
      ) : null}
    </div>
  );
}

export default LimitUsageSummary;
