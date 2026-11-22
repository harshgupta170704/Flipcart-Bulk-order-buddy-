import { useEffect, useState } from "react";
import { getToken, jobStreamUrl } from "./api";
import type { Job, JobItem, JobItemStatus, JobStreamMessage } from "./types";

/** An incoming message plus the moment we saw it, so logs can be ordered. */
export interface StreamEvent extends JobStreamMessage {
  /** Monotonic per-connection key — safe as a React list key. */
  key: number;
  receivedAt: string;
}

export interface JobStream {
  job: Job | null;
  items: JobItem[];
  /** Oldest first, capped at the most recent 200. */
  events: StreamEvent[];
  connected: boolean;
  error: string | null;
}

const MAX_RECONNECTS = 2;
const MAX_EVENTS = 200;

function parseMessage(raw: unknown): JobStreamMessage | null {
  if (typeof raw !== "string") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  if (typeof (parsed as Record<string, unknown>).type !== "string") return null;
  return parsed as JobStreamMessage;
}

function upsertItem(
  items: JobItem[],
  accountId: number,
  patch: Partial<JobItem>,
): JobItem[] {
  const now = new Date().toISOString();
  const index = items.findIndex((item) => item.account_id === accountId);
  if (index === -1) {
    return [
      ...items,
      {
        account_id: accountId,
        email: "",
        status: "running",
        message: null,
        updated_at: now,
        started_at: null,
        finished_at: null,
        seconds: null,
        ...patch,
      },
    ];
  }
  const next = items.slice();
  next[index] = { ...next[index], ...patch, updated_at: now };
  return next;
}

/**
 * Live job progress over `/ws/jobs/{id}`.
 *
 * Applies the opening snapshot, then merges each event into it. Closes on
 * `job_finished` and on unmount, and reconnects at most twice after an
 * unexpected close. Changing `jobId` tears the old socket down first, so
 * sockets never pile up.
 */
export function useJobStream(jobId: number | null): JobStream {
  const [job, setJob] = useState<Job | null>(null);
  const [items, setItems] = useState<JobItem[]>([]);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setJob(null);
    setItems([]);
    setEvents([]);
    setConnected(false);
    setError(null);

    if (jobId === null) return;

    const token = getToken();
    if (!token) {
      setError("Not signed in — live updates are unavailable.");
      return;
    }

    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let retries = 0;
    let finished = false;
    let disposed = false;
    let seq = 0;

    const detach = (ws: WebSocket) => {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
    };

    const apply = (msg: JobStreamMessage) => {
      seq += 1;
      const stamped: StreamEvent = { ...msg, key: seq, receivedAt: new Date().toISOString() };
      setEvents((prev) => {
        const next = [...prev, stamped];
        return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
      });

      if (msg.type === "snapshot") {
        if (msg.job) setJob(msg.job);
        if (msg.items) setItems(msg.items);
        return;
      }

      const progress = msg.progress;
      if (typeof progress === "number") {
        setJob((prev) => (prev ? { ...prev, progress } : prev));
      }

      const accountId = msg.account_id;
      const message = msg.message ?? null;

      if (msg.type === "job_started") {
        setJob((prev) => (prev ? { ...prev, status: "running" } : prev));
        return;
      }

      if (msg.type === "item_started" && typeof accountId === "number") {
        // The clock for this account starts now; a stale end from an earlier
        // attempt goes with it so the duration is never read off two runs.
        setItems((prev) =>
          upsertItem(prev, accountId, {
            status: "running",
            message,
            started_at: stamped.receivedAt,
            finished_at: null,
            seconds: null,
          }),
        );
        return;
      }

      if (msg.type === "item_finished" && typeof accountId === "number") {
        const ok = msg.ok !== false;
        const skipped = msg.skipped === true;
        const unconfirmed = msg.unconfirmed === true;
        const status: JobItemStatus = skipped
          ? "skipped"
          : unconfirmed
            ? "unconfirmed"
            : ok
              ? "done"
              : "failed";
        // The server's seconds are the authority (its clock stamped both
        // ends); the local end stamp is the fallback for a start we saw live.
        const seconds = typeof msg.seconds === "number" ? msg.seconds : null;
        setItems((prev) =>
          upsertItem(prev, accountId, {
            status,
            message,
            finished_at: stamped.receivedAt,
            seconds,
          }),
        );
        setJob((prev) =>
          prev
            ? {
                ...prev,
                done_items: prev.done_items + 1,
                failed_items: ok || skipped || unconfirmed ? prev.failed_items : prev.failed_items + 1,
                skipped_items: skipped ? (prev.skipped_items ?? 0) + 1 : prev.skipped_items,
                unconfirmed_items: unconfirmed
                  ? (prev.unconfirmed_items ?? 0) + 1
                  : prev.unconfirmed_items,
              }
            : prev,
        );
        return;
      }

      if (msg.type === "job_finished") {
        finished = true;
        // The server says how the run ended in `status` (completed / failed);
        // it never sends `ok` on this event, so reading that marked every run
        // "completed" (audit F-35, 25 Oct 2026).
        const ended = msg.status === "failed" ? "failed" : "completed";
        setJob((prev) =>
          prev
            ? {
                ...prev,
                status: ended,
                progress: 100,
                finished_at: prev.finished_at ?? new Date().toISOString(),
              }
            : prev,
        );
        if (socket) {
          const ws = socket;
          socket = null;
          detach(ws);
          setConnected(false);
          ws.close();
        }
      }
    };

    const open = () => {
      if (disposed) return;
      let ws: WebSocket;
      try {
        // The token rides in the Sec-WebSocket-Protocol header, never the URL
        // (see jobStreamUrl); the server answers with the "bearer" protocol.
        ws = new WebSocket(jobStreamUrl(jobId), ["bearer", token]);
      } catch {
        setError("Could not open the live connection.");
        return;
      }
      socket = ws;

      ws.onopen = () => {
        if (disposed) return;
        setConnected(true);
        setError(null);
      };

      ws.onmessage = (event: MessageEvent) => {
        if (disposed) return;
        const msg = parseMessage(event.data);
        if (msg) apply(msg);
      };

      ws.onerror = () => {
        // onclose always follows; the retry logic lives there.
      };

      ws.onclose = () => {
        if (disposed) return;
        setConnected(false);
        if (finished) return;
        if (retries < MAX_RECONNECTS) {
          retries += 1;
          timer = window.setTimeout(open, 1000 * retries);
        } else {
          setError("Live updates disconnected. Reload the page to resume watching.");
        }
      };
    };

    open();

    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      if (socket) {
        const ws = socket;
        socket = null;
        detach(ws);
        if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
          ws.close();
        }
      }
    };
  }, [jobId]);

  return { job, items, events, connected, error };
}
