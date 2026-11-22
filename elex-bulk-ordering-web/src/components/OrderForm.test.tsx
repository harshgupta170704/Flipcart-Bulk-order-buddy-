// @vitest-environment jsdom
/**
 * The Place orders form's price cap (audit F-23). A blank Target is an error
 * on the field and nothing is sent; ticking "No price cap" clears and turns off
 * the Target box, warns, and sends `no_cap`; a typed target is sent as the cap.
 *
 * The children that load data of their own — the account picker, the address
 * book, the unit-limit check — are stubbed. The stubs hand the form a two-account
 * selection and an address, which is all it needs from them to be submittable.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  listCardFailureCodes: () => Promise.resolve([]),
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

const MISSING_CAP = "Enter a price cap, or tick “No price cap” to order at any price.";
const NO_CAP_WARNING = "No price cap: orders are placed at any amount";

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
  // Cash on Delivery keeps the card sheet out of these tests.
  fireEvent.change(screen.getByLabelText(/Product URL/), {
    target: { value: "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001" },
  });
  fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });
  return { onStarted };
}

function targetInput(): HTMLInputElement {
  return screen.getByLabelText(/Target \(max payable per unit\)/) as HTMLInputElement;
}

function noCapBox(): HTMLInputElement {
  return screen.getByLabelText("No price cap — order at any price") as HTMLInputElement;
}

async function submit(): Promise<void> {
  const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
  // Enabled once the stubs have handed over the accounts and the address.
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
}

describe("OrderForm price cap", () => {
  it("refuses a blank Target on the field and sends nothing", async () => {
    renderForm();
    expect(noCapBox().checked).toBe(false);

    await submit();

    expect(await screen.findByText(MISSING_CAP)).toBeTruthy();
    expect(targetInput().getAttribute("aria-invalid")).toBe("true");
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
    // Nothing claims the run is uncapped: it is not, it is incomplete.
    expect(screen.queryByText(NO_CAP_WARNING)).toBeNull();
    expect(screen.queryByText(/No cap set/)).toBeNull();
    expect(screen.getByText(/Enter a price cap above, or tick “No price cap”\./)).toBeTruthy();
  });

  it("ticking No price cap clears and turns off the Target, warns, and sends no_cap", async () => {
    const { onStarted } = renderForm();
    fireEvent.change(targetInput(), { target: { value: "500" } });

    fireEvent.click(noCapBox());

    expect(noCapBox().checked).toBe(true);
    expect(targetInput().value).toBe("");
    expect(targetInput().disabled).toBe(true);
    expect(screen.getByText(NO_CAP_WARNING)).toBeTruthy();
    expect(screen.getByText(/No cap set — orders will be placed at any amount\./)).toBeTruthy();

    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0]).toMatchObject({
      target: null,
      no_cap: true,
      payment_mode: "cod",
      account_ids: [7, 8],
      address_id: 1,
    });
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(startedJob));
    expect(screen.queryByText(MISSING_CAP)).toBeNull();
  });

  it("unticks No price cap once the run has started, so the next run states its own cap", async () => {
    const { onStarted } = renderForm();
    fireEvent.click(noCapBox());
    await submit();
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(startedJob));

    expect(noCapBox().checked).toBe(false);
    expect(targetInput().disabled).toBe(false);
    // A second run from the same page without a cap is refused again.
    await submit();
    expect(await screen.findByText(MISSING_CAP)).toBeTruthy();
    expect(mocks.startOrderJob).toHaveBeenCalledTimes(1);
  });

  it("sends a typed target as the cap, with no_cap false", async () => {
    renderForm();
    fireEvent.change(targetInput(), { target: { value: "18250" } });

    expect(screen.queryByText(NO_CAP_WARNING)).toBeNull();
    expect(screen.queryByText(/No cap set/)).toBeNull();
    expect(screen.getByText(/Orders over the target will not be placed\./)).toBeTruthy();

    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0]).toMatchObject({ target: 18250, no_cap: false });
  });

  it("unticking turns the Target back on and asks for a cap again", async () => {
    renderForm();
    fireEvent.click(noCapBox());
    fireEvent.click(noCapBox());

    expect(targetInput().disabled).toBe(false);
    expect(screen.queryByText(NO_CAP_WARNING)).toBeNull();

    await submit();

    expect(await screen.findByText(MISSING_CAP)).toBeTruthy();
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });

  it("refuses a target of zero or less", async () => {
    renderForm();
    fireEvent.change(targetInput(), { target: { value: "0" } });

    await submit();

    expect(await screen.findByText(/A target of zero or less would fail every order/)).toBeTruthy();
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });
});
