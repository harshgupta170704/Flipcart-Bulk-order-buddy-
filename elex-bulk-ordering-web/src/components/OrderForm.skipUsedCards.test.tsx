// @vitest-environment jsdom
/**
 * "Skip cards already used in an order" on Smart Bulk Order (the owner, 24 Nov
 * 2026: "the card which I have already used for ordering should be
 * excluded"). Ticked by default on a card run: the run, the card-pairing
 * preview and the "Validate card file" report all leave out a card that
 * already paid for an order in this tool. "Run again" starts it as the run had
 * it; older runs, which did not record it, start ticked.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job, JobDetail } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  previewOrderCards: vi.fn(),
  checkCardSheet: vi.fn(),
  getJob: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  listCardFailureCodes: () => Promise.resolve([]),
  getSettings: () => Promise.resolve({ smart_id_default_on: false }),
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

const LINK = "https://www.flipkart.com/vivo-t5-lite-5g/p/itm5420c578ec2df?pid=MOBHQSFF6MZK4YNP";
const CARDS = new File(["name,card_number,expiry_month,expiry_year,cvv,password"], "corpassist.csv", { type: "text/csv" });

const startedJob: Job = {
  id: 961,
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
  started_at: "2026-11-24T17:00:00Z",
  finished_at: null,
};

function run960(payload: Record<string, unknown>): JobDetail {
  return {
    ...startedJob,
    id: 960,
    status: "completed",
    payload: { product_link: LINK, payment_mode: "card", target: 16500, ...payload },
    items: [],
  };
}

beforeEach(() => {
  mocks.previewProduct.mockReturnValue(new Promise(() => undefined));
  mocks.previewOrderCards.mockReturnValue(new Promise(() => undefined));
  mocks.startOrderJob.mockResolvedValue(startedJob);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderForm(path = "/ordering") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <OrderForm
        accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 2, truncated: false }}
        runActive={false}
        onStarted={vi.fn()}
      />
    </MemoryRouter>,
  );
}

function fillNewRun() {
  fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: LINK } });
  fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "16500" } });
}

function attachCards() {
  const input = document.querySelector('input[name="cards_file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [CARDS] } });
}

function box(): HTMLInputElement {
  return screen.getByRole("checkbox", { name: /Skip cards already used in an order/ }) as HTMLInputElement;
}

async function submit(): Promise<void> {
  const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false), { timeout: 5000 });
  fireEvent.click(button);
  await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalled(), { timeout: 5000 });
}

function lastPreviewAsk() {
  const calls = mocks.previewOrderCards.mock.calls;
  return calls[calls.length - 1][0];
}

describe("OrderForm — Skip cards already used in an order", () => {
  it("is ticked by default on a card run, and Start sends it", async () => {
    renderForm();
    fillNewRun();
    expect(box().checked).toBe(true);
    attachCards();

    await submit();

    expect(mocks.startOrderJob.mock.calls[0][0].card_exclude_used).toBe(true);
  });

  it("unticked, Start sends it off", async () => {
    renderForm();
    fillNewRun();
    fireEvent.click(box());
    attachCards();

    await submit();

    expect(mocks.startOrderJob.mock.calls[0][0].card_exclude_used).toBe(false);
  });

  it("the card pairing preview leaves used cards out too, and stops when unticked", async () => {
    renderForm();
    fillNewRun();
    attachCards();
    await waitFor(() => expect(mocks.previewOrderCards).toHaveBeenCalled());
    expect(lastPreviewAsk().card_exclude_used).toBe(true);

    const before = mocks.previewOrderCards.mock.calls.length;
    fireEvent.click(box());
    await waitFor(() => expect(mocks.previewOrderCards.mock.calls.length).toBeGreaterThan(before));
    expect(lastPreviewAsk().card_exclude_used).toBeUndefined();
  });

  it("with an email sheet, the preview marks the account the run will pass over, and says why", async () => {
    mocks.previewOrderCards.mockResolvedValue({
      accounts: [
        {
          account_id: 7,
          email: "a@catbar.net",
          card_last4: "1111",
          has_password: true,
          skip_reason: "its card already paid for 1 order in this tool (placed, or sent and not confirmed) (skip cards already used)",
        },
        { account_id: 8, email: "b@catbar.net", card_last4: "4242", has_password: true, skip_reason: null },
      ],
      paired: 2,
      without_card: 0,
      paired_by: "email",
      rows_set_aside: [],
      cards_kept_out: 0,
      will_skip: 1,
    });
    renderForm();
    fillNewRun();
    attachCards();

    expect(await screen.findByText("1 account will be passed over: its card is kept out")).toBeTruthy();
    expect(screen.getByText(/will be skipped: card already used/)).toBeTruthy();
    expect(screen.getByText("ending 4242")).toBeTruthy();
  });

  it("the order preview lists it among the card filters", () => {
    renderForm();
    fillNewRun();
    expect(screen.getByText("skip cards already used")).toBeTruthy();
    fireEvent.click(box());
    expect(screen.queryByText("skip cards already used")).toBeNull();
  });

  it("Cash on Delivery shows no such box", () => {
    renderForm();
    fireEvent.change(document.querySelector('select[name="payment_mode"]') as HTMLSelectElement, { target: { value: "cod" } });
    expect(screen.queryByRole("checkbox", { name: /Skip cards already used in an order/ })).toBeNull();
  });

  it("Run again of a run that had it off starts unticked", async () => {
    mocks.getJob.mockResolvedValue(run960({ card_exclude_used: false }));
    renderForm("/ordering?from=960");
    await waitFor(() => expect(mocks.getJob).toHaveBeenCalled());
    await waitFor(() => expect(box().checked).toBe(false));
  });

  it("Run again of an older run, which did not record it, starts ticked", async () => {
    mocks.getJob.mockResolvedValue(run960({}));
    renderForm("/ordering?from=960");
    await waitFor(() => expect(mocks.getJob).toHaveBeenCalled());
    await screen.findByDisplayValue(LINK);
    expect(box().checked).toBe(true);
  });
});
