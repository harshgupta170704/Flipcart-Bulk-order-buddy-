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
import { useCallback, useEffect, useRef, useState } from "react";
import { downloadInvoiceData, errorMessage, getInvoiceTypes } from "../lib/api";
import type { InvoiceTypes, OrderFilters } from "../lib/types";
import { countLabel, formatCount } from "../lib/format";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Checkbox } from "./Field";
import { SkeletonRows } from "./Skeleton";

/** The type ticked when the dialog opens, when the selection has it (S5). */
const DEFAULT_TYPE = "Product";

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

export function ExtractInvoicesDataControls({ filters, total, push }: ExtractInvoicesDataControlsProps) {
  const [open, setOpen] = useState(false);
  const [types, setTypes] = useState<InvoiceTypes | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(() => new Set());
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

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
                None of the {countLabel(types.units, "matching unit")} has a readable invoice on file. Read the orders
                back from Flipkart or upload the PDFs first.
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
