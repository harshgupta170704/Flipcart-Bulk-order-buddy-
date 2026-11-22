// @vitest-environment jsdom
/**
 * The Place orders form's Seller field (wave 2, package A). The list offers
 * the sellers the preview read; any name can be typed; blank is Flipkart's
 * default seller and sends nothing. A name the preview does not list, or a
 * seller offering only Add to cart, is flagged under the field but never
 * blocks the run — the run checks the seller on Flipkart's own page.
 *
 * The children that load data of their own are stubbed, as in OrderForm.test.tsx.
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

async function renderForm(preview: ProductPreview = PREVIEW) {
  mocks.previewProduct.mockResolvedValue(preview);
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
  // The preview has landed once its list of sellers shows.
  await screen.findByRole("list", { name: "Sellers of this product" }, { timeout: 3000 });
}

function sellerInput(): HTMLInputElement {
  // Exactly "Seller": the preview's list is labelled "Sellers of this product".
  return screen.getByLabelText("Seller") as HTMLInputElement;
}

async function submit(): Promise<void> {
  const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
}

describe("OrderForm seller", () => {
  it("offers the preview's sellers in the Seller list and names the default", async () => {
    await renderForm();

    const list = document.getElementById(sellerInput().getAttribute("list") ?? "");
    expect(list?.tagName).toBe("DATALIST");
    const options = [...(list?.querySelectorAll("option") ?? [])];
    expect(options.map((option) => option.value)).toEqual(["MYTHANGLORYRetail", "SAPHEDretail", "TBL Online"]);
    expect(options[1].getAttribute("label")).toBe("₹749.00 · rated 4.2 · Cash on Delivery");
    expect(sellerInput().placeholder).toBe("MYTHANGLORYRetail (Default)");
  });

  it("sends a chosen seller as seller", async () => {
    await renderForm();
    fireEvent.change(sellerInput(), { target: { value: "  SAPHEDretail " } });

    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0]).toMatchObject({ seller: "SAPHEDretail", target: 18250 });
  });

  it("leaves the seller to Flipkart when the box is blank", async () => {
    await renderForm();

    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].seller).toBeNull();
  });

  it("flags a typed seller the product does not list, and still lets the run decide", async () => {
    await renderForm();
    fireEvent.change(sellerInput(), { target: { value: "Nobody Retail" } });

    expect(screen.getByText(/“Nobody Retail” is not among the sellers listed for this product/)).toBeTruthy();
    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].seller).toBe("Nobody Retail");
  });

  it("finds a listed seller typed in another case", async () => {
    await renderForm();
    fireEvent.change(sellerInput(), { target: { value: "tbl online" } });

    expect(screen.queryByText(/is not among the sellers listed/)).toBeNull();
  });

  it("flags a seller offering only Add to cart", async () => {
    const sellers = PREVIEW.sellers?.map((offer) =>
      offer.name === "TBL Online" ? { ...offer, buy_now: false } : offer,
    );
    await renderForm({ ...PREVIEW, sellers });
    fireEvent.change(sellerInput(), { target: { value: "TBL Online" } });

    expect(screen.getByText(/TBL Online offers only Add to cart\. Choosing a seller works only for products with Buy now for now/)).toBeTruthy();
  });

  it("holds the box to 100 characters and refuses a longer name on the field", async () => {
    await renderForm();
    expect(sellerInput().maxLength).toBe(100);
    // A change event sets the value past maxLength, as a script could.
    fireEvent.change(sellerInput(), { target: { value: "S".repeat(101) } });

    await submit();

    expect(await screen.findByText("A seller's name is at most 100 characters.")).toBeTruthy();
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });
});
