/**
 * Bring units in from an order report: one row per unit, in the template's
 * columns (spec C-1, FR-021, FR-022, FR-024, FR-060). A unit the ledger holds
 * is updated — only its empty fields once the read-back has seen it on
 * Flipkart — a new one joins its order, and each refused row says why.
 */
import { useCallback, useId, useRef, useState } from "react";
import { downloadReportTemplate, errorMessage, importOrderReport } from "../lib/api";
import type { OrderReportImportResult } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";

/** Refused rows listed at first; "Show all" lists every one the server returned. */
const SHOWN_REJECTIONS = 20;

export function UploadOrderReport({ onChanged }: { onChanged: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<OrderReportImportResult | null>(null);
  const [allRejected, setAllRejected] = useState(false);
  const refusedId = useId();

  const upload = useCallback(async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setAllRejected(false);
    try {
      const out = await importOrderReport(file);
      setResult(out);
      setFile(null);
      if (input.current) input.current.value = "";
      if (out.created > 0 || out.updated > 0) onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [file, onChanged]);

  const template = useCallback(async () => {
    setError(null);
    try {
      await downloadReportTemplate();
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  return (
    <Card
      title="Bring units in from an order report"
      description="A CSV or XLSX with one row per unit, in the template's columns. A unit already in the ledger is updated — once Flipkart's own page has been read, only what it left empty is filled — and a new one joins its order under the account its row names. Card columns are never read."
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={input}
            type="file"
            accept=".csv,.xlsx"
            aria-label="Order report"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            className="text-[13px]"
          />
          <Button size="sm" variant="primary" onClick={() => void upload()} disabled={!file || busy} loading={busy}>
            Upload report
          </Button>
          <Button size="sm" variant="ghost" onClick={() => void template()}>
            Download the template
          </Button>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {result ? (
          <div className="flex flex-col gap-2 text-[13px]">
            <p>
              <span className="font-medium">{result.created}</span> unit{result.created === 1 ? "" : "s"} added,{" "}
              <span className="font-medium">{result.updated}</span> updated,{" "}
              <span className="font-medium">{result.unchanged}</span> already as the report says
              {result.rejected.length > 0 ? (
                <>
                  , <span className="font-medium text-critical">{result.rejected.length}</span> row
                  {result.rejected.length === 1 ? "" : "s"} refused
                </>
              ) : null}
              .
            </p>
            {result.kept_platform > 0 ? (
              <p className="text-[12px] text-ink-2">
                Flipkart's own page says otherwise for {result.kept_platform} unit
                {result.kept_platform === 1 ? "" : "s"} (status, dates, tracking, seller or address): its reading was
                kept, and only what it left empty was filled.
              </p>
            ) : null}
            {result.not_kept.length > 0 ? (
              <p className="text-[12px] text-ink-2">
                Not kept — the ledger has no place for these columns yet: {result.not_kept.join(", ")}.
              </p>
            ) : null}
            {result.rejected.length > 0 ? (
              <ul
                id={refusedId}
                aria-label="Refused rows"
                tabIndex={allRejected ? 0 : undefined}
                className={`list-disc pl-5 text-[12px] text-ink-2 ${allRejected ? "max-h-[320px] overflow-y-auto" : ""}`}
              >
                {(allRejected ? result.rejected : result.rejected.slice(0, SHOWN_REJECTIONS)).map((item) => (
                  <li key={item.row}>
                    Row {item.row}: {item.reason}
                  </li>
                ))}
                {!allRejected && result.rejected.length > SHOWN_REJECTIONS ? (
                  <li>… and {result.rejected.length - SHOWN_REJECTIONS} more</li>
                ) : null}
              </ul>
            ) : null}
            {result.rejected.length > SHOWN_REJECTIONS ? (
              <div>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-expanded={allRejected}
                  aria-controls={refusedId}
                  onClick={() => setAllRejected((open) => !open)}
                >
                  {allRejected ? `Show the first ${SHOWN_REJECTIONS}` : `Show all ${result.rejected.length}`}
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export default UploadOrderReport;
