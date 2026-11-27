// @vitest-environment jsdom
/**
 * The live run stream's `items_set_aside` event (Smart ID, part 1): Smart ID
 * Selection set IDs aside before the run started, one group per reason. Each
 * listed account's line becomes skipped with its group's message and no time,
 * and the run's counts become the ones the event carries.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job, JobItem } from "./types";

vi.mock("./api", () => ({
  getToken: () => "token",
  jobStreamUrl: (id: number) => `ws://test/ws/jobs/${id}`,
}));

import { useJobStream } from "./useJobStream";

/** Stands in for the browser's WebSocket: the test plays the server. */
class FakeSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static last: FakeSocket | null = null;
  readyState = FakeSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(
    public url: string,
    public protocols: string[],
  ) {
    FakeSocket.last = this;
  }
  close() {
    this.readyState = 3;
  }
  send(message: object) {
    act(() => this.onmessage?.({ data: JSON.stringify(message) }));
  }
}

function job(over: Partial<Job> = {}): Job {
  return {
    id: 9,
    type: "order",
    status: "running",
    progress: 0,
    total_items: 4,
    done_items: 0,
    failed_items: 0,
    skipped_items: 0,
    unconfirmed_items: 0,
    cancel_requested: false,
    payload: { smart_id: true },
    started_at: "2026-11-24T08:00:00",
    finished_at: null,
    ...over,
  };
}

function pending(accountId: number, email: string): JobItem {
  return {
    account_id: accountId,
    email,
    status: "pending",
    message: null,
    updated_at: "2026-11-24T08:00:00",
    started_at: null,
    finished_at: null,
    seconds: null,
  };
}

beforeEach(() => {
  FakeSocket.last = null;
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function connect() {
  const hook = renderHook(() => useJobStream(9));
  const socket = FakeSocket.last;
  if (!socket) throw new Error("no socket opened");
  act(() => socket.onopen?.());
  socket.send({
    type: "snapshot",
    job_id: 9,
    job: job(),
    items: [pending(1, "full@example.com"), pending(2, "recent@example.com"), pending(3, "free@example.com"), pending(4, "full2@example.com")],
  });
  return { hook, socket };
}

const AT_LIMIT =
  "set aside before the run started: unit limit reached — 2 units of brand 'vivo' per 30 days, nothing left in its window";
const RECENT = "set aside before the run started: ordered in the last 7 day(s)";

describe("useJobStream: items_set_aside", () => {
  it("skips each listed account's line with its group's message and takes the event's counts", () => {
    const { hook, socket } = connect();

    socket.send({
      type: "items_set_aside",
      job_id: 9,
      groups: [
        { message: AT_LIMIT, account_ids: [1, 4] },
        { message: RECENT, account_ids: [2] },
      ],
      progress: 75,
      done_items: 3,
      skipped_items: 3,
      total_items: 4,
    });

    const byId = new Map(hook.result.current.items.map((item) => [item.account_id, item]));
    expect(byId.get(1)).toMatchObject({ status: "skipped", message: AT_LIMIT, seconds: null, email: "full@example.com" });
    expect(byId.get(4)).toMatchObject({ status: "skipped", message: AT_LIMIT, seconds: null });
    expect(byId.get(2)).toMatchObject({ status: "skipped", message: RECENT, seconds: null });
    // Not listed: untouched.
    expect(byId.get(3)).toMatchObject({ status: "pending", message: null });
    expect(hook.result.current.items).toHaveLength(4);

    expect(hook.result.current.job).toMatchObject({
      progress: 75,
      done_items: 3,
      skipped_items: 3,
      total_items: 4,
      failed_items: 0,
      status: "running",
    });
    // The settings the snapshot carried are kept.
    expect(hook.result.current.job?.payload).toEqual({ smart_id: true });
  });

  it("adds a line for an account the snapshot did not carry yet", () => {
    const { hook, socket } = connect();

    socket.send({
      type: "items_set_aside",
      job_id: 9,
      groups: [{ message: RECENT, account_ids: [12] }],
      progress: 20,
      done_items: 1,
      skipped_items: 1,
      total_items: 5,
    });

    const added = hook.result.current.items.find((item) => item.account_id === 12);
    expect(added).toMatchObject({ status: "skipped", message: RECENT, seconds: null, email: "" });
    expect(hook.result.current.job).toMatchObject({ done_items: 1, skipped_items: 1, total_items: 5 });
  });

  it("keeps the counts it has when the event leaves one out", () => {
    const { hook, socket } = connect();

    socket.send({ type: "items_set_aside", job_id: 9, groups: [{ message: RECENT, account_ids: [2] }], skipped_items: 1 });

    expect(hook.result.current.job).toMatchObject({ done_items: 0, skipped_items: 1, total_items: 4, progress: 0 });
  });

  it("takes the run's notes when the event carries them", () => {
    const { hook, socket } = connect();
    const note = "Smart ID Selection: on — 1 ID set aside before the run started (1 ordered recently).";

    socket.send({
      type: "items_set_aside",
      job_id: 9,
      groups: [{ message: RECENT, account_ids: [2] }],
      done_items: 1,
      skipped_items: 1,
      total_items: 4,
      notes: [note],
    });

    expect(hook.result.current.job?.payload).toEqual({ smart_id: true, notes: [note] });
  });

  it("changes no line for a setting aside that named no ID", () => {
    const { hook, socket } = connect();
    const before = hook.result.current.items;

    socket.send({
      type: "items_set_aside",
      job_id: 9,
      groups: [{ message: RECENT, account_ids: [] }],
      done_items: 0,
      skipped_items: 0,
      total_items: 4,
    });
    socket.send({ type: "items_set_aside", job_id: 9, groups: [] });

    expect(hook.result.current.items).toBe(before);
    expect(hook.result.current.items.every((item) => item.status === "pending")).toBe(true);
    expect(hook.result.current.job).toMatchObject({ done_items: 0, skipped_items: 0, total_items: 4 });
  });

  it("keeps the event in the run's latest events", () => {
    const { hook, socket } = connect();
    socket.send({ type: "items_set_aside", job_id: 9, groups: [{ message: RECENT, account_ids: [2] }] });

    const last = hook.result.current.events[hook.result.current.events.length - 1];
    expect(last.type).toBe("items_set_aside");
    expect(last.groups).toEqual([{ message: RECENT, account_ids: [2] }]);
  });
});

describe("useJobStream: run_notes", () => {
  it("shows the run's notes as they now stand, without a reload", () => {
    const { hook, socket } = connect();
    const notes = [
      "Skip accounts that already bought this product: 1 account(s) already bought this product — passed over; 3 left to order on.",
      "Smart ID Selection: off — each ID is checked when its turn comes.",
    ];

    socket.send({ type: "run_notes", job_id: 9, notes });

    expect(hook.result.current.job?.payload?.notes).toEqual(notes);
    // The run's other settings stay as they were, and no line moves.
    expect(hook.result.current.job?.payload?.smart_id).toBe(true);
    expect(hook.result.current.items.every((item) => item.status === "pending")).toBe(true);
  });

  it("ignores a notes event that carries no list", () => {
    const { hook, socket } = connect();

    socket.send({ type: "run_notes", job_id: 9 });

    expect(hook.result.current.job?.payload).toEqual({ smart_id: true });
  });
});
