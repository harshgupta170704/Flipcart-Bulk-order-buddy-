// @vitest-environment jsdom
/**
 * The Place orders page's notice for a run whose bank step a person finishes
 * (27 Oct 2026): what to do, and how long each bank page waits as the server
 * recorded it on the run, shown while the run is going.
 *
 * The form and the progress panel are stubbed: each is a button that stands
 * for the run starting or ending.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../lib/types";

const started = vi.hoisted(() => ({ job: null as unknown }));

vi.mock("../components/OrderForm", async () => {
  const { createElement } = await import("react");
  return {
    OrderForm: ({ onStarted }: { onStarted: (job: unknown) => void }) =>
      createElement("button", { type: "button", onClick: () => onStarted(started.job) }, "start"),
  };
});

vi.mock("../components/ProgressStream", async () => {
  const { createElement } = await import("react");
  return {
    ProgressStream: ({ onDone }: { onDone?: () => void }) =>
      createElement("button", { type: "button", onClick: () => onDone?.() }, "finish"),
  };
});

vi.mock("../components/RunAccounts", () => ({
  useRunAccounts: () => ({ accounts: [], loading: false, error: null, reload: () => undefined, total: 0, truncated: false }),
}));

import Ordering from "./Ordering";

const TITLE = "You finish the bank's step in this run";

function job(payload: Record<string, unknown> | null): Job {
  return {
    id: 67,
    type: "order",
    status: "running",
    progress: 0,
    total_items: 2,
    done_items: 0,
    failed_items: 0,
    skipped_items: 0,
    unconfirmed_items: 0,
    cancel_requested: false,
    payload,
    started_at: "2026-10-27T15:40:00Z",
    finished_at: null,
  };
}

function startRun(payload: Record<string, unknown> | null): void {
  started.job = job(payload);
  render(
    <MemoryRouter>
      <Ordering />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "start" }));
}

afterEach(() => {
  cleanup();
});

describe("Ordering page, bank step by hand", () => {
  it("tells the person what to do and how long each bank page waits", () => {
    startRun({ payment_mode: "card", bank_step_by_hand: true, bank_wait_seconds: 390 });

    expect(screen.getByText(TITLE)).toBeTruthy();
    expect(screen.getByText(/Leave it alone until the bank’s page opens, then type the card’s password or OTP there\./)).toBeTruthy();
    expect(
      screen.getByText(/Each bank page waits about 6 minutes; after that the tool marks that order unconfirmed \(it may still go through\)/),
    ).toBeTruthy();
  });

  it("says a few minutes when the run did not record the wait", () => {
    startRun({ payment_mode: "card", bank_step_by_hand: true });

    expect(screen.getByText(/Each bank page waits a few minutes;/)).toBeTruthy();
  });

  it("shows nothing for a run the tool pays by itself", () => {
    startRun({ payment_mode: "card" });

    expect(screen.queryByText(TITLE)).toBeNull();
  });

  it("goes once the run has ended", () => {
    startRun({ payment_mode: "card", bank_step_by_hand: true, bank_wait_seconds: 390 });

    fireEvent.click(screen.getByRole("button", { name: "finish" }));

    expect(screen.queryByText(TITLE)).toBeNull();
  });
});
