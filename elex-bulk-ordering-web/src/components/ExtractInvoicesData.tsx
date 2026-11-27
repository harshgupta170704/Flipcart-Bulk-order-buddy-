/**
 * Extract Invoices Data (spec FR-026–FR-030): choose the invoice types found
 * on the order units on screen and download their invoice data as a CSV.
 *
 * The type list is built from the invoices on file for the current filter
 * (C-10), so a new fee type appears without a code change. The CSV is one row
 * per invoice line of the ticked types; units whose order has no invoice on
 * file are left out, and how many is said in the dialog before and in the
 * toast after.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { downloadInvoiceData, errorMessage, getInvoiceTypes, getJob, startInvoiceFetch } from "../lib/api";
import type { InvoiceTypes, JobDetail, OrderFilters } from "../lib/types";
import { countLabel, formatCount } from "../lib/format";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Checkbox } from "./Field";
import { SkeletonRows } from "./Skeleton";

/** The type ticked when the dialog opens, when the selection has it (S5). */
const DEFAULT_TYPE = "Product";
/** How often a running fetch is looked at. */
const FETCH_POLL_MS = 3000;

function fetchDone(job: JobDetail | null): boolean {
  return job !== null && (job.status === "completed" || job.status === "failed");
}

export interface ExtractToast {
  tone: "success" | "error" | "info";
  title: string;
  description?: string;
}

export interface ExtractInvoicesDataControlsProps {
  /** The ledger filter on screen — what the CSV covers. */
  filters: OrderFilters;
  /** How many order units that filter matches; zero disables the button (FR-018). */
  total: number;
  push: (toast: ExtractToast) => void;
}

export function ExtractInvoicesDataControls({ filters: ledgerFilters, total, push }: ExtractInvoicesDataControlsProps) {
  const [open, setOpen] = useState(false);
  /** Only orders that have been received: the invoices the owner wants (27 Nov 2026). */
  const [deliveredOnly, setDeliveredOnly] = useState(true);
  /** What the fetch, the type list and the CSV all cover: the ledger filter,
   *  narrowed to delivered orders when asked. */
  const filters = useMemo<OrderFilters>(
    () => (deliveredOnly ? { ...ledgerFilters, delivery_states: ["delivered"] } : ledgerFilters),
    [ledgerFilters, deliveredOnly],
  );
  const emails = ledgerFilters.emails ?? [];
  const [types, setTypes] = useState<InvoiceTypes | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(() => new Set());
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  /** The "Fetch invoices from Flipkart" run started from this dialog, as last read. */
  const [fetchJob, setFetchJob] = useState<JobDetail | null>(null);
  const [fetchOrders, setFetchOrders] = useState(0);
  const [fetchStarting, setFetchStarting] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const fetching = fetchStarting || (fetchJob !== null && !fetchDone(fetchJob));

  const alive = useRef(true);
  /** Only the newest request may land: the filter can change between opens. */
  const ticket = useRef(0);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    setTypes(null);
    setLoadError(null);
    try {
      const next = await getInvoiceTypes(filters);
      if (!alive.current || mine !== ticket.current) return;
      setTypes(next);
      setTicked(new Set(next.types.some((t) => t.type === DEFAULT_TYPE) ? [DEFAULT_TYPE] : []));
    } catch (err) {
      if (alive.current && mine === ticket.current) setLoadError(errorMessage(err));
    }
  }, [filters]);

  // The list loads when the dialog opens, for the filter as it is then.
  useEffect(() => {
    if (!open) return;
    setDownloadError(null);
    void load();
  }, [open, load]);

  // A fetch in progress is followed until it ends; then the types are read
  // again, as they now include what was fetched.
  const fetchJobId = fetchJob?.id ?? null;
  const fetchJobDone = fetchDone(fetchJob);
  useEffect(() => {
    if (fetchJobId === null || fetchJobDone) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await getJob(fetchJobId);
        if (!alive.current) return;
        setFetchJob(next);
        if (fetchDone(next)) {
          void load();
          const failed = next.failed_items + next.skipped_items;
          push({
            tone: next.failed_items > 0 ? "error" : "success",
            title: "Invoices fetched from Flipkart",
            description:
              failed > 0
                ? `${countLabel(next.done_items, "account")} done, ${formatCount(failed)} not — see the job's details.`
                : `Every matching order was asked over ${countLabel(next.done_items, "account")}.`,
          });
        }
      } catch (err) {
        if (alive.current) setFetchError(errorMessage(err));
      }
    }, FETCH_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [fetchJob, fetchJobId, fetchJobDone, load, push]);

  const fetchFromFlipkart = async () => {
    setFetchStarting(true);
    setFetchError(null);
    try {
      const started = await startInvoiceFetch(filters);
      if (!alive.current) return;
      setFetchOrders(started.orders);
      setFetchJob({ ...started.job, items: [] });
    } catch (err) {
      if (alive.current) setFetchError(errorMessage(err));
    } finally {
      if (alive.current) setFetchStarting(false);
    }
  };

  const toggle = (type: string) => {
    setTicked((current) => {
      const next = new Set(current);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const download = async () => {
    const chosen = (types?.types ?? []).map((t) => t.type).filter((t) => ticked.has(t));
    if (chosen.length === 0) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      const out = await downloadInvoiceData(filters, chosen);
      if (!alive.current) return;
      setOpen(false);
      const leftOut =
        out.units_left_out > 0
          ? ` ${formatCount(out.units_left_out)} of the matching units have no readable invoice of the chosen types and were left out.`
          : "";
      push({
        tone: "success",
        title: "CSV downloaded",
        description: `Your export has been downloaded: ${countLabel(out.rows, "invoice line")}.${leftOut}`,
      });
    } catch (err) {
      if (alive.current) setDownloadError(errorMessage(err));
    } finally {
      if (alive.current) setDownloading(false);
    }
  };

  const list = types?.types ?? [];
  const chosen = list.filter((t) => ticked.has(t.type));
  const nothingToDownload = chosen.reduce((sum, t) => sum + t.invoices, 0) === 0;
  const loading = open && types === null && loadError === null;
  const leftOut = types?.units_without_invoice ?? 0;

  return (
    <>
      <Button
        size="sm"
        variant="secondary"
        disabled={total === 0}
        onClick={() => setOpen(true)}
        title={total === 0 ? "No order unit matches the current filter" : undefined}
      >
        Extract Invoices Data
      </Button>

      <Dialog
        open={open}
        onClose={() => {
          if (!downloading) setOpen(false);
        }}
        title="Extract Invoices Data"
        description="Select the invoice types to include in the extracted CSV. The CSV will contain invoice data extracted from downloaded invoice PDFs."
        width="560px"
        footer={
          <>
            <Button size="sm" variant="ghost" disabled={downloading} onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="primary"
              loading={downloading}
              disabled={loading || downloading || chosen.length === 0 || nothingToDownload}
              onClick={() => void download()}
            >
              Download CSV
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          {downloadError ? (
            <Alert tone="error" title="Export failed">
              {downloadError}
            </Alert>
          ) : null}

          <section aria-label="Which orders" className="flex flex-col gap-1">
            <Checkbox
              label="Only delivered (received) orders"
              help="Fetch and export the invoices of orders that have been delivered."
              checked={deliveredOnly}
              disabled={fetching || downloading}
              onChange={() => setDeliveredOnly((on) => !on)}
            />
            <p className="text-[12px] text-ink-2">
              {emails.length > 0
                ? `For ${countLabel(emails.length, "email")}: ${emails.join(", ")}`
                : "For every email — choose Platform email in the ledger filter to narrow it."}
            </p>
          </section>

          <section aria-label="Fetch from Flipkart" className="rounded-md border border-line p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-[13px] text-ink-2">
                Download every invoice of the orders matching this filter from Flipkart, store it and read it.
              </p>
              <Button
                size="sm"
                variant="secondary"
                loading={fetching}
                disabled={fetching || downloading}
                onClick={() => void fetchFromFlipkart()}
              >
                Fetch invoices from Flipkart
              </Button>
            </div>
            {fetchError ? (
              <Alert tone="error" title="Could not fetch the invoices" className="mt-2">
                {fetchError}
              </Alert>
            ) : null}
            {fetchJob !== null ? (
              <p role="status" className="mt-2 text-[12px] text-ink-2">
                {fetchDone(fetchJob)
                  ? `Fetch #${fetchJob.id} ${fetchJob.status}: ${countLabel(fetchOrders, "order")} over ${countLabel(
                      fetchJob.total_items,
                      "account",
                    )}.`
                  : `Fetching the invoices of ${countLabel(fetchOrders, "order")} — ${formatCount(
                      fetchJob.done_items + fetchJob.failed_items + fetchJob.skipped_items,
                    )} of ${countLabel(fetchJob.total_items, "account")} done…`}
              </p>
            ) : null}
          </section>

          <section aria-label="Invoice types">
            <h3 className="text-[12px] font-semibold uppercase tracking-wide text-muted">Invoice Types</h3>
            {loadError !== null ? (
              <Alert tone="error" title="Could not load the invoice types" className="mt-2">
                {loadError}
                <Button size="sm" className="mt-2" onClick={() => void load()}>
                  Try again
                </Button>
              </Alert>
            ) : types === null ? (
              <SkeletonRows rows={3} className="mt-2" />
            ) : list.length === 0 ? (
              <p className="mt-2 text-[13px] text-ink-2">
                None of the {countLabel(types.units, "matching unit")} has a readable invoice on file yet. Press
                &ldquo;Fetch invoices from Flipkart&rdquo; above, or upload the PDFs.
              </p>
            ) : (
              <div className="mt-2 flex max-h-[320px] flex-col gap-2 overflow-y-auto">
                {list.map((entry) => (
                  <Checkbox
                    key={entry.type}
                    label={entry.type}
                    help={`${countLabel(entry.invoices, "invoice")} · ${countLabel(entry.units, "unit")}`}
                    checked={ticked.has(entry.type)}
                    disabled={downloading}
                    onChange={() => toggle(entry.type)}
                  />
                ))}
              </div>
            )}
          </section>

          {types !== null ? (
            <p role="status" className="text-[13px] text-ink">
              Selected {countLabel(chosen.length, "invoice type")}
            </p>
          ) : null}

          {leftOut > 0 ? (
            <p className="text-[12px] text-ink-2">
              {formatCount(leftOut)} of the {countLabel(types?.units ?? 0, "matching unit")} have no invoice the
              tool could read and will be left out.
            </p>
          ) : null}
        </div>
      </Dialog>
    </>
  );
}

export default ExtractInvoicesDataControls;
