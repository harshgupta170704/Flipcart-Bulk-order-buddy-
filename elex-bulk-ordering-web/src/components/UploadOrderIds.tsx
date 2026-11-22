/**
 * Hand in a sheet of order ids to track. The operator's own process starts
 * from an Excel of order ids; this control takes that sheet, says what became
 * an order to track and what was refused (row by row, with the reason), and
 * offers to read those orders back from Flipkart straight away.
 */
import { useCallback, useId, useRef, useState } from "react";
import { errorMessage, importOrderIds, startSyncJob } from "../lib/api";
import type { Job, OrderIdsImportResult } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { ProgressStream } from "./ProgressStream";

/** Refused rows listed at first; "Show all" lists every one the server returned. */
const SHOWN_REJECTIONS = 20;

export function UploadOrderIds({ onChanged, blocked = false }: { onChanged: () => void; blocked?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<OrderIdsImportResult | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  /* A sheet of thousands of rows can be refused row by row, and each row is
     one to fix in the sheet, so the full list is one click away. */
  const [allRejected, setAllRejected] = useState(false);
  const refusedId = useId();

  const upload = useCallback(async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setJobId(null);
    setAllRejected(false);
    try {
      const out = await importOrderIds(file);
      setResult(out);
      setFile(null);
      if (input.current) input.current.value = "";
      if (out.created > 0) onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [file, onChanged]);

  const readBack = useCallback(async () => {
    if (!result || result.order_ids.length === 0) return;
    setStarting(true);
    setError(null);
    try {
      const job: Job = await startSyncJob({ account_ids: result.account_ids, order_ids: result.order_ids });
      setJobId(job.id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  }, [result]);

  const count = result?.order_ids.length ?? 0;

  return (
    <Card
      title="Track orders from a sheet"
      description="A CSV or XLSX with the account's email and the order id on each row; quantity and order date (DD-MM-YYYY) columns are optional. Every readable row becomes an order to read back under that account. An order with no date counts against the unit limits as if placed today until it is read back."
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={input}
            type="file"
            accept=".csv,.xlsx"
            aria-label="Order id sheet"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            className="text-[13px]"
          />
          <Button size="sm" variant="primary" onClick={() => void upload()} disabled={!file || busy} loading={busy}>
            Upload
          </Button>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {result ? (
          <div className="flex flex-col gap-2 text-[13px]">
            <p>
              <span className="font-medium">{result.created}</span> new order{result.created === 1 ? "" : "s"} to track,{" "}
              <span className="font-medium">{result.existing}</span> already in the ledger
              {result.rejected.length > 0 ? (
                <>
                  , <span className="font-medium text-critical">{result.rejected.length}</span> row
                  {result.rejected.length === 1 ? "" : "s"} refused
                </>
              ) : null}
              .
            </p>
            {result.rejected.length > 0 ? (
              <ul
                id={refusedId}
                aria-label="Refused rows"
                // Scrolls once every row is listed: focusable then, so the keyboard can scroll it.
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
            {count > 0 && jobId === null ? (
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" onClick={() => void readBack()} loading={starting} disabled={starting || blocked}>
                  Read back {count} order{count === 1 ? "" : "s"} from Flipkart
                </Button>
                <span className="text-[12px] text-ink-2">
                  {blocked
                    ? "Waiting for the ordering run to finish — a read-back would compete with it for browsers."
                    : "Opens each order's own page under its account. Places nothing."}
                </span>
              </div>
            ) : null}
          </div>
        ) : null}

        {jobId !== null ? (
          <ProgressStream jobId={jobId} onDone={onChanged} title="Read-back of the listed orders" />
        ) : null}
      </div>
    </Card>
  );
}

export default UploadOrderIds;
