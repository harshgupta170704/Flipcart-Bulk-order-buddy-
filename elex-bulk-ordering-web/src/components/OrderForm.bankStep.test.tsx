// @vitest-environment jsdom
/**
 * The Place orders form's "I'll finish the bank's step myself" box
 * (27 Oct 2026): offered on a card run only, sent as bank_step_by_hand, and
 * ticked for one run at a time, like "No price cap".
 *
 * The children that load data of their own are stubbed, as in OrderForm.cards.test.tsx.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
vi.mock("./CardPairingPreview", () => ({ CardPairingPreview: () => null }));

import { OrderForm } from "./OrderForm";

const AUTH = /^Authentication/;
const CARDS = new File(["Email,Card Number,Expiry,CVV"], "cards.csv", { type: "text/csv" });

const startedJob: Job = {
  id: 66,
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
  started_at: "2026-10-27T15:30:00Z",
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
  render(
    <MemoryRouter>
      <OrderForm
        accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 2, truncated: false }}
        runActive={false}
        onStarted={vi.fn()}
      />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText(/Product URL/), {
    target: { value: "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001" },
  });
  fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "700" } });
}

function attachCards(): void {
  const input = document.querySelector('input[name="cards_file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [CARDS] } });
}

function auth(): HTMLSelectElement {
  return screen.getByLabelText(AUTH) as HTMLSelectElement;
}

function chooseOtp(): void {
  fireEvent.change(auth(), { target: { value: "otp" } });
}

async function start(): Promise<void> {
  const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
  await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
}

describe("OrderForm bank step by hand", () => {
  it("is offered on a card run, unticked, and not on Cash on Delivery", () => {
    renderForm();
    expect(auth().value).toBe("password");
    chooseOtp();
    expect(screen.getByText(/the tool types nothing there, so you type the card.s password or OTP/)).toBeTruthy();

    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });

    expect(screen.queryByLabelText(AUTH)).toBeNull();
  });

  it("sends bank_step_by_hand when ticked, and is unticked once the run has started", async () => {
    renderForm();
    attachCards();
    chooseOtp();

    await start();

    expect(mocks.startOrderJob.mock.calls[0][0]).toMatchObject({ payment_mode: "card", bank_step_by_hand: true });
    await waitFor(() => expect(auth().value).toBe("password"));
  });

  it("leaves the bank's step to the tool when not ticked", async () => {
    renderForm();
    attachCards();

    await start();

    expect(mocks.startOrderJob.mock.calls[0][0].bank_step_by_hand).toBe(false);
  });

  it("does not carry a tick over to a Cash-on-Delivery run", async () => {
    renderForm();
    chooseOtp();
    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });
    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "card" } });
    expect(auth().value).toBe("password");
    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });

    await start();

    expect(mocks.startOrderJob.mock.calls[0][0]).toMatchObject({ payment_mode: "cod", bank_step_by_hand: false });
  });
});
