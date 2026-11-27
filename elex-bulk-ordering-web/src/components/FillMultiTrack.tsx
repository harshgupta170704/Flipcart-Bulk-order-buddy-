/**
 * E-Kart's multi_track sheet, filled in: upload the file exported from the
 * courier's portal and the same file downloads back, every column as it was,
 * with Account, Delivery OTP, Last 6 Digits Of Phone and Flipkart Status
 * added at the end. Nothing in the ledger changes.
 */
import { useCallback, useRef, useState } from "react";
import { errorMessage, fillMultiTrack, type MultiTrackResult } from "../lib/api";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";

export function FillMultiTrack() {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MultiTrackResult | null>(null);

  const fill = useCallback(async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await fillMultiTrack(file));
      setFile(null);
      if (input.current) input.current.value = "";
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }, [file]);

  return (
    <Card
      title="Fill a multi-track sheet"
      description="Upload the multi_track file from E-Kart. It downloads back in the same format, with Account, Delivery OTP, Last 6 Digits Of Phone and Flipkart Status added as the last four columns. Rows are matched by tracking ID."
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={input}
            type="file"
            accept=".xlsx,.csv"
            aria-label="Multi-track sheet"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            className="text-[13px]"
          />
          <Button size="sm" variant="primary" onClick={() => void fill()} disabled={!file || busy} loading={busy}>
            Fill and download
          </Button>
        </div>
        {error ? <Alert tone="error">{error}</Alert> : null}
        {result ? (
          <p className="text-[13px]">
            Downloaded. <span className="font-medium">{result.matched}</span> of{" "}
            <span className="font-medium">{result.rows}</span> shipment{result.rows === 1 ? "" : "s"} filled
            {result.rows > result.matched
              ? `; ${result.rows - result.matched} not found in the ledger were left blank.`
              : "."}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

export default FillMultiTrack;
