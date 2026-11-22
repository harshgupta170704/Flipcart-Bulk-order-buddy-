// @vitest-environment jsdom
/**
 * The Place orders form's Real / Mock switch and Quantity mode (step A2).
 * Mock is chosen per run and sent as `mode: "mock"`; the next run is real
 * again. Upto is the default; Minimum is sent as `qty_mode: "minimum"`.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job, ProductPreview } from "../lib/types";

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

const LINK = "https://www.flipkart.com/pigeon-special-3-l-inner-lid-pressure-cooker/p/itm0c2cd3b721e22?pid=PRCFNFEBWFFDBVHP";

/** The pigeon cooker as the preview read it on 26 Oct 2026: three sellers. */
const PREVIEW: ProductPreview = {
  url: LINK,
  name: "Pigeon Special 3 L Inner Lid Pressure Cooker",
  brand: "Pigeon",
  category: "Pressure Cookers",
  color: null,
  image: null,
  sku: null,
  price: 749,
  currency: "INR",
  availability: null,
  in_stock: true,
  rating: 4.1,
  rating_count: 75149,
  seller: null,
  error: null,
  default_seller: "MYTHANGLORYRetail",
  sellers: [
    { name: "MYTHANGLORYRetail", price: 749, rating: 4.3, cod: true, buy_now: true },
    { name: "SAPHEDretail", price: 749, rating: 4.2, cod: true, buy_now: true },
    { name: "TBL Online", price: 749, rating: 4.4, cod: true, buy_now: true },
  ],
  sellers_error: null,
};

const startedJob: Job = {
  id: 52,
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
  started_at: "2026-10-26T09:30:00Z",
  finished_at: null,
};

beforeEach(() => {
  mocks.previewProduct.mockResolvedValue(PREVIEW);
  mocks.startOrderJob.mockResolvedValue(startedJob);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function renderForm() {
  render(
    <MemoryRouter>
      <OrderForm
        accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 2, truncated: false }}
        runActive={false}
        onStarted={vi.fn()}
      />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: LINK } });
  fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });
  fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "18250" } });
  await screen.findByRole("list", { name: "Sellers of this product" }, { timeout: 3000 });
}

function start(name: RegExp) {
  fireEvent.click(screen.getByRole("button", { name }));
}

describe("Real / Mock", () => {
  it("starts as Real and sends a real run", async () => {
    await renderForm();
    expect(screen.getByRole("radio", { name: "Real" }).getAttribute("aria-checked")).toBe("true");
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].mode).toBe("real");
  });

  it("sends a mock run, says nothing is bought, and is Real again afterwards", async () => {
    await renderForm();
    fireEvent.click(screen.getByRole("radio", { name: "Mock" }));
    expect(screen.getByText("Mock run: nothing is bought")).toBeTruthy();
    start(/Submit Mock Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].mode).toBe("mock");
    expect(mocks.startOrderJob.mock.calls[0][0].bank_step_by_hand).toBe(false);
    await waitFor(() =>
      expect(screen.getByRole("radio", { name: "Real" }).getAttribute("aria-checked")).toBe("true"),
    );
  });

  it("hides the Payment section, bank step included, in a mock run", async () => {
    await renderForm();
    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "card" } });
    expect(screen.getByLabelText(/^Authentication/)).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "Mock" }));
    // Mock pays nothing: the whole Payment section, bank step included, is gone.
    expect(screen.queryByLabelText(/^Authentication/)).toBeNull();
    expect(screen.queryByLabelText(/Payment mode/)).toBeNull();
  });
});

describe("Quantity mode", () => {
  it("defaults to Upto", async () => {
    await renderForm();
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].qty_mode).toBe("upto");
  });

  it("sends Minimum when chosen", async () => {
    await renderForm();
    fireEvent.change(screen.getByLabelText(/^Mode/), { target: { value: "minimum" } });
    expect(screen.getByText(/that order is refused before payment/)).toBeTruthy();
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].qty_mode).toBe("minimum");
  });
});

describe("Order preview, GST mandatory, phones, reset (step A3)", () => {
  it("shows the run as it stands, beside the form", async () => {
    await renderForm();
    const panel = screen.getByRole("complementary", { name: "Order preview" });
    expect(panel.textContent).toContain("Real");
    expect(panel.textContent).toContain("Cash on Delivery");
    expect(panel.textContent).toContain("₹18,250");
    fireEvent.click(screen.getByRole("radio", { name: "Mock" }));
    expect(panel.textContent).toContain("Mock");
  });

  it("sends GST mandatory and the delivery phones", async () => {
    await renderForm();
    fireEvent.click(screen.getByLabelText(/GST mandatory/));
    fireEvent.change(screen.getByLabelText(/Delivery phone numbers/), {
      target: { value: "9000000001, 9000000002 9000000003" },
    });
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.gst_mandatory).toBe(true);
    expect(sent.address_phones).toEqual(["9000000001", "9000000002", "9000000003"]);
  });

  it("refuses a phone that is not 10 digits and sends nothing", async () => {
    await renderForm();
    fireEvent.change(screen.getByLabelText(/Delivery phone numbers/), { target: { value: "9000000001 +919000000002" } });
    expect(screen.getByText(/must be 10 digits.*\+919000000002/)).toBeTruthy();
    start(/Submit Order/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });

  it("Reset to defaults empties the form", async () => {
    await renderForm();
    fireEvent.click(screen.getByRole("radio", { name: "Mock" }));
    fireEvent.click(screen.getByLabelText(/GST mandatory/));
    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));
    expect((screen.getByLabelText(/Product URL/) as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText(/GST mandatory/) as HTMLInputElement).checked).toBe(false);
    expect(screen.getByRole("radio", { name: "Real" }).getAttribute("aria-checked")).toBe("true");
  });
});

describe("Account selection (step B)", () => {
  it("sends the exclusion window and smart selection", async () => {
    await renderForm();
    fireEvent.click(screen.getByLabelText(/^Exclude IDs$/));
    fireEvent.change(screen.getByLabelText(/Exclude IDs ordered/), { target: { value: "14" } });
    fireEvent.click(screen.getByLabelText(/Smart ID Selection/));
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.exclude_ordered_days).toBe(14);
    expect(sent.smart_selection).toBe(true);
  });

  it("leaves both off by default", async () => {
    await renderForm();
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.exclude_ordered_days).toBeNull();
    expect(sent.smart_selection).toBe(false);
  });

  it("refuses a window outside 1–365 days", async () => {
    await renderForm();
    fireEvent.click(screen.getByLabelText(/^Exclude IDs$/));
    fireEvent.change(screen.getByLabelText(/Exclude IDs ordered/), { target: { value: "500" } });
    expect(screen.getByText(/from 1 to 365/)).toBeTruthy();
    start(/Submit Order/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });
});

describe("Card filters (step C)", () => {
  it("are shown for a card run only", async () => {
    await renderForm();
    expect(screen.queryByText("Card filters")).toBeNull();
    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "card" } });
    expect(screen.getByText("Card filters")).toBeTruthy();
  });

  it("refuse a value out of range", async () => {
    await renderForm();
    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "card" } });
    fireEvent.change(screen.getByLabelText(/Max orders per card/), { target: { value: "0" } });
    expect(screen.getByText(/Each card filter is a whole number/)).toBeTruthy();
  });
});

describe("AI mode (step E)", () => {
  it("shows the pacing controls and sends them", async () => {
    await renderForm();
    fireEvent.click(screen.getByRole("radio", { name: "AI" }));
    expect(screen.getByText("AI pacing")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Minimum attempts per interval/), { target: { value: "5" } });
    fireEvent.click(screen.getByLabelText(/Rehearsal/));
    start(/Submit AI Rehearsal/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.mode).toBe("ai");
    expect(sent.ai).toEqual({ max_interval_minutes: 1, min_attempts: 5, scale_up_pct: 20, run_duration: "12h", rehearsal: true });
  });

  it("refuses an unreadable run duration", async () => {
    await renderForm();
    fireEvent.click(screen.getByRole("radio", { name: "AI" }));
    fireEvent.change(screen.getByLabelText(/Run duration/), { target: { value: "soon" } });
    start(/Submit AI Order/);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
    expect(screen.getByText(/like 12h, 90m or 2d/)).toBeTruthy();
  });

  it("hides Total Attempts in AI mode and sends price tracker + fixed count options", async () => {
    await renderForm();
    fireEvent.click(screen.getByRole("radio", { name: "AI" }));
    expect(screen.queryByLabelText(/Total Attempts/)).toBeNull();
    fireEvent.change(screen.getByLabelText(/Scale-Up Threshold Mode/), { target: { value: "count" } });
    fireEvent.change(screen.getByLabelText(/Scale-Up Threshold \(orders\)/), { target: { value: "3" } });
    fireEvent.click(screen.getByLabelText(/Price tracker mode/));
    start(/Submit AI Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.mode).toBe("ai");
    expect(sent.ai).toEqual({
      max_interval_minutes: 1,
      min_attempts: 10,
      scale_up_pct: 20,
      scale_up_mode: "count",
      scale_up_count: 3,
      run_duration: "12h",
      single_probe: true,
      recycle_accounts: true,
      rehearsal: false,
    });
  });
});

describe("Mock without payment, and Ref Code", () => {
  it("starts a mock run with no card sheet and sends the Ref Code", async () => {
    await renderForm();
    fireEvent.click(screen.getByRole("radio", { name: "Mock" }));
    fireEvent.change(screen.getByLabelText(/^Ref Code/), { target: { value: "PO-77" } });
    start(/Submit Mock Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.mode).toBe("mock");
    expect(sent.cards_file).toBeNull();
    expect(sent.ref_code).toBe("PO-77");
  });
});

describe("The rest of the video's form", () => {
  it("adds product URLs and keeps the divisibility rule", async () => {
    await renderForm();
    fireEvent.click(screen.getByRole("button", { name: "+ Add product URL" }));
    fireEvent.change(screen.getByLabelText(/Additional product URL 2/), {
      target: { value: "https://www.flipkart.com/p/second" },
    });
    fireEvent.change(screen.getByLabelText(/^Quantity Per Order/), { target: { value: "3" } });
    start(/Submit Order/);
    expect(await screen.findByText(/divisible by the number of product URLs\./, { selector: "[role=alert]" })).toBeTruthy();
    expect(mocks.startOrderJob).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText(/^Quantity Per Order/), { target: { value: "2" } });
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].extra_product_links).toEqual(["https://www.flipkart.com/p/second"]);
  });

  it("offers only the payment modes mapped live, sends coupons, and hides the options no step uses", async () => {
    await renderForm();
    const options = [...(screen.getByLabelText(/Payment mode/) as HTMLSelectElement).options].map((o) => o.text);
    // COD + Card, RTGS, IMPS and Gift Voucher only wait until they are mapped live.
    expect(options).toEqual(["Card", "Cash on Delivery", "GV + card"]);
    fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });
    // Saved on the run but used by no ordering step: hidden (audit of 19 Nov 2026).
    expect(screen.queryByLabelText(/^Flipkart Minutes$/)).toBeNull();
    expect(screen.queryByLabelText(/^Apply Additional Offer$/)).toBeNull();
    expect(screen.queryByLabelText(/^Reward IDs$/)).toBeNull();
    expect(screen.queryByRole("option", { name: /Higher MCC/ })).toBeNull();
    fireEvent.click(screen.getByLabelText(/^Coupon Codes$/, { selector: "input[type=checkbox]" }));
    fireEvent.change(screen.getByLabelText(/^Coupon Codes/, { selector: "input[name=coupon_codes]" }), {
      target: { value: "SAVE10, NEW50" },
    });
    start(/Submit Order/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.payment_mode).toBe("cod");
    expect(sent.cards_file).toBeNull();
    expect(sent.flipkart_minutes).toBe(false);
    expect(sent.max_delivery_days).toBe(3);
    expect(sent.coupon_codes).toEqual(["SAVE10", "NEW50"]);
  });
});
