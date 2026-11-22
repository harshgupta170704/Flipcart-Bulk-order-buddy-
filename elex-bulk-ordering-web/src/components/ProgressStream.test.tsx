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
