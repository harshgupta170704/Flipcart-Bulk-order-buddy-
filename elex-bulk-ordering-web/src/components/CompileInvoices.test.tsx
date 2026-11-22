// @vitest-environment jsdom
/**
 * The compile controls rendered for real: the FR-033 announcement, the
 * disabled "Compiling Invoices..." with a Cancel beside it, polling while a
 * run is active with its end announced once, a failed run with no recorded
 * reason shown as a failure (FR-036), and the two-click Delete.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../lib/types";

const mocks = vi.hoisted(() => ({
  getCompileStatus: vi.fn(),
  startCompile: vi.fn(),
  listArchives: vi.fn(),
  deleteArchive: vi.fn(),
  downloadArchive: vi.fn(),
  cancelJob: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { CompileInvoicesControls } from "./CompileInvoices";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function job(over: Partial<Job> = {}): Job {
  return {
    id: 7,
    type: "compile",
    status: "running",
    progress: 0,
    total_items: 3,
    done_items: 0,
    failed_items: 0,
    skipped_items: 0,
    unconfirmed_items: 0,
    cancel_requested: false,
    payload: { archive_name: "oct.zip" },
    started_at: "2026-10-24T10:00:00",
    finished_at: null,
    ...over,
  };
}

const idle = { active: null, last: null, archive_retention_days: 90 };

describe("CompileInvoicesControls", () => {
  it("announces a start and shows the compiling state with a Cancel beside it", async () => {
    // Idle at mount; the run the Start reports is what every later status says.
    mocks.getCompileStatus.mockResolvedValueOnce(idle).mockResolvedValue({ ...idle, active: job() });
    mocks.startCompile.mockResolvedValue({ job: job(), archive_name: "oct.zip", orders: 3, units: 4, units_without_invoice: 1 });
    const push = vi.fn();
    render(<CompileInvoicesControls filters={{ date_field: "placed_at" }} total={4} push={push} pollMs={100_000} />);
    await waitFor(() => expect(mocks.getCompileStatus).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Compile Invoices" }));
    fireEvent.change(screen.getByLabelText(/Invoice Name Prefix/), { target: { value: "oct" } });
    fireEvent.click(screen.getByRole("button", { name: "Start Compilation" }));

    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(push.mock.calls[0][0]).toMatchObject({ tone: "info", title: "Invoice compilation started" });
    expect(push.mock.calls[0][0].description).toContain("Compiling invoices for 3 orders. This may take a few minutes.");
    expect(push.mock.calls[0][0].description).toContain("1 of 4 units have no invoice on file");
    const compiling = screen.getByRole("button", { name: "Compiling Invoices..." }) as HTMLButtonElement;
    expect(compiling.disabled).toBe(true);
    expect(screen.getAllByRole("button", { name: "Cancel" }).length).toBeGreaterThan(0);
  });

  it("polls while a run is active and announces its end once", async () => {
    mocks.getCompileStatus
      .mockResolvedValueOnce({ ...idle, active: job() })
      .mockResolvedValue({
        ...idle,
        last: job({ status: "completed", finished_at: "2026-10-24T10:01:00", payload: { archive_name: "oct.zip", outcome: "completed" } }),
      });
    const push = vi.fn();
    render(<CompileInvoicesControls filters={{ date_field: "placed_at" }} total={4} push={push} pollMs={10} />);

    await waitFor(() => expect(push).toHaveBeenCalledWith(expect.objectContaining({ title: "Invoices compiled" })));
    expect(mocks.getCompileStatus.mock.calls.length).toBeGreaterThanOrEqual(2);
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(push).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Compile Invoices" })).toBeTruthy();
  });

  it("shows a run that failed without a recorded reason as a failure", async () => {
    mocks.getCompileStatus.mockResolvedValue({ ...idle, last: job({ status: "failed", payload: { archive_name: "oct.zip" } }) });
    mocks.listArchives.mockResolvedValue({ items: [], total: 0, next_before: null });
    render(<CompileInvoicesControls filters={{ date_field: "placed_at" }} total={4} push={vi.fn()} pollMs={100_000} />);
    await waitFor(() => expect(mocks.getCompileStatus).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Download Compiled Invoices" }));

    await waitFor(() => expect(screen.getByText("The last compilation (oct.zip) failed.")).toBeTruthy());
    expect(screen.getByText(/The server stopped before the compilation finished/)).toBeTruthy();
    expect(screen.getByText(/kept for 90 days/)).toBeTruthy();
  });

  it("deletes an archive only on the second click", async () => {
    mocks.getCompileStatus.mockResolvedValue(idle);
    mocks.listArchives.mockResolvedValue({
      items: [
        { id: 3, name: "oct.zip", size: 2048, orders: 2, units: 2, invoices: 2, left_out: 0, missing_files: 0, created_at: "2026-10-24T10:01:00", job_id: 7 },
      ],
      total: 1,
      next_before: null,
    });
    mocks.deleteArchive.mockResolvedValue(undefined);
    render(<CompileInvoicesControls filters={{ date_field: "placed_at" }} total={4} push={vi.fn()} pollMs={100_000} />);
    fireEvent.click(screen.getByRole("button", { name: "Download Compiled Invoices" }));
    const first = await screen.findByRole("button", { name: "Delete" });

    fireEvent.click(first);
    expect(mocks.deleteArchive).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() => expect(mocks.deleteArchive).toHaveBeenCalledWith(3));
    expect(mocks.deleteArchive).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByText("oct.zip")).toBeNull());
  });
});
