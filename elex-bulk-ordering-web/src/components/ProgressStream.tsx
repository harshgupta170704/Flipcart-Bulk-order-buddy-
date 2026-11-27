import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cancelJob, errorMessage } from "../lib/api";
import { clampProgress, countLabel, formatDuration, formatSpent, formatTime } from "../lib/format";
import type { AiTrackerState, JobItem, JobItemStatus } from "../lib/types";
import { jobNotes } from "../lib/types";
import { useJobStream, type StreamEvent } from "../lib/useJobStream";
import { AiTrackerCard } from "./AiTrackerCard";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Spinner } from "./Spinner";
import { StatusBadge } from "./StatusBadge";

export interface ProgressStreamProps {
  /** null renders nothing — pages pass null until a run has been started. */
  jobId: number | null;
  /** Fired once, when the run reaches a terminal state. */
  onDone?: () => void;
  /** Heading above the bar. Default "Run progress". */
  title?: string;
  className?: string;
}

/**
 * A failed item whose message mentions the price cap is an over-target skip,
 * not a generic failure, so it gets its own amber "Over target" state.
 */
const OVER_TARGET = /over\s*-?\s*target/i;
/**
 * The run reports an order Flipkart never confirmed with a message that leads
 * with "unconfirmed:". It may exist, so it must not read as a plain failure.
 */
const UNCONFIRMED = /^unconfirmed:/i;

function itemStatusKey(item: JobItem): string {
  if (item.status === "failed" && item.message && UNCONFIRMED.test(item.message)) {
    return "unconfirmed";
  }
  if (item.status === "failed" && item.message && OVER_TARGET.test(item.message)) {
    return "failed_over_target";
  }
  return item.status;
}

const RANK: Record<JobItemStatus, number> = {
  running: 0,
  unconfirmed: 1,
  failed: 1,
  done: 1,
  skipped: 1,
  pending: 2,
};

/** How many IDs an "items_set_aside" event names. */
function setAsideCount(event: StreamEvent): number {
  return (event.groups ?? []).reduce(
    (sum, group) => sum + (Array.isArray(group?.account_ids) ? group.account_ids.length : 0),
    0,
  );
}

/** One line of the "Latest" list: the event's message, else what happened. */
function eventLine(event: StreamEvent): string {
  if (event.type === "items_set_aside") {
    return `${countLabel(setAsideCount(event), "ID")} set aside before the run started`;
  }
  if (event.type === "run_notes") {
    // The note just added is the last one.
    const notes = Array.isArray(event.notes) ? event.notes : [];
    return notes.length > 0 ? notes[notes.length - 1] : "note added";
  }
  return event.message ?? event.type.replace(/_/g, " ");
}

/** Said for an order run from before Smart ID Selection, which saved no value
 *  for it, as the Orders page's run settings say it. */
const SMART_ID_OLDER_RUN = "Smart ID Selection: not available (older run)";

/** Running first, then the most recent activity, then whatever is still waiting. */
function byActivity(a: JobItem, b: JobItem): number {
  const ra = RANK[a.status] ?? 3;
  const rb = RANK[b.status] ?? 3;
  if (ra !== rb) return ra - rb;
  if (ra === 1) {
    const ta = Date.parse(a.updated_at);
    const tb = Date.parse(b.updated_at);
    if (!Number.isNaN(ta) && !Number.isNaN(tb) && ta !== tb) return tb - ta;
  }
  return a.email.localeCompare(b.email);
}

/**
 * The live view of a run. Sessions, Coupons and Ordering all mount this once
 * they have a job id.
 */
export function ProgressStream({
  jobId,
  onDone,
  title = "Run progress",
  className = "",
}: ProgressStreamProps) {
  const { job, items, events, connected, error } = useJobStream(jobId);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelled, setCancelled] = useState(false);
  const notified = useRef(false);

  // Reset the one-shot guards whenever we start watching a different run.
  useEffect(() => {
    notified.current = false;
    setCancelling(false);
    setCancelError(null);
    setCancelled(false);
  }, [jobId]);

  const terminal = job?.status === "completed" || job?.status === "failed";
  const finishedEvent = useMemo(() => events.some((e) => e.type === "job_finished"), [events]);

  useEffect(() => {
    if (notified.current) return;
    if (!terminal && !finishedEvent) return;
    notified.current = true;
    onDone?.();
  }, [terminal, finishedEvent, onDone]);

  const onCancel = useCallback(async () => {
    if (jobId === null) return;
    setCancelling(true);
    setCancelError(null);
    try {
      await cancelJob(jobId);
      setCancelled(true);
    } catch (err) {
      setCancelError(errorMessage(err));
    } finally {
      setCancelling(false);
    }
  }, [jobId]);

  if (jobId === null) return null;

  const total = job?.total_items ?? items.length;
  const done = job?.done_items ?? items.filter((i) => i.status !== "pending" && i.status !== "running").length;
  const failed = job?.failed_items ?? items.filter((i) => i.status === "failed").length;
  const unconfirmed =
    job?.unconfirmed_items ?? items.filter((i) => i.status === "unconfirmed").length;
  const percent = clampProgress(job ? job.progress : total > 0 ? (done / total) * 100 : 0);
  const cancelRequested = cancelled || job?.cancel_requested === true;

  /* Said on its own line and in these words on purpose: an unconfirmed order
     may exist, so "retry" is the one thing the operator must not do blindly. */
  const unconfirmedNote =
    unconfirmed > 0
      ? ` ${unconfirmed} unconfirmed — ${unconfirmed === 1 ? "it" : "they"} may exist; check ${unconfirmed === 1 ? "that account" : "those accounts"} on Flipkart before running again.`
      : "";

  /* The whole run's time, from the job row's two stamps, once it has ended. */
  const took =
    terminal && job?.started_at && job?.finished_at
      ? formatDuration(job.started_at, job.finished_at)
      : null;

  const statusLine = (() => {
    if (!job) return connected ? "Connected — waiting for the run to start…" : "Connecting…";
    if (job.status === "completed") {
      const head = took ? `Finished in ${took}.` : "Finished.";
      const base =
        failed > 0
          ? `${head} ${done} of ${total} processed, ${failed} did not go through.`
          : `${head} All ${total} processed.`;
      return base + unconfirmedNote;
    }
    if (job.status === "failed") {
      return `The run failed after ${done} of ${total}${took ? ` (${took})` : ""}.${unconfirmedNote}`;
    }
    if (job.status === "queued") return `Queued — ${total} waiting to start.`;
    return `Running — ${done} of ${total} done${failed > 0 ? `, ${failed} failed` : ""}${unconfirmed > 0 ? `, ${unconfirmed} unconfirmed` : ""}.`;
  })();

  const sorted = items.slice().sort(byActivity);
  /* A setting aside that named no ID changed nothing, so it is not news. */
  const recent = events
    .filter((event) => event.type !== "items_set_aside" || setAsideCount(event) > 0)
    .slice(-6)
    .reverse();
  /* What the run said about itself (Smart ID Selection on or off, IDs set
     aside before it started), saved on the run as its notes. An order run
     from before Smart ID Selection saved no value for it, and says so. */
  const olderOrderRun = job?.type === "order" && typeof job.payload?.smart_id !== "boolean";
  const notes = [...(olderOrderRun ? [SMART_ID_OLDER_RUN] : []), ...jobNotes(job?.payload)];

  return (
    <section
      className={`rounded-card border border-line bg-surface ${className}`}
      aria-label={title}
    >
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-4">
        <div className="flex items-center gap-2.5">
          <h2 className="text-[15px] font-semibold leading-tight text-ink">{title}</h2>
          <span className="text-[12px] text-muted">#{jobId}</span>
          {connected ? (
            <span className="inline-flex items-center gap-1.5 text-[12px] text-ink-2">
              <span
                aria-hidden="true"
                className="inline-block size-[7px] rounded-full"
                style={{ backgroundColor: "var(--status-good)" }}
              />
              Live
            </span>
          ) : !terminal ? (
            <span className="inline-flex items-center gap-1.5 text-[12px] text-ink-2">
              <Spinner size={12} />
              Reconnecting
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {job ? <StatusBadge status={job.status} /> : null}
          <Button
            variant="danger"
            size="sm"
            loading={cancelling}
            disabled={Boolean(terminal) || cancelRequested}
            onClick={() => void onCancel()}
          >
            {cancelRequested ? "Cancelling…" : "Cancel run"}
          </Button>
        </div>
      </header>

      <div className="flex flex-col gap-4 px-5 py-4">
        <div className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-3">
            <p className="text-[13px] text-ink" aria-live="polite">
              {statusLine}
            </p>
            <p className="shrink-0 text-[13px] font-medium tabular-nums text-ink-2">
              {done}/{total}
            </p>
          </div>
          <div
            role="progressbar"
            aria-valuenow={Math.round(percent)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuetext={`${Math.round(percent)} percent — ${done} of ${total} done`}
            className="h-2 w-full overflow-hidden rounded-full"
            style={{ backgroundColor: "var(--grid)" }}
          >
            <div
              className="h-full rounded-full transition-[width] duration-300"
              style={{
                width: `${percent}%`,
                backgroundColor: terminal && failed > 0 ? "var(--status-warning)" : "var(--accent)",
              }}
            />
          </div>
        </div>

        {notes.length > 0 ? (
          <ul aria-label="Run notes" className="flex flex-col gap-0.5 text-[12px] leading-snug text-ink-2">
            {notes.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        ) : null}

        {error ? <Alert tone="error">{error}</Alert> : null}
        {cancelError ? <Alert tone="error">{cancelError}</Alert> : null}
        {cancelRequested && !terminal ? (
          <Alert tone="warning">
            Cancellation requested. Accounts already in flight will finish first.
          </Alert>
        ) : null}

        {(() => {
          const tracker = (job?.payload as { ai_tracker?: AiTrackerState } | null | undefined)?.ai_tracker;
          return tracker ? <AiTrackerCard tracker={tracker} terminal={Boolean(terminal)} /> : null;
        })()}

        {recent.length > 0 ? (
          <div>
            <h3 className="mb-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted">
              Latest
            </h3>
            <ul className="flex flex-col gap-1 text-[12px] text-ink-2">
              {recent.map((event) => (
                <li key={event.key} className="flex gap-2">
                  <span className="shrink-0 tabular-nums text-muted">
                    {formatTime(event.receivedAt)}
                  </span>
                  <span className="min-w-0 truncate">{eventLine(event)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <div>
          <h3 className="mb-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted">
            Per account
          </h3>
          {sorted.length === 0 ? (
            <p className="rounded-control border border-dashed border-line px-3 py-4 text-center text-[13px] text-ink-2">
              Waiting for the first account to start.
            </p>
          ) : (
            <ul className="divide-y divide-line rounded-control border border-line">
              {sorted.map((item) => {
                const spent = formatSpent(item);
                return (
                  <li key={item.account_id} className="flex flex-wrap items-center gap-2 px-3 py-2">
                    <span className="min-w-0 flex-1 truncate text-[13px] text-ink">
                      {item.email || `Account ${item.account_id}`}
                    </span>
                    {item.message ? (
                      <span className="min-w-0 max-w-full basis-full truncate text-[12px] text-ink-2 sm:basis-auto sm:max-w-[45%]">
                        {item.message}
                      </span>
                    ) : null}
                    {spent ? (
                      <span
                        className="shrink-0 text-[12px] tabular-nums text-muted"
                        title="Time the run spent on this account"
                      >
                        {spent}
                      </span>
                    ) : null}
                    <StatusBadge status={itemStatusKey(item)} />
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}

export default ProgressStream;
