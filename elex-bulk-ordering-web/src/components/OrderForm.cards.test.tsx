// @vitest-environment jsdom
/**
 * The Place orders form with a card sheet attached: the pairing the run will
 * use shows before Start, and goes with the sheet once the run has started.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  previewOrderCards: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  listCardFailureCodes: () => Promise.resolve([]),
  // Where Smart ID Selection starts (on, the server's default), and its room check.
  getSettings: () => Promise.resolve({ smart_id_default_on: true }),
  checkRun: () => new Promise(() => undefined),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

vi.mock("./RunAccounts", async () => {
  const { useEffect } = await import("react");
  return {
    RunAccountPicker: ({ onSelectionChange }: { onSelectionChange?: (next: unknown) => void }) => {
      useEffect(() => onSelectionChange?.({ mode: "ids", ids: [7, 8] }), [onSelectionChange]);
      return null;
    },
  };
});

vi.mock("./AddressGstPicker", async () => {
  const { useEffect } = await import("react");
  return {
    AddressGstPicker: ({ onChange }: { onChange: (next: { address_id: number; gst_id: null }) => void }) => {
      useEffect(() => onChange({ address_id: 1, gst_id: null }), [onChange]);
      return null;
    },
  };
});

vi.mock("./LimitUsageSummary", () => ({ LimitUsageSummary: () => null }));

import { OrderForm } from "./OrderForm";


const startedJob: Job = {
  id: 41,
  type: "order",
  status: "queued",
  progress: 0,
  total_items: 2,
  done_items: 0,
  failed_items: 0,
  skipped_items: 0,
  unconfirmed_items: 0,
  cancel_requested: false,
  payload: null,
  started_at: "2026-10-26T06:30:00Z",
  finished_at: null,
};

beforeEach(() => {
  // The product preview is not what is under test: it never answers.
  mocks.previewProduct.mockReturnValue(new Promise(() => undefined));
  mocks.startOrderJob.mockResolvedValue(startedJob);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderForm() {
  const onStarted = vi.fn();
  render(
    <MemoryRouter>
      <OrderForm
        accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 2, truncated: false }}
        runActive={false}
        onStarted={onStarted}
      />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText(/Product URL/), {
    target: { value: "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001" },
  });
  return { onStarted };
}

const CARDS = new File(["Email,Card Number,Expiry,CVV"], "cards.csv", { type: "text/csv" });

describe("OrderForm card pairing", () => {
  it("shows which card pays for which account once a sheet is attached, and lets go of it after Start", async () => {
    mocks.previewOrderCards.mockResolvedValue({
      accounts: [
        { account_id: 7, email: "a7@test.com", card_last4: "4242", has_password: false },
        { account_id: 8, email: "a8@test.com", card_last4: null, has_password: null },
      ],
      paired: 1,
      without_card: 1,
      paired_by: "email",
      rows_set_aside: [],
    });
    renderForm();
    fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "1000" } });

    const input = document.querySelector('input[name="cards_file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [CARDS] } });

    expect(await screen.findByText("ending 4242")).toBeTruthy();
    expect(screen.getByText("No card — will be skipped")).toBeTruthy();
    // With Smart ID on (the setting) the preview is always told who the run
    // passes over, even with nothing ticked: a wildcard limit can set IDs aside.
    // "Skip cards already used in an order" is ticked by default, so the preview asks for it too.
    expect(mocks.previewOrderCards).toHaveBeenCalledWith({
      cards_file: CARDS,
      account_ids: [7, 8],
      account_filter: undefined,
      card_exclude_used: true,
      leave_out: { qty: 1, qty_mode: "upto", total_quantity: null, exclude_ordered_days: null, smart_id: true, ai_run: false },
    });

    const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].cards_file).toBe(CARDS); // Start sends the sheet itself
    await waitFor(() => expect(screen.queryByText("ending 4242")).toBeNull());
  });

  it("tells the pairing who the run passes over, so a pool's cards go to the IDs it tries", async () => {
    // Run #960 (24 Nov 2026): "Exclude IDs" and "Skip IDs that already bought
    // this product" ticked, yet a pool sheet's cards went to those IDs first.
    mocks.previewOrderCards.mockResolvedValue({
      accounts: [
        { account_id: 8, email: "a8@test.com", card_last4: "4242", has_password: true, passed_over: null },
        { account_id: 7, email: "a7@test.com", card_last4: null, has_password: null, passed_over: "ordered in the last 7 day(s)" },
      ],
      paired: 1,
      without_card: 0,
      paired_by: "balance",
      rows_set_aside: [],
      passed_over: 1,
    });
    renderForm();
    fireEvent.click(document.querySelector('input[name="exclude_ids"]') as HTMLInputElement);
    fireEvent.click(document.querySelector('input[name="skip_bought_product"]') as HTMLInputElement);

    const input = document.querySelector('input[name="cards_file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [CARDS] } });

    expect(await screen.findByText("Not tried — ordered in the last 7 day(s)")).toBeTruthy();
    const asked = mocks.previewOrderCards.mock.calls.at(-1)?.[0];
    expect(asked.leave_out).toEqual({
      qty: 1,
      qty_mode: "upto",
      total_quantity: null,
      exclude_ordered_days: 7,
      skip_bought_product: true,
      product_link: "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001",
      // Smart ID starts on (the setting); not an AI run.
      smart_id: true,
      ai_run: false,
    });
  });

  it("asks nothing while no sheet is attached", () => {
    renderForm();
    expect(screen.queryByText("Which card pays for which account")).toBeNull();
    expect(mocks.previewOrderCards).not.toHaveBeenCalled();
  });

  it.each([
    [
      "a file that is not a sheet",
      new File(["%PDF-1.4 test bytes"], "statement.pdf", { type: "application/pdf" }),
      "That is not an .xlsx or .csv file. Upload the corporate-card sheet.",
    ],
    [
      "a sheet over 1 MB",
      new File([new Uint8Array(1_300_000)], "HDFC corporate cards.xlsx", {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
      "That file is 1.24 MB; a card sheet can be at most 1 MB (most are a few KB). Upload the corporate-card sheet.",
    ],
  ])("says at once why %s cannot go, and neither previews nor sends it", async (_, file, says) => {
    renderForm();
    fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "1000" } });
    const input = document.querySelector('input[name="cards_file"]') as HTMLInputElement;

    fireEvent.change(input, { target: { files: [file] } });

    // Said on pick, before Start is pressed.
    expect(screen.getByText(says)).toBeTruthy();
    expect(screen.queryByText(/Ready to send/)).toBeNull();
    // No pairing preview, so nothing is uploaded for one, even after its wait.
    await act(() => new Promise((resolve) => window.setTimeout(resolve, 400)));
    expect(screen.queryByText("Which card pays for which account")).toBeNull();
    expect(mocks.previewOrderCards).not.toHaveBeenCalled();

    // And Start refuses it too.
    const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await act(() => new Promise((resolve) => window.setTimeout(resolve, 0)));
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
    expect(screen.getByText(says)).toBeTruthy();
  });
});
