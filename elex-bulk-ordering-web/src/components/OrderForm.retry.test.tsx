// @vitest-environment jsdom
/**
 * "Retry failed" (owner, 24 Nov 2026): the Orders page opens the form as
 * /ordering?retry=<run>. The run's settings are filled in as for Run again,
 * exactly its accounts that got no order are ticked, "Skip IDs that already
 * bought this product" is on and cannot be turned off, a card run's cooldown
 * covers every order since the run began, and the run is sent as retry_of.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job, JobDetail, RetryPlan } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  getSettings: vi.fn(),
  getJob: vi.fn(),
  getRetryPlan: vi.fn(),
  checkRun: vi.fn(),
  previewOrderCards: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  startOrderJob: mocks.startOrderJob,
  previewProduct: mocks.previewProduct,
  getSettings: mocks.getSettings,
  getJob: mocks.getJob,
  getRetryPlan: mocks.getRetryPlan,
  checkRun: mocks.checkRun,
  previewOrderCards: mocks.previewOrderCards,
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

const LINK = "https://www.flipkart.com/vivo-t5-lite-5g/p/itm5420c578ec2df?pid=MOBHQSFF6MZK4YNP";

const startedJob: Job = {
  id: 900,
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
  started_at: "2026-11-24T14:00:00Z",
  finished_at: null,
};

function run820(payload: Record<string, unknown> = {}): JobDetail {
  return {
    ...startedJob,
    id: 820,
    status: "completed",
    payload: { product_link: LINK, payment_mode: "cod", target: 16500, card_cooldown_days: null, ...payload },
    items: [],
  };
}

function plan(overrides: Partial<RetryPlan> = {}): RetryPlan {
  return {
    job_id: 820,
    product_link: LINK,
    payment_mode: "cod",
    accounts: [
      { account_id: 34, email: "dad875@catbar.net", status: "failed", message: "delivery is 5 days out, limit is 4" },
      { account_id: 72, email: "gst@catbar.net", status: "skipped", message: "not started — the server was stopping" },
    ],
    placed: 188,
    waiting: 9,
    passed_over: 0,
    since: "2026-11-24T08:44:00Z",
    card_cooldown_days: null,
    units_wanted: null,
    units_bought: 0,
    units_missing: null,
    ...overrides,
  };
}

beforeEach(() => {
  mocks.previewProduct.mockReturnValue(new Promise(() => undefined));
  mocks.startOrderJob.mockResolvedValue(startedJob);
  mocks.getSettings.mockResolvedValue({ smart_id_default_on: false });
  mocks.checkRun.mockReturnValue(new Promise(() => undefined));
  mocks.previewOrderCards.mockReturnValue(new Promise(() => undefined));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderForm(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <OrderForm
        accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 300, truncated: false }}
        runActive={false}
        onStarted={vi.fn()}
      />
    </MemoryRouter>,
  );
}

async function submit(): Promise<void> {
  const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false), { timeout: 5000 });
  fireEvent.click(button);
  await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalled(), { timeout: 5000 });
}

describe("Retry failed", () => {
  it("ticks only the run's accounts that got no order and sends the run as a retry", async () => {
    mocks.getJob.mockResolvedValue(run820());
    mocks.getRetryPlan.mockResolvedValue(plan());
    renderForm("/ordering?retry=820");

    expect(await screen.findByText(/Retrying run #820: 2 accounts that got no order are ticked/)).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Retry failed of run #820" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText(/Left out: 188 with an order placed, 9 waiting for confirmation/)).toBeTruthy();
    expect(screen.getByText("9 orders still waiting for confirmation")).toBeTruthy();
    expect(screen.getByText(/delivery is 5 days out, limit is 4/)).toBeTruthy();
    const skip = screen.getByRole("checkbox", { name: /Skip IDs that already bought this product/ }) as HTMLInputElement;
    expect(skip.checked).toBe(true);
    expect(skip.disabled).toBe(true);

    await submit();
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.account_ids).toEqual([34, 72]);
    expect(sent.retry_of).toBe(820);
    expect(sent.skip_bought_product).toBe(true);
    expect(sent.product_link).toBe(LINK);
    expect(sent.target).toBe(16500);
  });

  it("is sent once: afterwards nothing is ticked and the retry tab is gone", async () => {
    mocks.getJob.mockResolvedValue(run820());
    mocks.getRetryPlan.mockResolvedValue(plan());
    renderForm("/ordering?retry=820");
    await screen.findByText(/Retrying run #820/);
    await submit();
    await waitFor(() => expect(screen.queryByRole("tab", { name: /Retry failed of run/ })).toBeNull(), { timeout: 5000 });
    expect((screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("choosing All Email IDs leaves the retry: no retry_of is sent", async () => {
    mocks.getJob.mockResolvedValue(run820());
    mocks.getRetryPlan.mockResolvedValue(plan());
    renderForm("/ordering?retry=820");
    await screen.findByText(/Retrying run #820/);
    fireEvent.click(screen.getByRole("tab", { name: "All Email IDs" }));
    expect(screen.queryByText(/Left out: 188/)).toBeNull();
    await submit();
    expect(mocks.startOrderJob.mock.calls[0][0].retry_of).toBeNull();
  });

  it("a card retry raises the card cooldown to cover the run and previews with it", async () => {
    mocks.getJob.mockResolvedValue(run820({ payment_mode: "card", card_cooldown_days: 1 }));
    mocks.getRetryPlan.mockResolvedValue(plan({ payment_mode: "card", card_cooldown_days: 2 }));
    renderForm("/ordering?retry=820");
    expect(await screen.findByText(/Upload the card sheet again: card details are never kept/)).toBeTruthy();
    expect(screen.getByText(/no card that paid for an order since run #820 began is used/)).toBeTruthy();

    const sheet = new File(["Card Number,Exp Month,Exp Year,CVV"], "hdfc.csv", { type: "text/csv" });
    const input = document.querySelector('input[name="cards_file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [sheet] } });
    await waitFor(() => expect(mocks.previewOrderCards).toHaveBeenCalled());
    const asked = mocks.previewOrderCards.mock.calls[mocks.previewOrderCards.mock.calls.length - 1][0];
    expect(asked).toMatchObject({ account_ids: [34, 72], card_cooldown_days: 2, retry_of: 820 });

    await submit();
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.card_cooldown_days).toBe(2);
    expect(sent.cards_file).toBe(sheet);
    expect(sent.retry_of).toBe(820);
  });

  it("a quantity run retries only the units still missing", async () => {
    mocks.getJob.mockResolvedValue(run820({ total_quantity: 300, qty_per_order: 1 }));
    mocks.getRetryPlan.mockResolvedValue(plan({ units_wanted: 300, units_bought: 197, units_missing: 103 }));
    renderForm("/ordering?retry=820");
    expect(await screen.findByText(/asked for 300 units and got 197; Total Quantity is set to the 103 still missing/)).toBeTruthy();
    await submit();
    expect(mocks.startOrderJob.mock.calls[0][0].total_quantity).toBe(103);
  });

  it("says so when there is nothing to retry, and sends nothing", async () => {
    mocks.getJob.mockResolvedValue(run820());
    mocks.getRetryPlan.mockResolvedValue(plan({ accounts: [], placed: 290, waiting: 2 }));
    renderForm("/ordering?retry=820");
    expect(await screen.findByText("Nothing to retry in run #820")).toBeTruthy();
    expect(screen.getByText("Run #820 has no account left to retry.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("a run that cannot be retried leaves the form as it was and says why", async () => {
    mocks.getJob.mockResolvedValue(run820());
    mocks.getRetryPlan.mockRejectedValue(new Error("Run #820 is still going; retry it once it has finished."));
    renderForm("/ordering?retry=820");
    expect(await screen.findByText(/Run #820 cannot be retried: Run #820 is still going/)).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /Retry failed of run/ })).toBeNull();
  });
});
