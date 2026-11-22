/**
 * The Cancel Order Units / Reschedule Order Units dialog (spec FR-020,
 * decision C-4).
 *
 * Nothing here reaches Flipkart. What it records is a request — which units,
 * why, and for a reschedule the wanted new date — for a person to action on
 * the platform by hand and then mark done under Requests. The server passes
 * over a unit that cannot take the request (already delivered, already asked)
 * and says why; the outcome stays in the dialog so those reasons can be read.
 */
import { useEffect, useState } from "react";
import { createUnitRequests, errorMessage } from "../lib/api";
import type { UnitRequestCreateInput, UnitRequestCreateResult, UnitRequestKind } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Field, Textarea, TextInput } from "./Field";

/** Enough of a ledger row to name it in the dialog; an `Order` fits. */
export interface RequestUnit {
  id: number;
  order_id: string | null;
  unit_position?: number;
  units_in_order?: number;
}

export interface UnitRequestDraft {
  kind: UnitRequestKind;
  units: RequestUnit[];
}

export interface UnitRequestDialogProps {
  /** null keeps the dialog closed. */
  draft: UnitRequestDraft | null;
  onClose: () => void;
  /** After the server recorded the requests: the page clears its selection and reloads. */
  onRecorded?: (result: UnitRequestCreateResult) => void;
}

const COPY: Record<UnitRequestKind, { title: string; submit: string; verb: string }> = {
  cancel: { title: "Cancel Order Units", submit: "Record cancel request", verb: "cancel" },
  reschedule: { title: "Reschedule Order Units", submit: "Record reschedule request", verb: "reschedule" },
};

function unitLabel(unit: RequestUnit): string {
  const name = unit.order_id ?? `row #${unit.id}`;
  return unit.units_in_order && unit.units_in_order > 1 ? `${name} (unit ${unit.unit_position} of ${unit.units_in_order})` : name;
}

/** Today as YYYY-MM-DD in the browser's calendar — the earliest date a reschedule may ask for. */
function today(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

/** "3 requests recorded; 1 skipped (already delivered)". */
export function outcomeLine(result: UnitRequestCreateResult): string {
  const n = result.created.length;
  const m = result.skipped.length;
  const recorded = `${n} request${n === 1 ? "" : "s"} recorded`;
  if (m === 0) return `${recorded}.`;
  const counts = new Map<string, number>();
  for (const skip of result.skipped) counts.set(skip.why, (counts.get(skip.why) ?? 0) + 1);
  const reasons = [...counts].map(([why, count]) => (count > 1 ? `${why} ×${count}` : why)).join("; ");
  return `${recorded}; ${m} skipped (${reasons}).`;
}

export function UnitRequestDialog({ draft, onClose, onRecorded }: UnitRequestDialogProps) {
  const [reason, setReason] = useState("");
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UnitRequestCreateResult | null>(null);

  // A fresh draft is a fresh form: nothing typed for one selection carries over.
  useEffect(() => {
    setReason("");
    setDate("");
    setBusy(false);
    setError(null);
    setResult(null);
  }, [draft]);

  if (!draft) return null;
  const copy = COPY[draft.kind];
  const count = draft.units.length;
  const units = `${count} unit${count === 1 ? "" : "s"}`;
  const ready = draft.kind === "cancel" ? reason.trim() !== "" : date !== "";
  const byId = new Map(draft.units.map((unit) => [unit.id, unit]));

  const close = () => {
    if (!busy) onClose();
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const input: UnitRequestCreateInput = { unit_ids: draft.units.map((unit) => unit.id), kind: draft.kind };
      if (reason.trim() !== "") input.reason = reason.trim();
      if (draft.kind === "reschedule") input.requested_for = date;
      const recorded = await createUnitRequests(input);
      setResult(recorded);
      onRecorded?.(recorded);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={close}
      title={copy.title}
      description={
        result
          ? "Recorded. Someone now does this on Flipkart by hand and marks it done under Requests."
          : `${units} selected. Nothing is sent to Flipkart: this records a request for someone to ${copy.verb} the ${
              count === 1 ? "unit" : "units"
            } there by hand, then mark it done under Requests.`
      }
      width="560px"
      footer={
        result ? (
          <Button size="sm" variant="primary" onClick={onClose}>
            Close
          </Button>
        ) : (
          <>
            <Button size="sm" variant="ghost" disabled={busy} onClick={close}>
              Close
            </Button>
            <Button
              size="sm"
              variant={draft.kind === "cancel" ? "danger" : "primary"}
              loading={busy}
              disabled={busy || !ready}
              onClick={() => void submit()}
            >
              {copy.submit}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <div className="flex flex-col gap-3">
          <Alert tone={result.created.length > 0 ? "success" : "warning"}>{outcomeLine(result)}</Alert>
          {result.skipped.length > 0 ? (
            <ul className="flex flex-col gap-1 text-[13px] text-ink-2">
              {result.skipped.map((skip) => {
                const unit = byId.get(skip.unit_id);
                return (
                  <li key={skip.unit_id}>
                    <span className="font-medium text-ink">{unit ? unitLabel(unit) : `row #${skip.unit_id}`}</span> — {skip.why}
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {error ? (
            <Alert tone="error" title="Could not record the request">
              {error}
            </Alert>
          ) : null}
          {draft.kind === "reschedule" ? (
            <Field id="request-date" label="New delivery date" required help="The day the units should arrive instead.">
              <TextInput type="date" min={today()} value={date} onChange={(event) => setDate(event.target.value)} />
            </Field>
          ) : null}
          <Field
            id="request-reason"
            label={draft.kind === "cancel" ? "Reason" : "Reason (optional)"}
            required={draft.kind === "cancel"}
            help={
              draft.kind === "cancel"
                ? "Why the units should be cancelled, for whoever does it and whoever reads the log later."
                : "Anything the person moving the delivery should know."
            }
          >
            <Textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={2000}
              placeholder={draft.kind === "cancel" ? "Ordered twice by mistake." : "The office is shut that week."}
            />
          </Field>
          {count <= 8 ? (
            <p className="text-[12px] leading-snug text-ink-2">{draft.units.map(unitLabel).join(" · ")}</p>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}

export default UnitRequestDialog;
