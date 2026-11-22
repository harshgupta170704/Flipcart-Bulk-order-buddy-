/**
 * Reading order status back from Flipkart.
 *
 * This sits on the ordering page, one card away from a button that spends real
 * money, so everything here is written to make the difference unmistakable: a
 * sync OPENS each account's order pages and writes down what it finds —
 * delivered or not, tracking id, delivery date, delivery OTP. It places
 * nothing, changes nothing on the platform and spends no unit quota. The
 * button is a secondary one for the same reason.
 */
import { useCallback, useState } from "react";
import { errorMessage, startSyncJob } from "../lib/api";
import { countLabel, formatCount } from "../lib/format";
import type { AccountSelection, Job } from "../lib/types";
import {
  SYNC_ORDERS_PER_ACCOUNT,
  SYNC_ORDERS_PER_ACCOUNT_MAX,
  selectionCount,
} from "../lib/types";
import { refreshAlertSummary } from "./AlertsSummary";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { Checkbox, Field, NumberInput } from "./Field";
import { ProgressStream } from "./ProgressStream";
import { RunAccountPicker, type RunAccountsState } from "./RunAccounts";
import { RunLink } from "./RunStates";

export interface SyncOrdersProps {
  /** The ordering page's own account load — reused, not fetched again. */
  accounts: RunAccountsState;
  /** Called when a sync run finishes, so the orders table can reload. */
  onSynced?: () => void;
  /**
   * True while an ordering run is being watched. A sync would compete with it
   * for the same browser concurrency, so it waits rather than racing.
   */
  blocked?: boolean;
  className?: string;
}

/** A sync can only read an order page while the account is logged in. */
const ELIGIBLE = ["session_valid"] as const;

export function SyncOrders({ accounts, onSynced, blocked = false, className = "" }: SyncOrdersProps) {
  /* "Every account with a valid session", as a filter rather than ids, so the
     run can cover thousands without the browser listing one of them. */
  const [selection, setSelection] = useState<AccountSelection>({
    mode: "filter",
    filter: { statuses: ["session_valid"] },
    count: 0,
  });
  /** Kept as text so a half-typed number is not silently read as 0. */
  const [perAccount, setPerAccount] = useState(String(SYNC_ORDERS_PER_ACCOUNT));
  const [importUnknown, setImportUnknown] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [running, setRunning] = useState(false);

  const runCount = selectionCount(selection);
  const busy = starting || running || blocked;

  const perAccountNumber = Number(perAccount);
  const perAccountValid =
    perAccount.trim() !== "" &&
    Number.isFinite(perAccountNumber) &&
    Number.isInteger(perAccountNumber) &&
    perAccountNumber >= 1 &&
    perAccountNumber <= SYNC_ORDERS_PER_ACCOUNT_MAX;
  const perAccountError = perAccountValid
    ? undefined
    : `A whole number from 1 to ${formatCount(SYNC_ORDERS_PER_ACCOUNT_MAX)}.`;

  const onStart = useCallback(async () => {
    if (busy || !perAccountValid) return;
    setStarting(true);
    setStartError(null);
    try {
      // One of the two, never both — the same contract every start endpoint has.
      const job: Job = await startSyncJob({
        account_ids: selection.mode === "ids" ? selection.ids : null,
        account_filter: selection.mode === "filter" ? selection.filter : undefined,
        limit_per_account: perAccountNumber,
        import_unknown: importUnknown,
      });
      setJobId(job.id);
      setRunning(job.status !== "completed" && job.status !== "failed");
    } catch (err) {
      setStartError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  }, [busy, importUnknown, perAccountNumber, perAccountValid, selection]);

  const onDone = useCallback(() => {
    setRunning(false);
    onSynced?.();
    // Fresh order data is exactly what the alert rules read, so the queue's
    // counts are stale the moment a sync finishes.
    refreshAlertSummary();
  }, [onSynced]);

  // A dropped WebSocket must never strand the panel on a run it cannot hear.
  const onDismiss = useCallback(() => {
    setJobId(null);
    setRunning(false);
    onSynced?.();
  }, [onSynced]);

  const hint = (() => {
    if (blocked) return "Waiting for the ordering run above to finish.";
    if (accounts.total === 0) return "Add accounts before syncing.";
    if (runCount === 0) return "No account with a valid session is selected.";
    if (!perAccountValid) return "Fix the orders-per-account number to start.";
    // An account whose session has gone stale is skipped, not failed: a sync
    // reads, so "never logged in" is nothing to report in red.
    return `Reads up to ${formatCount(perAccountNumber)} ${
      perAccountNumber === 1 ? "order" : "orders"
    } each for ${countLabel(runCount, "account")}. Nothing is placed or changed.`;
  })();

  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      <Card
        title="Sync from Flipkart"
        description="Reads order status back: delivered or not, tracking id, delivery date, the delivery OTP and the seller. It never places or changes an order — nothing here can buy anything."
      >
        <div className="flex flex-col gap-4">
          <RunAccountPicker
            state={accounts}
            selection={selection}
            onSelectionChange={setSelection}
            filterStatus={[...ELIGIBLE]}
            label="Accounts to read back"
            disabled={busy}
            emptyAction={<RunLink to="/accounts">Go to Accounts</RunLink>}
          />

          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:gap-6">
            <Field
              label="Orders per account"
              error={perAccountError}
              help={`The most recent ones first. Each order costs a page of its own — that is where the ids and the OTP live — so this decides how long the run takes. ${formatCount(SYNC_ORDERS_PER_ACCOUNT)} covers the first page of an account's order list, which is where anything still moving is.`}
              className="w-full sm:max-w-xs"
            >
              <NumberInput
                value={perAccount}
                min={1}
                max={SYNC_ORDERS_PER_ACCOUNT_MAX}
                step={1}
                inputMode="numeric"
                disabled={busy}
                onChange={(event) => setPerAccount(event.target.value)}
                className="max-w-[140px]"
              />
            </Field>

            <Checkbox
              label="Also record orders this tool did not place"
              help="Off by default. On an account with years of personal history this would bury the orders this tool placed under everything else it ever bought — and those rows then count towards the platform unit limits. Turn it on when orders made by hand or in the old portal should appear in this table too."
              checked={importUnknown}
              disabled={busy}
              onChange={(event) => setImportUnknown(event.target.checked)}
              className="sm:pt-1"
            />
          </div>

          <div aria-live="polite" className="empty:hidden">
            {startError ? (
              <Alert tone="error" title="Could not start the sync">
                {startError}
              </Alert>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Button
              loading={starting}
              disabled={busy || runCount === 0 || !perAccountValid}
              onClick={() => void onStart()}
            >
              {starting ? "Starting…" : "Sync from Flipkart"}
            </Button>
            <p className="text-[12px] text-ink-2">{hint}</p>
          </div>
        </div>
      </Card>

      {jobId !== null ? (
        <div className="flex flex-col gap-2">
          <ProgressStream jobId={jobId} onDone={onDone} title="Order sync" />
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" variant="ghost" onClick={onDismiss}>
              {running ? "Stop watching this sync" : "Hide this sync"}
            </Button>
            {running ? (
              <p className="text-[12px] text-ink-2">
                Stopping the watch only closes this view. The sync itself keeps reading — use Cancel
                run above to actually stop it.
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default SyncOrders;
