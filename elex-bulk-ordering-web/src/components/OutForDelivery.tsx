/**
 * "Out for delivery" on the dashboard (the owner's ask of 26 Oct 2026): every
 * unit Flipkart says is on the courier's van, with the delivery code the
 * courier asks for at the door, as of the last read-back. The server reads
 * those accounts back on its own every half hour while anything is out for
 * delivery (ORDERING_OUT_FOR_DELIVERY_MINUTES), and every hour for any order
 * still on its way (ORDERING_TRACK_EVERY_MINUTES); "Read back now" does it at
 * once.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, getOutForDelivery, startSyncJob, waitForJob } from "../lib/api";
import type { OutForDelivery as OutForDeliveryData, OutForDeliveryUnit } from "../lib/types";
import { countLabel, formatDateTime, formatRelative } from "../lib/format";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { EmptyState } from "./EmptyState";
import { SkeletonRows } from "./Skeleton";
import { Table, type Column } from "./Table";

/** The card reloads itself this often while it is on screen. */
const RELOAD_MS = 120_000;

const COLUMNS: Column<OutForDeliveryUnit>[] = [
  {
    key: "product",
    header: "Product",
    render: (unit) => (
      <span className="block min-w-0">
        <span className="block truncate" style={{ maxWidth: "260px" }} title={unit.model ?? undefined}>
          {unit.model ?? "Unnamed product"}
        </span>
        <span className="block text-[12px] text-muted">
          {unit.order_id ?? "No order id yet"}
          {unit.color ? ` · ${unit.color}` : ""}
        </span>
      </span>
    ),
  },
  {
    key: "account",
    header: "Account",
    width: "200px",
    render: (unit) => (
      <span className="block truncate" style={{ maxWidth: "200px" }} title={unit.account_email ?? undefined}>
        {unit.account_email ?? `Account #${unit.account_id}`}
      </span>
    ),
  },
  {
    key: "code",
    header: "Delivery code",
    width: "150px",
    render: (unit) =>
      unit.delivery_otp ? (
        <span className="font-mono text-[16px] font-semibold tracking-[0.15em] text-ink tabular-nums">{unit.delivery_otp}</span>
      ) : (
        <span className="text-[12px] text-muted">Not shown by Flipkart yet</span>
      ),
  },
  {
    key: "status",
    header: "Flipkart says",
    width: "150px",
    render: (unit) => <span className="text-ink-2">{unit.platform_status ?? "—"}</span>,
  },
  {
    key: "read",
    header: "Read back",
    width: "120px",
    render: (unit) => (
      <span className="whitespace-nowrap text-ink-2" title={formatDateTime(unit.synced_at)}>
        {formatRelative(unit.synced_at)}
      </span>
    ),
  },
];

export function OutForDelivery({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<OutForDeliveryData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  /** The page's Refresh the last reply to land answered, list or error. null until the first lands. */
  const [answered, setAnswered] = useState<number | null>(null);
  /** The page's Refresh a load started now answers: the newest one handed in. */
  const asking = useRef(refreshKey);
  /* Only the newest load may land; the card can reload while a read-back runs. */
  const ticket = useRef(0);
  const live = useRef(true);
  const stop = useRef<AbortController | null>(null);

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      stop.current?.abort();
    };
  }, []);

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    const refreshAsked = asking.current;
    setLoading(true);
    try {
      const next = await getOutForDelivery();
      if (mine !== ticket.current || !live.current) return;
      setData(next);
      setError(null);
    } catch (err) {
      if (mine !== ticket.current || !live.current) return;
      setError(errorMessage(err));
    } finally {
      if (mine === ticket.current && live.current) {
        setLoading(false);
        setAnswered(refreshAsked);
      }
    }
  }, []);

  useEffect(() => {
    asking.current = refreshKey;
    void load();
    const timer = window.setInterval(() => void load(), RELOAD_MS);
    return () => window.clearInterval(timer);
  }, [load, refreshKey]);

  const readBack = async () => {
    if (!data || data.account_ids.length === 0) return;
    const controller = new AbortController();
    stop.current = controller;
    setReading(true);
    setNote(null);
    try {
      const job = await startSyncJob({ account_ids: data.account_ids });
      if (live.current) {
        setNote(
          `Reading ${countLabel(data.account_ids.length, "account")} back from Flipkart (run #${job.id}) — about a minute per account.`,
        );
      }
      const done = await waitForJob(job.id, { signal: controller.signal });
      if (!live.current) return;
      // An account the run passed over (an order run was using it, say) was
      // not read: said, with the run's own reason.
      const passed = done.items.filter((item) => item.status === "skipped");
      setNote(
        done.status !== "completed"
          ? "The read-back did not finish cleanly — the list shows what was read."
          : passed.length > 0
            ? `Read back from Flipkart, except ${countLabel(passed.length, "account")}: ${passed[0].message ?? "skipped"}`
            : "Read back from Flipkart just now.",
      );
      await load();
    } catch (err) {
      if (live.current && !controller.signal.aborted) setNote(errorMessage(err));
    } finally {
      if (live.current) setReading(false);
    }
  };

  /* From the frame the page's Refresh reaches the card until the card's own
     reply to it lands, placeholders stand in for the list (spec FR-049): the
     list from before is not shown as if it were the Refresh's answer. The
     card's own two-minute reload and the reload after "Read back now" are
     not the user's Refresh and keep the list on screen meanwhile: someone
     may be reading a delivery code out at the door. */
  const waiting = answered !== refreshKey;
  const shown = waiting ? null : data;
  const units = shown?.units ?? [];
  const title = units.length > 0 ? `Out for delivery — ${countLabel(units.length, "unit")}` : "Out for delivery";

  return (
    <Card
      title={title}
      description="Orders Flipkart says are on the courier's van, with the delivery code the courier asks for at the door. Read back from Flipkart every half hour while anything is out for delivery, and every hour for orders still on their way."
      actions={
        // A way to act, not a figure: offered for the units last seen, and
        // kept through a Refresh with a read-back's spinner in it.
        (data?.units.length ?? 0) > 0 ? (
          <Button size="sm" loading={reading} disabled={reading} onClick={() => void readBack()}>
            Read back now
          </Button>
        ) : null
      }
    >
      {shown === null && (waiting || loading) ? (
        <SkeletonRows rows={2} />
      ) : shown === null ? (
        <Alert tone="error" title="Could not load the orders out for delivery">
          {error}
          <Button size="sm" className="mt-2" onClick={() => void load()}>
            Try again
          </Button>
        </Alert>
      ) : units.length === 0 ? (
        <EmptyState
          title="Nothing is out for delivery"
          description="When Flipkart shows an order as out for delivery, it appears here with its delivery code."
        />
      ) : (
        <div className="flex flex-col gap-2">
          {note ? (
            <p role="status" className="text-[12px] text-ink-2">
              {note}
            </p>
          ) : null}
          {error ? (
            <p role="status" className="text-[12px] text-ink-2">
              Showing the last list that loaded — the refresh failed: {error}
            </p>
          ) : null}
          <Table columns={COLUMNS} rows={units} rowKey={(unit) => String(unit.unit_pk)} minWidth="720px" />
        </div>
      )}
    </Card>
  );
}

export default OutForDelivery;
