// @vitest-environment jsdom
/**
 * Smart Bulk Order with explicit ID ticks (owner, 24 Nov 2026): the form asks
 * its picker for explicit ticks, and a form with every ID unticked is never
 * sent — an empty id list reads as "every account" on the server. Also: GST
 * mandatory is on by default and warns when the run has no GST at all.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccountSelection, ProductPreview } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  pickerProps: vi.fn(),
  selection: { current: { mode: "ids", ids: [] } as AccountSelection },
  gst: { current: null as null | { id: number; gstin: string } },
}));

vi.mock("../lib/api", () => ({
  startOrderJob: mocks.startOrderJob,
  previewProduct: mocks.previewProduct,
  listCardFailureCodes: () => Promise.resolve([]),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  // Smart ID Selection: the form reads where its switch starts and asks the
  // room check as it changes; neither is what these tests are about.
  getSettings: () => Promise.resolve({ smart_id_default_on: true }),
  checkRun: () => new Promise(() => undefined),
}));

vi.mock("./RunAccounts", async () => {
  const { useEffect } = await import("react");
  return {
    RunAccountPicker: (props: { onSelectionChange?: (next: AccountSelection) => void; explicitTicks?: boolean }) => {
      mocks.pickerProps(props);
      const { onSelectionChange } = props;
      useEffect(() => onSelectionChange?.(mocks.selection.current), [onSelectionChange]);
      return null;
    },
  };
});

vi.mock("./AddressGstPicker", async () => {
  const { useEffect } = await import("react");
  return {
    AddressGstPicker: ({
      onChange,
      onResolved,
    }: {
      onChange: (next: { address_id: number; gst_id: null }) => void;
      onResolved?: (address: unknown, gst: unknown) => void;
    }) => {
      useEffect(() => onChange({ address_id: 1, gst_id: null }), [onChange]);
      useEffect(() => onResolved?.({ id: 1, name: "Ayush" }, mocks.gst.current), [onResolved]);
      return null;
    },
  };
});

vi.mock("./LimitUsageSummary", () => ({ LimitUsageSummary: () => null }));

import { OrderForm } from "./OrderForm";

const LINK = "https://www.flipkart.com/pigeon-special-3-l-inner-lid-pressure-cooker/p/itm0c2cd3b721e22?pid=PRCFNFEBWFFDBVHP";

const PREVIEW = {
  url: LINK,
  name: "Pigeon Special 3 L Inner Lid Pressure Cooker",
  brand: "Pigeon",
  category: "Pressure Cookers",
  price: 749,
  in_stock: true,
  error: null,
  default_seller: null,
  sellers: [],
  sellers_error: null,
} as unknown as ProductPreview;

beforeEach(() => {
  mocks.previewProduct.mockResolvedValue(PREVIEW);
  mocks.startOrderJob.mockResolvedValue({ id: 9, type: "order", status: "queued" });
  mocks.selection.current = { mode: "ids", ids: [] };
  mocks.gst.current = { id: 3, gstin: "09ABACA7232Q1Z9" };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function renderForm() {
  render(
    <MemoryRouter>
      <OrderForm
        accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 302, truncated: false }}
        runActive={false}
        onStarted={vi.fn()}
      />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: LINK } });
  fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });
  fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "18250" } });
}

describe("Smart Bulk Order: ticks are what runs", () => {
  it("asks the picker for explicit ticks", async () => {
    await renderForm();
    expect(mocks.pickerProps).toHaveBeenCalledWith(expect.objectContaining({ explicitTicks: true }));
  });

  it("with every ID unticked, Submit is off, says why, and nothing is sent", async () => {
    await renderForm();
    const submit = screen.getByRole("button", { name: /Submit Order/ }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(screen.getByText(/No email ID ticked — tick some, or Select all/)).toBeTruthy();
    // Not the "no account has a valid session" alert: accounts exist, none is ticked.
    expect(screen.queryByText(/No account has a valid session/)).toBeNull();

    // Even a submit that bypasses the button (Enter in a field) sends nothing.
    fireEvent.submit(document.querySelector("form") as HTMLFormElement);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });

  it("all IDs of a domain go out as the domain filter", async () => {
    mocks.selection.current = {
      mode: "filter",
      filter: { statuses: ["session_valid"], domains: ["catbar.net"] },
      count: 302,
    };
    await renderForm();
    fireEvent.click(screen.getByRole("button", { name: /Submit Order/ }));
    await vi.waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.account_filter).toEqual({ statuses: ["session_valid"], domains: ["catbar.net"] });
    expect(sent.account_ids).toBeNull();
    expect(sent.gst_mandatory).toBe(true);
  });
});

describe("GST mandatory by default", () => {
  it("warns when GST mandatory is on and the run has no GST", async () => {
    mocks.gst.current = null;
    mocks.selection.current = { mode: "ids", ids: [7] };
    await renderForm();
    expect(screen.getByText("GST mandatory is on, but this run has no GST")).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/GST mandatory/));
    expect(screen.queryByText("GST mandatory is on, but this run has no GST")).toBeNull();
  });

  it("says nothing when the run has a GST", async () => {
    mocks.selection.current = { mode: "ids", ids: [7] };
    await renderForm();
    expect((screen.getByLabelText(/GST mandatory/) as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByText("GST mandatory is on, but this run has no GST")).toBeNull();
  });
});
