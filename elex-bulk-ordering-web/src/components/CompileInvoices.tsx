/**
 * Compile Invoices (spec FR-031–FR-039): zip the invoice PDFs of the order
 * units on screen, watch the compilation, and download or delete the archives.
 *
 * The compilation runs in the background on the server; this polls its status
 * while one is running, says how it ended, and survives a page reload because
 * the status comes from the server, not from this component's memory. A
 * download is a plain navigation to a short-lived link — probed first, so a
 * refused link becomes an error here and never a bare JSON page in the app's
 * place — and a gigabyte streams to disk rather than through a blob in memory.
 *
 * The archive list comes a page at a time, newest first, and "Show older
 * archives" adds the next page under the rows shown, so every archive kept can
 * be reached (FR-038: the new archive "or any earlier archive").
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  cancelJob,
  deleteArchive,
  downloadArchive,
  errorMessage,
  getCompileStatus,
  listArchives,
  startCompile,
} from "../lib/api";
import type { CompiledArchive, CompileStatus, Job, OrderFilters } from "../lib/types";
import { clampProgress, countLabel, formatBytes, formatCount, formatDateTime, formatRelative } from "../lib/format";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Field, TextInput } from "./Field";
import { SkeletonRows } from "./Skeleton";
import { Table, type Column } from "./Table";

/** How often the status is asked for while a compilation runs. */
const DEFAULT_POLL_MS = 3000;

type Outcome = "completed" | "cancelled" | "failed";

/** What a failed run with no recorded reason reads as. */
const STOPPED_EARLY = "The server stopped before the compilation finished — no archive was added.";

/**
 * How a run ended. The job body records an outcome for every way it can end;
 * a run that died outside it (a crash, a restart) carries only a failed
 * status, and a failed status is a failure whatever the payload says.
 */
function outcomeOf(job: Job | null): Outcome | null {
  const value = job?.payload?.outcome;
  if (value === "completed" || value === "cancelled" || value === "failed") return value;
  return job?.status === "failed" ? "failed" : null;
}

function payloadText(job: Job | null, key: string): string | null {
  const value = job?.payload?.[key];
  return typeof value === "string" && value !== "" ? value : null;
}

export interface CompileToast {
  tone: "success" | "error" | "info";
  title: string;
  description?: string;
}

export interface CompileInvoicesControlsProps {
  /** The ledger filter on screen — what the archive covers. */
  filters: OrderFilters;
  /** How many order units that filter matches; zero disables the button. */
  total: number;
  push: (toast: CompileToast) => void;
  /** How often to poll while a run is active; tests pass something small. */
  pollMs?: number;
}

/** One line on how a finished compilation ended, for the status panel. */
function finishedLine(job: Job): string {
  const name = payloadText(job, "archive_name") ?? "the archive";
  switch (outcomeOf(job)) {
    case "completed":
      return `${name} compiled ${formatRelative(job.finished_at ?? job.started_at)}.`;
    case "cancelled":
      return `The last compilation (${name}) was cancelled — no archive was added.`;
    case "failed":
      return `The last compilation (${name}) failed.`;
    default:
      return `The last compilation (${name}) ended ${formatRelative(job.finished_at ?? job.started_at)}.`;
  }
}

export function CompileInvoicesControls({ filters, total, push, pollMs = DEFAULT_POLL_MS }: CompileInvoicesControlsProps) {
  const [status, setStatus] = useState<CompileStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [compileOpen, setCompileOpen] = useState(false);
  const [prefix, setPrefix] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  /** The server accepted a cancel; the run stops after the current order. */
  const [cancelSent, setCancelSent] = useState(false);
  const [archivesOpen, setArchivesOpen] = useState(false);
  const [archives, setArchives] = useState<CompiledArchive[] | null>(null);
  /** Every archive, on every page, as the server last counted them. */
  const [archivesTotal, setArchivesTotal] = useState(0);
  /** Where "Show older archives" picks up; null once the oldest is shown. */
  const [olderBefore, setOlderBefore] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);
  const [archivesError, setArchivesError] = useState<string | null>(null);
  const [busyArchive, setBusyArchive] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);

  const alive = useRef(true);
  /** The run being watched, so its end can be announced once. */
  const watching = useRef<number | null>(null);
  /** Only the newest status request may land: a slow early reply must not
   *  overwrite the run a Start just reported. */
  const statusTicket = useRef(0);
  /** Only the newest request for the newest page may land, as with the status. */
  const listTicket = useRef(0);
  /** Moves on each time the list shown starts over from its newest page. An
   *  older page asked for before then extends the list as it was — the one
   *  still shown while the dialog, opened again, waits for its newest page —
   *  and added under the new one it could step over archives, so it is
   *  dropped. Counted when the list is replaced, not when it is asked for: a
   *  reload that fails leaves the old list, and its older pages, in place. */
  const listVersion = useRef(0);

  const loadStatus = useCallback(async () => {
    const mine = ++statusTicket.current;
    try {
      const next = await getCompileStatus();
      if (!alive.current || mine !== statusTicket.current) return;
      setStatus(next);
      setStatusError(null);
    } catch (err) {
      if (alive.current && mine === statusTicket.current) setStatusError(errorMessage(err));
    }
  }, []);

  /** The newest page of archives, in place of whatever the list held. */
  const loadArchives = useCallback(async () => {
    const mine = ++listTicket.current;
    try {
      const page = await listArchives();
      if (!alive.current || mine !== listTicket.current) return;
      listVersion.current += 1;
      setArchives(page.items);
      setArchivesTotal(page.total);
      setOlderBefore(page.next_before);
      setOlderError(null);
      setArchivesError(null);
    } catch (err) {
      if (alive.current && mine === listTicket.current) setArchivesError(errorMessage(err));
    }
  }, []);

  /** The next page of older archives, added under the rows shown. It starts
   *  after the last archive of the page before, by key: rows deleted from the
   *  list meanwhile make it skip nothing. */
  const showOlder = async () => {
    if (olderBefore === null || loadingOlder) return;
    const mine = listVersion.current;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const page = await listArchives(olderBefore);
      if (!alive.current || mine !== listVersion.current) return;
      setArchives((rows) => {
        const shown = new Set((rows ?? []).map((row) => row.id));
        return [...(rows ?? []), ...page.items.filter((row) => !shown.has(row.id))];
      });
      setArchivesTotal(page.total);
      setOlderBefore(page.next_before);
    } catch (err) {
      if (alive.current && mine === listVersion.current) setOlderError(errorMessage(err));
    } finally {
      if (alive.current) setLoadingOlder(false);
    }
  };

  useEffect(() => {
    alive.current = true;
    void loadStatus();
    return () => {
      alive.current = false;
    };
  }, [loadStatus]);

  // A run started elsewhere — another tab, another operator — shows up when
  // this tab is looked at again.
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void loadStatus();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [loadStatus]);

  const active = status?.active ?? null;
  const activeId = active?.id ?? null;
  const running = active !== null;
  const cancelRequested = running && (active.cancel_requested || cancelSent);

  // Poll only while a compilation runs. Nothing running, nothing polled.
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => void loadStatus(), pollMs);
    return () => window.clearInterval(id);
  }, [running, pollMs, loadStatus]);

  // A cancel belongs to one run.
  useEffect(() => {
    setCancelSent(false);
  }, [activeId]);

  // Announce how the run that was being watched ended.
  useEffect(() => {
    if (active !== null) {
      watching.current = active.id;
      return;
    }
    const last = status?.last ?? null;
    if (watching.current === null || last === null || last.id !== watching.current) return;
    watching.current = null;
    const name = payloadText(last, "archive_name") ?? "The archive";
    const outcome = outcomeOf(last);
    if (outcome === "completed") {
      push({ tone: "success", title: "Invoices compiled", description: `${name} is ready under Download Compiled Invoices.` });
    } else if (outcome === "cancelled") {
      push({ tone: "info", title: "Compilation cancelled", description: "No archive was added." });
    } else {
      push({ tone: "error", title: "Compilation failed", description: payloadText(last, "error") ?? STOPPED_EARLY });
    }
    if (archivesOpen) void loadArchives();
  }, [active, status, push, archivesOpen, loadArchives]);

  // The archive list loads when its dialog opens.
  useEffect(() => {
    if (!archivesOpen) return;
    setConfirmDelete(null);
    setArchiveError(null);
    setOlderError(null);
    void loadArchives();
    void loadStatus();
  }, [archivesOpen, loadArchives, loadStatus]);

  const start = async () => {
    setStarting(true);
    setStartError(null);
    try {
      const out = await startCompile(filters, prefix);
      if (!alive.current) return;
      setCompileOpen(false);
      setPrefix("");
      watching.current = out.job.id;
      statusTicket.current += 1; // whatever was in flight is older than this
      setStatus((current) => ({
        active: out.job,
        last: current?.last ?? null,
        archive_retention_days: current?.archive_retention_days ?? 0,
      }));
      const leftOut =
        out.units_without_invoice > 0
          ? ` ${formatCount(out.units_without_invoice)} of ${countLabel(out.units, "unit")} have no invoice on file and are left out.`
          : "";
      push({
        tone: "info",
        title: "Invoice compilation started",
        description: `Compiling invoices for ${countLabel(out.orders, "order")}. This may take a few minutes.${leftOut}`,
      });
      void loadStatus();
    } catch (err) {
      if (!alive.current) return;
      setStartError(errorMessage(err));
      // Refused because one is already running — started elsewhere, most
      // likely: show that run rather than a button that contradicts the server.
      if (err instanceof ApiError && err.status === 409) void loadStatus();
    } finally {
      if (alive.current) setStarting(false);
    }
  };

  const cancel = async () => {
    if (active === null || cancelRequested) return;
    setCancelling(true);
    try {
      await cancelJob(active.id);
      if (!alive.current) return;
      setCancelSent(true);
      void loadStatus();
    } catch (err) {
      if (alive.current) push({ tone: "error", title: "Could not cancel", description: errorMessage(err) });
    } finally {
      if (alive.current) setCancelling(false);
    }
  };

  const download = async (row: CompiledArchive) => {
    setBusyArchive(row.id);
    setArchiveError(null);
    try {
      await downloadArchive(row.id);
    } catch (err) {
      if (alive.current) setArchiveError(errorMessage(err));
    } finally {
      if (alive.current) setBusyArchive(null);
    }
  };

  const remove = async (row: CompiledArchive) => {
    if (confirmDelete !== row.id) {
      setConfirmDelete(row.id);
      return;
    }
    setBusyArchive(row.id);
    setArchiveError(null);
    try {
      await deleteArchive(row.id);
      if (!alive.current) return;
      setConfirmDelete(null);
      setArchives((rows) => (rows ?? []).filter((item) => item.id !== row.id));
      setArchivesTotal((count) => Math.max(0, count - 1));
    } catch (err) {
      if (alive.current) setArchiveError(errorMessage(err));
    } finally {
      if (alive.current) setBusyArchive(null);
    }
  };

  const columns: Column<CompiledArchive>[] = [
    { key: "name", header: "Archive", render: (row) => <span className="font-medium">{row.name}</span> },
    { key: "size", header: "Size", numeric: true, width: "100px", render: (row) => formatBytes(row.size) },
    {
      key: "contents",
      header: "Contents",
      width: "230px",
      render: (row) => (
        <span className="block text-[12px] text-ink-2">
          {countLabel(row.invoices, "PDF")} · {countLabel(row.orders, "order")}
          {row.left_out > 0 ? ` · ${countLabel(row.left_out, "unit")} left out` : ""}
          {row.missing_files > 0 ? ` · ${countLabel(row.missing_files, "file")} missing` : ""}
        </span>
      ),
    },
    {
      key: "created_at",
      header: "Compiled",
      width: "170px",
      render: (row) => (
        <span className="block">
          <span className="block whitespace-nowrap text-ink-2">{formatDateTime(row.created_at)}</span>
          <span className="block text-[12px] text-muted">{formatRelative(row.created_at)}</span>
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      width: "230px",
      render: (row) => (
        <span className="inline-flex items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            loading={busyArchive === row.id}
            disabled={busyArchive !== null}
            onClick={() => void download(row)}
          >
            Download
          </Button>
          <Button
            size="sm"
            variant={confirmDelete === row.id ? "danger" : "ghost"}
            disabled={busyArchive !== null}
            onClick={() => void remove(row)}
          >
            {confirmDelete === row.id ? "Confirm delete" : "Delete"}
          </Button>
        </span>
      ),
    },
  ];

  const last = status?.last ?? null;
  const lastOutcome = outcomeOf(last);
  const retention = status?.archive_retention_days ?? null;
  const keptFor =
    retention === null
      ? ""
      : retention > 0
        ? ` Archives are kept for ${countLabel(retention, "day")} unless deleted sooner.`
        : " Archives are kept until deleted.";

  return (
    <>
      <Button size="sm" variant="secondary" onClick={() => setArchivesOpen(true)}>
        Download Compiled Invoices
      </Button>
      <Button
        size="sm"
        variant="secondary"
        disabled={total === 0 || running || starting}
        onClick={() => {
          setStartError(null);
          setCompileOpen(true);
        }}
        title={total === 0 ? "No order unit matches the current filter" : undefined}
      >
        {running ? "Compiling Invoices..." : "Compile Invoices"}
      </Button>
      {running ? (
        <Button size="sm" variant="danger" loading={cancelling} disabled={cancelRequested} onClick={() => void cancel()}>
          {cancelRequested ? "Cancelling…" : "Cancel"}
        </Button>
      ) : null}

      <Dialog
        open={compileOpen}
        onClose={() => {
          if (!starting) setCompileOpen(false);
        }}
        title="Compile Invoices"
        description={`One zip of the invoice PDFs of the ${countLabel(total, "order unit")} matching the current filter.`}
        width="560px"
        footer={
          <>
            <Button size="sm" variant="ghost" disabled={starting} onClick={() => setCompileOpen(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="primary"
              loading={starting}
              disabled={prefix.trim() === "" || starting}
              onClick={() => void start()}
            >
              Start Compilation
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {startError ? (
            <Alert tone="error" title="Could not start">
              {startError}
            </Alert>
          ) : null}
          <Field
            label="Invoice Name Prefix"
            required
            help="The zip is named after it. Letters, digits, dots, dashes and underscores; anything else becomes an underscore."
          >
            <TextInput
              value={prefix}
              autoComplete="off"
              spellCheck={false}
              maxLength={120}
              placeholder="invoices_oct_2026"
              onChange={(event) => setPrefix(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && prefix.trim() !== "" && !starting) {
                  event.preventDefault();
                  void start();
                }
              }}
            />
          </Field>
          <p className="text-[12px] text-ink-2">
            Units whose order has no invoice on file are left out, and the count is reported.
            The compilation runs in the background — you can keep working while it does.
          </p>
        </div>
      </Dialog>

      <Dialog
        open={archivesOpen}
        onClose={() => setArchivesOpen(false)}
        title="Compiled Invoices"
        description={`Each archive is one zip of invoice PDFs.${keptFor}`}
        width="860px"
        footer={
          <Button size="sm" onClick={() => setArchivesOpen(false)}>
            Close
          </Button>
        }
      >
        <div className="flex flex-col gap-4">
          <section aria-label="Compilation status" className="rounded-control border border-line bg-page px-3.5 py-3">
            <h3 className="text-[12px] font-semibold uppercase tracking-wide text-muted">Compilation status</h3>
            {statusError !== null && status === null ? (
              <p role="status" className="mt-1 text-[13px] text-ink-2">
                Could not load the status: {statusError}
              </p>
            ) : active !== null ? (
              <div className="mt-1 flex flex-wrap items-center gap-3">
                <p className="text-[13px] text-ink">
                  {cancelRequested ? "Stopping after the current order… " : "Compiling "}
                  {payloadText(active, "archive_name") ?? "the archive"} — {formatCount(active.done_items)} of{" "}
                  {countLabel(active.total_items, "order")} ({clampProgress(active.progress)}%)
                </p>
                <Button size="sm" variant="danger" loading={cancelling} disabled={cancelRequested} onClick={() => void cancel()}>
                  {cancelRequested ? "Cancelling…" : "Cancel"}
                </Button>
              </div>
            ) : last !== null && lastOutcome === "failed" ? (
              <Alert tone="error" title={finishedLine(last)} className="mt-2">
                {payloadText(last, "error") ?? STOPPED_EARLY}
              </Alert>
            ) : (
              <p className="mt-1 text-[13px] text-ink-2">
                No compilation in progress.{last !== null ? ` ${finishedLine(last)}` : ""}
              </p>
            )}
          </section>

          {archiveError ? (
            <Alert tone="error" title="That did not work">
              {archiveError}
            </Alert>
          ) : null}

          {archivesError !== null ? (
            <Alert tone="error" title={archives === null ? "Could not load the archives" : "Could not refresh the archives"}>
              {archivesError}
              <Button size="sm" className="mt-2" onClick={() => void loadArchives()}>
                Try again
              </Button>
            </Alert>
          ) : null}

          {archives === null ? (
            archivesError === null ? <SkeletonRows rows={3} /> : null
          ) : archives.length === 0 && olderBefore === null ? (
            <p className="text-[13px] text-ink-2">No archive yet. Compile invoices from the ledger and it appears here.</p>
          ) : (
            <>
              {archives.length > 0 ? (
                <Table columns={columns} rows={archives} rowKey={(row) => row.id} caption="Compiled archives, newest first" minWidth="740px" />
              ) : null}
              {olderBefore !== null ? (
                <div className="flex flex-wrap items-center gap-3">
                  <Button size="sm" variant="secondary" loading={loadingOlder} disabled={loadingOlder} onClick={() => void showOlder()}>
                    Show older archives
                  </Button>
                  {/* Never fewer in all than on screen: rows deleted elsewhere meanwhile are still shown here. */}
                  <span className="text-[12px] text-muted">
                    Showing {formatCount(archives.length)} of {formatCount(Math.max(archivesTotal, archives.length))}
                  </span>
                </div>
              ) : null}
              {olderError !== null ? (
                <p role="status" className="text-[12px] text-ink-2">
                  Could not load the older archives: {olderError}
                </p>
              ) : null}
            </>
          )}
        </div>
      </Dialog>
    </>
  );
}

export default CompileInvoicesControls;
