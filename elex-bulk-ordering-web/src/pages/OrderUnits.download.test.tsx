// @vitest-environment jsdom
/**
 * What the ledger says when a download completes (spec FR-016): the full
 * ledger CSV is confirmed in the spec's own words, "CSV downloaded" over
 * "Your export has been downloaded.", and a file of more than 20,000 rows adds
 * one more line saying it is big — never in place of the spec's line.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listOrders: vi.fn(),
  orderFilterOptions: vi.fn(),
  getOrderView: vi.fn(),
  listJobs: vi.fn(),
  downloadOrdersCsv: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  ...mocks,
  saveOrderView: vi.fn(),
  clearOrderView: vi.fn(),
  downloadOrders: vi.fn(),
  downloadInvoice: vi.fn(),
}));
vi.mock("../components/CompileInvoices", () => ({ CompileInvoicesControls: () => null }));
vi.mock("../components/ExtractInvoicesData", () => ({ ExtractInvoicesDataControls: () => null }));

import OrderUnits from "./OrderUnits";

beforeEach(() => {
  mocks.orderFilterOptions.mockResolvedValue({ statuses: [], platform_statuses: [], delivery_states: [], total: 0 });
  mocks.getOrderView.mockResolvedValue(null);
  mocks.listJobs.mockResolvedValue([]);
  mocks.downloadOrdersCsv.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

/** Open the page over `total` matching rows and download the layout named `choice`. */
async function download(total: number, choice: RegExp) {
  mocks.listOrders.mockResolvedValue({ items: [], total, page: 1, size: 100, pages: Math.ceil(total / 100) });
  render(
    <MemoryRouter>
      <OrderUnits />
    </MemoryRouter>,
  );
  const button = screen.getByRole("button", { name: "Download CSV" });
  await waitFor(() => expect(button.hasAttribute("disabled")).toBe(false));
  fireEvent.click(button);
  fireEvent.click(screen.getByRole("menuitem", { name: choice }));
  await waitFor(() => expect(mocks.downloadOrdersCsv).toHaveBeenCalled());
}

/** The success toast's two parts: its title and the line under it. */
async function confirmation(title: string): Promise<{ lines: string[] }> {
  const heading = await screen.findByText(title);
  const description = heading.nextElementSibling as HTMLElement;
  // The spec's line is the description's own text; a further line is a block of its own.
  const lines = Array.from(description.childNodes).map((node) => node.textContent ?? "");
  return { lines };
}

describe("a finished download", () => {
  it("confirms the full ledger CSV in the spec's words", async () => {
    await download(3, /^Full ledger CSV/);

    expect((await confirmation("CSV downloaded")).lines).toEqual(["Your export has been downloaded."]);
    expect(screen.queryByText("Full ledger CSV downloaded")).toBeNull();
  });

  it("adds the big-file line under the spec's above 20,000 rows", async () => {
    await download(25_000, /^Full ledger CSV/);

    expect((await confirmation("CSV downloaded")).lines).toEqual([
      "Your export has been downloaded.",
      "25,000 rows — a big file; give the spreadsheet a moment to open it.",
    ]);
  });

  it("names the owner's own layouts by theirs, with the same line", async () => {
    await download(3, /^Out for delivery sheet/);

    expect((await confirmation("Out for delivery sheet downloaded")).lines).toEqual([
      "Your export has been downloaded.",
    ]);
    expect(mocks.downloadOrdersCsv.mock.calls[0][1]).toBe("out_for_delivery");
  });
});
