// @vitest-environment jsdom
/**
 * The Extract Invoices Data dialog rendered for real: the type list with its
 * counts and the left-out note (FR-026, FR-030), Download CSV disabled with
 * nothing ticked (FR-029), Product pre-ticked and the count line, and a
 * download that sends the ticked types and reports the left-out count.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getInvoiceTypes: vi.fn(),
  downloadInvoiceData: vi.fn(),
  startInvoiceFetch: vi.fn(),
  getJob: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { ExtractInvoicesDataControls } from "./ExtractInvoicesData";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const filters = { date_field: "placed_at" as const, statuses: ["placed" as const] };
/** What the dialog asks for by default: only the orders that were received. */
const delivered = { ...filters, delivery_states: ["delivered"] };

const found = {
  types: [
    { type: "Product", invoices: 3, units: 4 },
    { type: "Offer Handling Fee", invoices: 2, units: 2 },
    { type: "Secure Packaging Fee", invoices: 1, units: 1 },
  ],
  units: 6,
  units_without_invoice: 2,
};

function downloadButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Download CSV" }) as HTMLButtonElement;
}

describe("ExtractInvoicesDataControls", () => {
  it("lists the types with their counts, pre-ticks Product and notes the left-out units", async () => {
    mocks.getInvoiceTypes.mockResolvedValue(found);
    render(<ExtractInvoicesDataControls filters={filters} total={6} push={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Extract Invoices Data" }));

    expect(await screen.findByText("Selected 1 invoice type")).toBeTruthy();
    expect(mocks.getInvoiceTypes).toHaveBeenCalledWith(delivered);
    expect((screen.getByLabelText("Product") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("Offer Handling Fee") as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText("3 invoices · 4 units")).toBeTruthy();
    expect(screen.getByText("1 invoice · 1 unit")).toBeTruthy();
    expect(screen.getByText(/2 of the 6 matching units have no invoice the tool could read and will be left out/)).toBeTruthy();
    expect(downloadButton().disabled).toBe(false);
    expect(screen.getByText(/invoice data extracted from downloaded invoice PDFs/)).toBeTruthy();
  });

  it("disables Download CSV while nothing is ticked", async () => {
    mocks.getInvoiceTypes.mockResolvedValue({ ...found, types: found.types.slice(1), units_without_invoice: 0 });
    render(<ExtractInvoicesDataControls filters={filters} total={6} push={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Extract Invoices Data" }));

    expect(await screen.findByText("Selected 0 invoice types")).toBeTruthy();
    expect(downloadButton().disabled).toBe(true);
    expect(screen.queryByText(/will be left out/)).toBeNull();

    fireEvent.click(screen.getByLabelText("Secure Packaging Fee"));
    expect(screen.getByText("Selected 1 invoice type")).toBeTruthy();
    expect(downloadButton().disabled).toBe(false);

    fireEvent.click(screen.getByLabelText("Secure Packaging Fee"));
    expect(downloadButton().disabled).toBe(true);
  });

  it("downloads the ticked types and reports the left-out count", async () => {
    mocks.getInvoiceTypes.mockResolvedValue(found);
    mocks.downloadInvoiceData.mockResolvedValue({ rows: 7, units_left_out: 2 });
    const push = vi.fn();
    render(<ExtractInvoicesDataControls filters={filters} total={6} push={push} />);
    fireEvent.click(screen.getByRole("button", { name: "Extract Invoices Data" }));
    await screen.findByText("Selected 1 invoice type");

    fireEvent.click(screen.getByLabelText("Secure Packaging Fee"));
    expect(screen.getByText("Selected 2 invoice types")).toBeTruthy();
    fireEvent.click(downloadButton());

    await waitFor(() => expect(push).toHaveBeenCalled());
    expect(mocks.downloadInvoiceData).toHaveBeenCalledWith(delivered, ["Product", "Secure Packaging Fee"]);
    expect(push.mock.calls[0][0]).toMatchObject({ tone: "success", title: "CSV downloaded" });
    expect(push.mock.calls[0][0].description).toContain("7 invoice lines");
    expect(push.mock.calls[0][0].description).toContain(
      "2 of the matching units have no readable invoice of the chosen types and were left out",
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("shows a failed download in the dialog and the button stays disabled at zero units", async () => {
    mocks.getInvoiceTypes.mockResolvedValue(found);
    mocks.downloadInvoiceData.mockRejectedValue(new Error("Tick at least one invoice type."));
    const push = vi.fn();
    render(<ExtractInvoicesDataControls filters={filters} total={6} push={push} />);
    fireEvent.click(screen.getByRole("button", { name: "Extract Invoices Data" }));
    await screen.findByText("Selected 1 invoice type");

    fireEvent.click(downloadButton());

    expect(await screen.findByText("Tick at least one invoice type.")).toBeTruthy();
    // Said as a failed export, in the dialog where the choice was made (NFR-009).
    expect(screen.getByText("Export failed")).toBeTruthy();
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();

    cleanup();
    render(<ExtractInvoicesDataControls filters={filters} total={0} push={push} />);
    expect((screen.getByRole("button", { name: "Extract Invoices Data" }) as HTMLButtonElement).disabled).toBe(true);
  });
it("fetches the matching orders' invoices from Flipkart and lists the types again when done", async () => {
    mocks.getInvoiceTypes.mockResolvedValueOnce({ types: [], units: 6, units_without_invoice: 6 }).mockResolvedValue(found);
    const job = { id: 7, type: "sync", status: "running", progress: 0, total_items: 2, done_items: 0, failed_items: 0,
      skipped_items: 0, unconfirmed_items: 0, cancel_requested: false, payload: null, started_at: "2026-11-27T00:00:00Z", finished_at: null };
    mocks.startInvoiceFetch.mockResolvedValue({ job, orders: 5, accounts: 2 });
    mocks.getJob.mockResolvedValue({ ...job, status: "completed", done_items: 2, items: [] });
    const push = vi.fn();
    render(<ExtractInvoicesDataControls filters={filters} total={6} push={push} />);
    fireEvent.click(screen.getByRole("button", { name: "Extract Invoices Data" }));

    expect(await screen.findByText(/Fetch invoices from Flipkart/, { selector: "p" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Fetch invoices from Flipkart" }));

    expect(mocks.startInvoiceFetch).toHaveBeenCalledWith(delivered);
    expect(await screen.findByText(/Fetching the invoices of 5 orders/)).toBeTruthy();
    await waitFor(() => expect(mocks.getJob).toHaveBeenCalledWith(7), { timeout: 5000 });
    await waitFor(() => expect(push).toHaveBeenCalledWith(expect.objectContaining({ title: "Invoices fetched from Flipkart" })));
    expect(await screen.findByText("Selected 1 invoice type")).toBeTruthy();
    expect(mocks.getInvoiceTypes).toHaveBeenCalledTimes(2);
  }, 10000);

  it("covers only the chosen emails' delivered orders, and every order once the box is cleared", async () => {
    mocks.getInvoiceTypes.mockResolvedValue(found);
    const chosen = { ...filters, emails: ["one@test.com", "two@test.com"] };
    render(<ExtractInvoicesDataControls filters={chosen} total={6} push={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Extract Invoices Data" }));

    expect(await screen.findByText("For 2 emails: one@test.com, two@test.com")).toBeTruthy();
    const box = screen.getByLabelText("Only delivered (received) orders") as HTMLInputElement;
    expect(box.checked).toBe(true);
    await waitFor(() => expect(mocks.getInvoiceTypes).toHaveBeenLastCalledWith({ ...chosen, delivery_states: ["delivered"] }));

    fireEvent.click(box);
    await waitFor(() => expect(mocks.getInvoiceTypes).toHaveBeenLastCalledWith(chosen));
  });
});

