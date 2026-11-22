// @vitest-environment jsdom
/**
 * The Place orders form's "Search Flipkart": picking a result fills the
 * Product link, and the product preview loads from it as from a pasted link —
 * without the search or the pick ever submitting the form.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  searchProducts: vi.fn(),
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


const LINK = "https://www.flipkart.com/vivo-t5-lite-44w-5g-twilight-shadow-64-gb/p/itm5420c578ec2df?pid=MOBHQSFF6MZK4YNP";

describe("OrderForm product search", () => {
  it("fills the product link with the picked result, and the preview follows", async () => {
    mocks.searchProducts.mockResolvedValue({
      query: "vivo t5 lite",
      results: [{ title: "vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)", link: LINK, pid: "MOBHQSFF6MZK4YNP", price: 17999, rating: 4.3, image: null }],
      error: null,
    });
    const { container } = render(
      <MemoryRouter>
        <OrderForm
          accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 2, truncated: false }}
          runActive={false}
          onStarted={vi.fn()}
        />
      </MemoryRouter>,
    );
    // The search sits inside the Place orders form, and none of its buttons may
    // submit that form. Watched on the form's own submit event: this form has
    // no price cap and no card sheet, so its submit stops before starting a
    // run, and a startOrderJob check alone could never fail.
    const forms = container.querySelectorAll("form");
    expect(forms).toHaveLength(1);
    const submitted = vi.fn();
    forms[0].addEventListener("submit", submitted);

    fireEvent.click(screen.getByRole("tab", { name: "Search" }));
    fireEvent.change(screen.getByLabelText("Search Flipkart"), { target: { value: "vivo t5 lite" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(submitted).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByText("vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)"));
    expect(submitted).not.toHaveBeenCalled();

    expect((screen.getByLabelText(/Product URL/) as HTMLInputElement).value).toBe(LINK);
    await waitFor(() => expect(mocks.previewProduct).toHaveBeenCalledWith(LINK), { timeout: 3000 });
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });
});
