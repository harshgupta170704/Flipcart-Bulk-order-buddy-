// @vitest-environment jsdom
/**
 * "Validate card file" on Smart Bulk Order (the owner, 24 Nov 2026): the
 * server reads the chosen sheet as the run will and says which CVVs it gave
 * their leading zeros back, which rows it set aside, and what to check.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  previewOrderCards: vi.fn(),
  checkCardSheet: vi.fn(),
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

const CARDS = new File(["name,card_number,expiry_month,expiry_year,cvv,password"], "corpassist.csv", { type: "text/csv" });
const OTHER = new File(["name,card_number,expiry_month,expiry_year,cvv,password"], "other.csv", { type: "text/csv" });

const REPORT = {
  cards: 10,
  with_password: 10,
  fixed: [
    { row: 2, card_last4: "3989", field: "CVV", note: "the CVV had 1 digit — 2 zeros added in front, now 3 digits" },
    { row: 5, card_last4: "4482", field: "CVV", note: "the CVV had 2 digits — 1 zero added in front, now 3 digits" },
  ],
  set_aside: [{ row: 12, reason: "the card number is not 12 to 19 digits" }],
  warnings: [{ row: 7, card_last4: "4185", note: "the bank password has 6 digits while most in this sheet have 7" }],
};

function attach(file: File) {
  const input = document.querySelector('input[name="cards_file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

function validateButton() {
  return screen.getByRole("button", { name: /Validate card file|Validating/ }) as HTMLButtonElement;
}

beforeEach(() => {
  mocks.previewOrderCards.mockReturnValue(new Promise(() => undefined));
});

describe("OrderForm — Validate card file", () => {
  it("is off until a card file is chosen", () => {
    renderForm();
    expect(validateButton().disabled).toBe(true);
    expect(mocks.checkCardSheet).not.toHaveBeenCalled();
  });

  it("checks the chosen file and lists what was fixed, set aside and to check", async () => {
    mocks.checkCardSheet.mockResolvedValue(REPORT);
    renderForm();
    attach(CARDS);

    expect(validateButton().disabled).toBe(false);
    fireEvent.click(validateButton());

    expect(await screen.findByText("Fixed 2 CVVs that had lost their leading zeros")).toBeTruthy();
    expect(mocks.checkCardSheet).toHaveBeenCalledWith(CARDS);
    expect(screen.getByText("Row 2 · card ending 3989")).toBeTruthy();
    expect(screen.getByText("Row 5 · card ending 4482")).toBeTruthy();
    expect(screen.getByText(/1 row set aside — the run will not use it/)).toBeTruthy();
    expect(screen.getByText("Row 12")).toBeTruthy();
    expect(screen.getByText("1 thing to check in the sheet")).toBeTruthy();
    expect(screen.getByText("Row 7 · card ending 4185")).toBeTruthy();
    expect(screen.getByText(/10 cards/)).toBeTruthy();
  });

  it("says so when the file has nothing to fix", async () => {
    mocks.checkCardSheet.mockResolvedValue({ cards: 3, with_password: 3, fixed: [], set_aside: [], warnings: [] });
    renderForm();
    attach(CARDS);
    fireEvent.click(validateButton());
    expect(await screen.findByText(/No problems found/)).toBeTruthy();
  });

  it("drops the report when another file is chosen, and never shows a late answer for the old one", async () => {
    let answer: (value: typeof REPORT) => void = () => undefined;
    mocks.checkCardSheet.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    renderForm();
    attach(CARDS);
    fireEvent.click(validateButton());
    expect(validateButton().textContent).toContain("Validating");

    attach(OTHER);
    await act(async () => answer(REPORT));
    expect(screen.queryByText(/Fixed 2 CVVs/)).toBeNull();
    expect(validateButton().disabled).toBe(false);
  });

  it("shows the server's refusal", async () => {
    mocks.checkCardSheet.mockRejectedValue(new Error("No usable card rows in the card file."));
    renderForm();
    attach(CARDS);
    fireEvent.click(validateButton());
    expect(await screen.findByText("The card file could not be validated")).toBeTruthy();
    expect(screen.getByText("No usable card rows in the card file.")).toBeTruthy();
  });

  it("Start still sends the sheet itself — the run makes the same fixes", async () => {
    mocks.checkCardSheet.mockResolvedValue(REPORT);
    mocks.startOrderJob.mockResolvedValue(startedJob);
    renderForm();
    fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "1000" } });
    attach(CARDS);
    fireEvent.click(validateButton());
    await screen.findByText("Fixed 2 CVVs that had lost their leading zeros");

    const submit = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.click(submit);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].cards_file).toBe(CARDS);
    // The file and its report go once the run has it.
    await waitFor(() => expect(screen.queryByText(/Fixed 2 CVVs/)).toBeNull());
  });
});
