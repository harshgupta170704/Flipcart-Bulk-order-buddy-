// @vitest-environment jsdom
/**
 * The live run view rendered for real, with the stream hook replaced: the
 * per-account rows show how long the run spent on each account once it is
 * done with it (plan §6), nothing while an account is still running or was
 * written off unstarted, and the finished line carries the whole run's time.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Job, JobItem } from "../lib/types";
import type { JobStream } from "../lib/useJobStream";

const mocks = vi.hoisted(() => ({
  stream: { current: null as JobStream | null },
}));

vi.mock("../lib/useJobStream", () => ({
  useJobStream: () => mocks.stream.current,
}));

vi.mock("../lib/api", () => ({
  cancelJob: vi.fn(),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { ProgressStream } from "./ProgressStream";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function job(over: Partial<Job> = {}): Job {
  return {
    id: 5,
    type: "order",
    status: "completed",
    progress: 100,
    total_items: 3,
    done_items: 3,
    failed_items: 0,
    skipped_items: 1,
    unconfirmed_items: 0,
    cancel_requested: false,
    payload: null,
    started_at: "2026-10-26T04:30:00",
    finished_at: "2026-10-26T04:31:42",
    ...over,
  };
}

function item(over: Partial<JobItem>): JobItem {
  return {
    account_id: 1,
    email: "one@example.com",
    status: "done",
    message: null,
    updated_at: "2026-10-26T04:30:14",
    started_at: null,
    finished_at: null,
    seconds: null,
    ...over,
  };
}

function show(stream: Partial<JobStream>) {
  mocks.stream.current = {
    job: null,
    items: [],
    events: [],
    connected: true,
    error: null,
    ...stream,
  };
  return render(<ProgressStream jobId={5} title="Ordering run" />);
}

describe("ProgressStream per-account timing", () => {
  it("shows the seconds spent on each finished account and the run's total", () => {
    show({
      job: job(),
      items: [
        item({
          account_id: 1,
          email: "placed@example.com",
          status: "done",
          message: "placed OD1",
          started_at: "2026-10-26T04:30:00",
          finished_at: "2026-10-26T04:30:14.3",
          seconds: 14.3,
        }),
        item({
          account_id: 2,
          email: "slow@example.com",
          status: "failed",
          message: "over target: 18207.00 > 18200.00",
          started_at: "2026-10-26T04:30:00",
          finished_at: "2026-10-26T04:31:42",
          seconds: 102,
        }),
        item({
          account_id: 3,
          email: "nosession@example.com",
          status: "skipped",
          message: "no session",
          finished_at: "2026-10-26T04:30:00",
        }),
      ],
    });

    expect(screen.getByText("14s")).toBeTruthy();
    expect(screen.getByText("1m 42s")).toBeTruthy();
    // Written off before it was started: no time to show for it.
    const skipped = screen.getByText("nosession@example.com").closest("li");
    expect(skipped?.querySelector('[title="Time the run spent on this account"]')).toBeNull();
    expect(screen.getByText(/Finished in 1m 42s\./)).toBeTruthy();
  });

  it("says nothing about time while an account is still running", () => {
    show({
      job: job({ status: "running", progress: 0, done_items: 0, finished_at: null }),
      items: [
        item({
          account_id: 1,
          email: "busy@example.com",
          status: "running",
          started_at: "2026-10-26T04:30:00",
        }),
      ],
    });

    expect(screen.queryByTitle("Time the run spent on this account")).toBeNull();
    expect(screen.getByText(/^Running — 0 of 3 done\.$/)).toBeTruthy();
  });

  it("falls back to the two stamps when the stream carried no seconds", () => {
    show({
      job: job({ started_at: undefined, finished_at: null }),
      items: [
        item({
          account_id: 1,
          status: "done",
          started_at: "2026-10-26T04:30:00",
          finished_at: "2026-10-26T04:30:20",
          seconds: null,
        }),
      ],
    });

    expect(screen.getByText("20s")).toBeTruthy();
    // No stamps on the job row: the finished line does not invent a total.
    expect(screen.getByText(/^Finished\. All 3 processed\.$/)).toBeTruthy();
  });
});

describe("ProgressStream: Smart ID Selection's notes and set-aside lines", () => {
  const NOTE =
    "Smart ID Selection: on — 3 IDs set aside before the run started (2 at their unit limit, 1 ordered recently).";
  const RECENT = "set aside before the run started: ordered in the last 7 day(s)";

  it("shows the run's notes as short lines near the top", () => {
    show({
      job: job({
        status: "running",
        finished_at: null,
        payload: { smart_id: true, notes: [NOTE, "variant limits cannot apply: the link names no variant"] },
      }),
    });

    const notes = screen.getByRole("list", { name: "Run notes" });
    const lines = Array.from(notes.querySelectorAll("li")).map((line) => line.textContent);
    expect(lines).toEqual([NOTE, "variant limits cannot apply: the link names no variant"]);
  });

  it("shows no notes for a run that saved none", () => {
    show({ job: job({ payload: { smart_id: false } }) });
    expect(screen.queryByRole("list", { name: "Run notes" })).toBeNull();
  });

  it("says Smart ID Selection is not available for an order run from before it, above the run's notes", () => {
    show({ job: job({ payload: { product_link: "https://www.flipkart.com/p", notes: ["no brand or category given"] } }) });

    const notes = screen.getByRole("list", { name: "Run notes" });
    expect(Array.from(notes.querySelectorAll("li")).map((line) => line.textContent)).toEqual([
      "Smart ID Selection: not available (older run)",
      "no brand or category given",
    ]);
  });

  it("says it for an older order run that saved no settings at all, and never for other kinds of run", () => {
    show({ job: job({ payload: null }) });
    expect(screen.getByText("Smart ID Selection: not available (older run)")).toBeTruthy();
    cleanup();

    show({ job: job({ type: "login", payload: null }) });
    expect(screen.queryByText(/Smart ID Selection/)).toBeNull();
    expect(screen.queryByRole("list", { name: "Run notes" })).toBeNull();
  });

  it("adds no Latest line for a setting aside that named no ID", () => {
    show({
      job: job({ status: "running", finished_at: null, payload: { smart_id: true } }),
      events: [
        { type: "job_started", job_id: 5, key: 1, receivedAt: "2026-10-26T04:30:00" },
        { type: "items_set_aside", job_id: 5, groups: [], key: 2, receivedAt: "2026-10-26T04:30:01" },
        {
          type: "items_set_aside",
          job_id: 5,
          groups: [{ message: RECENT, account_ids: [] }],
          key: 3,
          receivedAt: "2026-10-26T04:30:02",
        },
      ],
    });

    expect(screen.queryByText(/set aside before the run started/)).toBeNull();
    expect(screen.queryByText(/items set aside/)).toBeNull();
    // The rest of Latest is still there.
    expect(screen.getByText("job started")).toBeTruthy();
  });

  it("names the setting aside in Latest, and shows each set-aside line skipped with its reason and no time", () => {
    show({
      job: job({ status: "running", finished_at: null, done_items: 3, skipped_items: 3, payload: { smart_id: true } }),
      items: [
        item({ account_id: 2, email: "recent@example.com", status: "skipped", message: RECENT, seconds: null }),
      ],
      events: [
        {
          type: "items_set_aside",
          job_id: 5,
          groups: [
            { message: RECENT, account_ids: [2] },
            { message: "set aside before the run started: unit limit reached", account_ids: [1, 3] },
          ],
          key: 1,
          receivedAt: "2026-10-26T04:30:01",
        },
      ],
    });

    expect(screen.getByText("3 IDs set aside before the run started")).toBeTruthy();
    const line = screen.getByText("recent@example.com").closest("li");
    expect(line?.textContent).toContain(RECENT);
    expect(line?.textContent).toContain("Skipped");
    expect(line?.querySelector('[title="Time the run spent on this account"]')).toBeNull();
  });
});

describe("ProgressStream: a note added during the run", () => {
  it("names the note in Latest as well as under the run's notes", () => {
    const note = "Smart ID Selection: off — each ID is checked when its turn comes.";
    show({
      job: job({ status: "running", finished_at: null, payload: { smart_id: false, notes: [note] } }),
      events: [{ type: "run_notes", job_id: 5, notes: [note], key: 1, receivedAt: "2026-10-26T04:30:01" }],
    });

    expect(screen.getAllByText(note)).toHaveLength(2);
    expect(screen.queryByText("run notes")).toBeNull();
  });
});
