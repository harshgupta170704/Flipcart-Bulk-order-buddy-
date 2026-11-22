// @vitest-environment jsdom
/**
 * The archive list a page at a time (spec US3, FR-038: the operator downloads
 * the new archive "or any earlier archive"). The dialog showed the newest 200
 * and nothing past them; now "Show older archives" adds the next page under
 * the rows shown, says how many there are in all, and Download and Delete work
 * on the rows it added as on any other.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CompiledArchive, CompiledArchivePage } from "../lib/types";

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

const idle = { active: null, last: null, archive_retention_days: 90 };

/** A made-up archive; the higher the id, the newer. */
function archive(id: number): CompiledArchive {
  return {
    id,
    name: `oct-${id}.zip`,
    size: 2048,
    orders: 1,
    units: 1,
    invoices: 1,
    left_out: 0,
    missing_files: 0,
    created_at: `2026-10-24T10:0${id}:00`,
    job_id: null,
  };
}

/** The page the server would send: `next_before` is the key of its last row. */
function page(ids: number[], total: number, more: boolean): CompiledArchivePage {
  const last = ids[ids.length - 1];
  return { items: ids.map(archive), total, next_before: more ? `2026-10-24T10:0${last}:00.000000,${last}` : null };
}

function openArchives() {
  render(<CompileInvoicesControls filters={{ date_field: "placed_at" }} total={4} push={vi.fn()} pollMs={100_000} />);
  fireEvent.click(screen.getByRole("button", { name: "Download Compiled Invoices" }));
}

/** The archive names in the table, top to bottom. */
function shownNames(): string[] {
  return screen.getAllByText(/^oct-\d\.zip$/).map((cell) => cell.textContent ?? "");
}

function rowOf(name: string): HTMLElement {
  return screen.getByText(name).closest("tr") as HTMLElement;
}

describe("the archive list past its first page", () => {
  it("adds the older archives under the ones shown, with how many there are in all", async () => {
    mocks.getCompileStatus.mockResolvedValue(idle);
    mocks.listArchives.mockResolvedValueOnce(page([5, 4], 3, true)).mockResolvedValueOnce(page([3], 3, false));
    openArchives();

    await screen.findByText("oct-5.zip");
    expect(shownNames()).toEqual(["oct-5.zip", "oct-4.zip"]);
    expect(screen.getByText("Showing 2 of 3")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Show older archives" }));

    await screen.findByText("oct-3.zip");
    expect(mocks.listArchives).toHaveBeenLastCalledWith("2026-10-24T10:04:00.000000,4");
    expect(shownNames()).toEqual(["oct-5.zip", "oct-4.zip", "oct-3.zip"]);
    // The oldest is on screen: nothing more to ask for.
    expect(screen.queryByRole("button", { name: "Show older archives" })).toBeNull();
    expect(screen.queryByText(/^Showing \d/)).toBeNull();
  });

  it("downloads and deletes an archive the button added, and the next page still starts after it", async () => {
    mocks.getCompileStatus.mockResolvedValue(idle);
    mocks.listArchives
      .mockResolvedValueOnce(page([5, 4], 5, true))
      .mockResolvedValueOnce(page([3, 2], 5, true))
      .mockResolvedValueOnce(page([1], 4, false));
    mocks.downloadArchive.mockResolvedValue(undefined);
    mocks.deleteArchive.mockResolvedValue(undefined);
    openArchives();
    fireEvent.click(await screen.findByRole("button", { name: "Show older archives" }));
    await screen.findByText("oct-2.zip");
    expect(screen.getByText("Showing 4 of 5")).toBeTruthy();

    fireEvent.click(within(rowOf("oct-3.zip")).getByRole("button", { name: "Download" }));
    await waitFor(() => expect(mocks.downloadArchive).toHaveBeenCalledWith(3));
    const remove = within(rowOf("oct-2.zip")).getByRole("button", { name: "Delete" }) as HTMLButtonElement;
    await waitFor(() => expect(remove.disabled).toBe(false)); // the download has handed over

    fireEvent.click(remove);
    fireEvent.click(within(rowOf("oct-2.zip")).getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(screen.queryByText("oct-2.zip")).toBeNull());
    expect(mocks.deleteArchive).toHaveBeenCalledWith(2);
    expect(shownNames()).toEqual(["oct-5.zip", "oct-4.zip", "oct-3.zip"]);
    expect(screen.getByText("Showing 3 of 4")).toBeTruthy();

    // The deleted row was the last one shown; the next page is asked for after it all the same.
    fireEvent.click(screen.getByRole("button", { name: "Show older archives" }));
    await screen.findByText("oct-1.zip");
    expect(mocks.listArchives).toHaveBeenLastCalledWith("2026-10-24T10:02:00.000000,2");
    expect(shownNames()).toEqual(["oct-5.zip", "oct-4.zip", "oct-3.zip", "oct-1.zip"]);
  });

  it("says when the older archives could not be loaded, and keeps the button to try again", async () => {
    mocks.getCompileStatus.mockResolvedValue(idle);
    mocks.listArchives
      .mockResolvedValueOnce(page([5, 4], 3, true))
      .mockRejectedValueOnce(new Error("Could not reach the server."))
      .mockResolvedValueOnce(page([3], 3, false));
    openArchives();

    fireEvent.click(await screen.findByRole("button", { name: "Show older archives" }));

    await screen.findByText(/Could not load the older archives: Could not reach the server\./);
    expect(shownNames()).toEqual(["oct-5.zip", "oct-4.zip"]);
    fireEvent.click(screen.getByRole("button", { name: "Show older archives" }));
    await screen.findByText("oct-3.zip");
    expect(screen.queryByText(/Could not load the older archives/)).toBeNull();
  });

  it("drops an older page that lands after the list was loaded again", async () => {
    mocks.getCompileStatus.mockResolvedValue(idle);
    let answerOlder: (value: CompiledArchivePage) => void = () => undefined;
    mocks.listArchives
      .mockResolvedValueOnce(page([5, 4], 3, true))
      .mockReturnValueOnce(new Promise<CompiledArchivePage>((resolve) => (answerOlder = resolve)))
      // Opened again: a new archive came in meanwhile.
      .mockResolvedValueOnce(page([6, 5, 4], 4, true));
    openArchives();
    fireEvent.click(await screen.findByRole("button", { name: "Show older archives" }));
    await waitFor(() => expect(mocks.listArchives).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Download Compiled Invoices" }));
    await screen.findByText("oct-6.zip");
    answerOlder(page([3], 3, false));

    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Show older archives" }) as HTMLButtonElement).disabled).toBe(false),
    );
    expect(shownNames()).toEqual(["oct-6.zip", "oct-5.zip", "oct-4.zip"]);
    expect(screen.getByText("Showing 3 of 4")).toBeTruthy();
  });

  it("drops an older page asked for from the list shown while its newest page was still loading", async () => {
    // Opened again, the dialog shows the list from before — its button too —
    // until the newest page comes back. A page asked for from that list
    // follows the list from before: added under the newest page, it would
    // step over the archives in between.
    mocks.getCompileStatus.mockResolvedValue(idle);
    let answerNewest: (value: CompiledArchivePage) => void = () => undefined;
    let answerOlder: (value: CompiledArchivePage) => void = () => undefined;
    mocks.listArchives
      .mockResolvedValueOnce(page([5, 4], 5, true))
      .mockReturnValueOnce(new Promise<CompiledArchivePage>((resolve) => (answerNewest = resolve)))
      .mockReturnValueOnce(new Promise<CompiledArchivePage>((resolve) => (answerOlder = resolve)))
      .mockResolvedValueOnce(page([5, 4], 7, true));
    openArchives();
    await screen.findByText("oct-5.zip");
    fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Download Compiled Invoices" }));
    await waitFor(() => expect(mocks.listArchives).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole("button", { name: "Show older archives" }));
    await waitFor(() => expect(mocks.listArchives).toHaveBeenCalledTimes(3));
    expect(mocks.listArchives).toHaveBeenLastCalledWith("2026-10-24T10:04:00.000000,4");
    // Two archives were compiled since: the newest page is theirs.
    answerNewest(page([7, 6], 7, true));
    await screen.findByText("oct-7.zip");
    answerOlder(page([3, 2], 7, true));

    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Show older archives" }) as HTMLButtonElement).disabled).toBe(false),
    );
    expect(shownNames()).toEqual(["oct-7.zip", "oct-6.zip"]);
    expect(screen.getByText("Showing 2 of 7")).toBeTruthy();

    // The next page starts after the newest page's last row: 5 and 4 are not stepped over.
    fireEvent.click(screen.getByRole("button", { name: "Show older archives" }));
    await screen.findByText("oct-5.zip");
    expect(mocks.listArchives).toHaveBeenLastCalledWith("2026-10-24T10:06:00.000000,6");
    expect(shownNames()).toEqual(["oct-7.zip", "oct-6.zip", "oct-5.zip", "oct-4.zip"]);
  });
});
