/**
 * Invoice PDFs by hand, several at once, loose or zipped. The read-back
 * fetches an invoice by itself when an order's page offers the link; this is
 * for the ones it did not. Each file is matched to its order by the order id
 * (OD…) in its name, which is how Flipkart names them, or failing that by the
 * order id printed on the PDF itself; a file whose name and print disagree is
 * filed under neither. A zip — Flipkart hands out an order's invoices as
 * OD….zip holding file_1.pdf, file_2.pdf… — is unpacked on the server and
 * answered PDF by PDF. The answer says what happened to every file, and a
 * PDF put on file that the reader could not read is said to be, with why:
 * it is in no invoice-data CSV until it can be read (spec NFR-009).
 */
import { useCallback, useRef, useState } from "react";
import { errorMessage, uploadInvoices } from "../lib/api";
import type { InvoiceUploadResult } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";

type UploadedFile = InvoiceUploadResult["files"][number];

const STATUS_LABEL: Record<UploadedFile["status"], string> = {
  stored: "On file",
  duplicate: "Already on file",
  no_match: "No matching order",
  mismatch: "Order id mismatch",
  not_pdf: "Not a PDF",
  too_large: "Too large",
  not_saved: "Not saved",
};

/** On file, but the reader could not make an invoice of it. */
function unreadable(file: UploadedFile): boolean {
  return file.status === "stored" && file.reading === "not_read";
}

export function UploadInvoices({ onChanged }: { onChanged: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<InvoiceUploadResult | null>(null);
  const unreadCount = result ? result.files.filter(unreadable).length : 0;

  const upload = useCallback(async () => {
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const out = await uploadInvoices(files);
      setResult(out);
      setFiles([]);
      if (input.current) input.current.value = "";
      if (out.stored > 0) onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [files, onChanged]);

  return (
    <Card
      title="Put invoice PDFs on file"
      description="Several at once, PDFs or the zips Flipkart sends (one OD….zip per order, unpacked here). Each file is matched to its order by the order id (OD…) in the file or zip name — the way Flipkart names them — or, when the name has none, by the order id printed on the PDF. The read-back fetches invoices by itself when an order's page offers the link; this is for the ones it did not."
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={input}
            type="file"
            accept="application/pdf,.pdf,application/zip,application/x-zip-compressed,.zip"
            multiple
            aria-label="Invoice PDFs or zips"
            onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
            className="text-[13px]"
          />
          <Button
            size="sm"
            variant="primary"
            onClick={() => void upload()}
            disabled={files.length === 0 || busy}
            loading={busy}
          >
            Upload {files.length > 0 ? `${files.length} file${files.length === 1 ? "" : "s"}` : ""}
          </Button>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {result ? (
          <div className="flex flex-col gap-2 text-[13px]">
            <p>
              <span className="font-medium">{result.stored}</span> invoice{result.stored === 1 ? "" : "s"} put on file.
              {unreadCount > 0 ? ` ${unreadCount} could not be read.` : ""}
            </p>
            <ul className="list-disc pl-5 text-[12px] text-ink-2">
              {result.files.map((file, index) => (
                <li key={`${file.filename}-${index}`}>
                  <span className="text-ink">{file.filename}</span> —{" "}
                  {unreadable(file) ? (
                    <span className="text-critical">Stored, but could not be read</span>
                  ) : (
                    (STATUS_LABEL[file.status] ?? file.status)
                  )}
                  {file.order_id ? ` (${file.order_id})` : ""}
                  {file.reason && (file.status !== "stored" || unreadable(file)) ? `: ${file.reason}` : ""}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export default UploadInvoices;
