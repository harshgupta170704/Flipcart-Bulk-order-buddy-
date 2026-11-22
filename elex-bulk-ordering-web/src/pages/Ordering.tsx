/**
 * Place orders: the form that spends money, and nothing else. What became of
 * the orders is answered on Order Units History, which a finished run links to.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Job } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { OrderForm } from "../components/OrderForm";
import { ProgressStream } from "../components/ProgressStream";
import { useRunAccounts } from "../components/RunAccounts";

/** How long each bank page waits, for a run whose bank step a person
 *  finishes (POST /jobs/order `bank_step_by_hand`, with `bank_wait_seconds`):
 *  "about 6 minutes", or "a few minutes" when the run did not say. null for
 *  any other run. */
function bankStepWait(job: Job): string | null {
  if (job.payload?.bank_step_by_hand !== true) return null;
  const seconds = job.payload.bank_wait_seconds;
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return "a few minutes";
  if (seconds < 60) return `about ${Math.round(seconds)} seconds`;
  const minutes = Math.floor(seconds / 60);
  return `about ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

export default function Ordering() {
  /* Paged on the server. The form's picker does its own searching and counting
     against all accounts; this state only frames the loading, error and
     nothing-yet cases. */
  const accountsState = useRunAccounts();
  const reloadAccounts = accountsState.reload;

  const [jobId, setJobId] = useState<number | null>(null);
  const [runActive, setRunActive] = useState(false);
  /* How long each bank page waits, when the started run is one whose bank
     step a person finishes; null for any other run. */
  const [bankWait, setBankWait] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Smart Bulk Order — Ordering Suite";
  }, []);

  const onStarted = useCallback((job: Job) => {
    setJobId(job.id);
    setRunActive(job.status !== "completed" && job.status !== "failed");
    setBankWait(bankStepWait(job));
  }, []);

  const onRunDone = useCallback(() => {
    setRunActive(false);
    // Sessions go stale during a run, so the picker's counts should too.
    reloadAccounts();
  }, [reloadAccounts]);

  // Always leave a way out of the locked form — a dropped WebSocket must never
  // be able to strand the page on a run it can no longer hear from.
  const onDismissRun = useCallback(() => {
    setJobId(null);
    setRunActive(false);
    setBankWait(null);
  }, []);

  return (
    <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-5">
      <h1 className="sr-only">Smart Bulk Order</h1>
      <OrderForm accounts={accountsState} runActive={runActive} onStarted={onStarted} />

      {jobId !== null ? (
        <div className="flex flex-col gap-2">
          {runActive && bankWait !== null ? (
            <Alert tone="info" title="You finish the bank's step in this run">
              A browser window opens on the computer the tool runs on, one account at a time. Leave it
              alone until the bank&rsquo;s page opens, then type the card&rsquo;s password or OTP there.
              Each bank page waits {bankWait}; after that the tool marks that order unconfirmed (it may
              still go through) and moves on to the next account.
            </Alert>
          ) : null}
          <ProgressStream jobId={jobId} onDone={onRunDone} title="Ordering run" />
          <div className="flex flex-wrap items-center gap-3">
            <Link
              to={`/order-units?job_id=${jobId}`}
              className="inline-flex h-8 items-center rounded-control border border-line px-3 text-[13px] font-medium text-ink hover:bg-page"
            >
              View this run's orders
            </Link>
            <Button size="sm" variant="ghost" onClick={onDismissRun}>
              {runActive ? "Stop watching this run" : "Hide this run"}
            </Button>
            {runActive ? (
              <p className="text-[12px] text-ink-2">
                Stopping the watch unlocks this form. The run itself keeps going — use Cancel run
                above to actually stop it.
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
