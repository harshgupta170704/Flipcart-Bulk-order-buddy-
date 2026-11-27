// @vitest-environment jsdom
/**
 * The order form's Smart ID Selection switch (Smart ID, part 1). It shows on
 * until the setting "Smart ID Selection starts on" has loaded, and stays on
 * when it cannot be read; it then starts from the setting, goes back to it
 * after each Submit and on Reset to defaults, and "Run again" restores it from
 * the earlier run (or takes the setting for a run from before the switch).
 * It is sent on every order, on or off. The old box of the same name is now
 * "Spread orders across accounts", still sent as smart_selection.
 *
 * The children that load data of their own are stubbed, as in
 * OrderForm.test.tsx; the unit-limit summary is a stub that records what the
 * form hands it.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Job, JobDetail } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  getSettings: vi.fn(),
  getJob: vi.fn(),
  checkRun: vi.fn(),
  summaryProps: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  startOrderJob: mocks.startOrderJob,
  previewProduct: mocks.previewProduct,
  getSettings: mocks.getSettings,
  getJob: mocks.getJob,
  checkRun: mocks.checkRun,
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

vi.mock("./LimitUsageSummary", () => ({
  LimitUsageSummary: (props: unknown) => {
    mocks.summaryProps(props);
    return null;
  },
}));

import { OrderForm } from "./OrderForm";

const LINK = "https://www.flipkart.com/vivo-t5-lite-5g/p/itm5420c578ec2df?pid=MOBHQSFF6MZK4YNP";

const startedJob: Job = {
  id: 90,
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
  started_at: "2026-11-24T08:00:00Z",
  finished_at: null,
};

/** An earlier run, as GET /jobs/{id} returns it for "Run again". */
function earlierRun(payload: Record<string, unknown>): JobDetail {
  return {
    ...startedJob,
    id: 31,
    status: "completed",
    payload: { product_link: LINK, payment_mode: "cod", target: 18250, ...payload },
    items: [],
  };
}

/** A setting that answers only when the test says so. */
function settingLater(): (smartIdDefaultOn: boolean) => Promise<void> {
  let answer: (value: unknown) => void = () => undefined;
  mocks.getSettings.mockReturnValue(
    new Promise((resolve) => {
      answer = resolve;
    }),
  );
  return async (smartIdDefaultOn) => {
    await act(async () => answer({ smart_id_default_on: smartIdDefaultOn }));
  };
}

beforeEach(() => {
  // The product preview is not what is under test: it never answers.
  mocks.previewProduct.mockReturnValue(new Promise(() => undefined));
  mocks.startOrderJob.mockResolvedValue(startedJob);
  mocks.getSettings.mockResolvedValue({ smart_id_default_on: true });
  // The room check is looked at, not answered.
  mocks.checkRun.mockReturnValue(new Promise(() => undefined));
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

/** Product, Cash on Delivery and a cap: all a run needs to be sent. */
function fillIn() {
  fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: LINK } });
  fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });
  fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "18250" } });
}

function smartIdBox(): HTMLInputElement {
  return screen.getByLabelText("Smart ID Selection") as HTMLInputElement;
}

function spreadBox(): HTMLInputElement {
  return screen.getByLabelText("Spread orders across accounts") as HTMLInputElement;
}

async function submit(): Promise<void> {
  const button = screen.getByRole("button", { name: "Submit Order" }) as HTMLButtonElement;
  // Enabled once the stubs have handed over the accounts and the address.
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
}

/** What the Order preview shows on one row ("Smart ID:" → "Enabled"). */
function previewRow(label: string): string | null | undefined {
  const panel = screen.getByRole("complementary", { name: "Order preview" });
  return within(panel).getByText(`${label}:`).nextElementSibling?.textContent;
}

/** What the form last handed the unit-limit summary. */
function summary(): Record<string, unknown> {
  const calls = mocks.summaryProps.mock.calls;
  return calls[calls.length - 1][0] as Record<string, unknown>;
}

/** The last room check the form asked. */
function lastCheck(): Record<string, unknown> {
  const calls = mocks.checkRun.mock.calls;
  return calls[calls.length - 1][0] as Record<string, unknown>;
}

describe("Smart ID Selection: where the switch starts", () => {
  it("shows on until the setting loads, then takes it, and goes back to it after Submit", async () => {
    const setting = settingLater();
    renderForm();
    expect(smartIdBox().checked).toBe(true);
    expect(smartIdBox().name).toBe("smart_id");

    await setting(false);
    expect(smartIdBox().checked).toBe(false);

    // On for this one order.
    fireEvent.click(smartIdBox());
    expect(smartIdBox().checked).toBe(true);
    fillIn();
    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(mocks.startOrderJob.mock.calls[0][0].smart_id).toBe(true);
    // The next order starts from the setting again, like Mock and No cap.
    await waitFor(() => expect(smartIdBox().checked).toBe(false));
  });

  it("stays on when the setting cannot be read, and is sent on", async () => {
    mocks.getSettings.mockRejectedValue(new Error("Not signed in"));
    renderForm();
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalled());
    fillIn();
    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(smartIdBox().checked).toBe(true);
    expect(mocks.startOrderJob.mock.calls[0][0].smart_id).toBe(true);
  });

  it("is on for a server that sends no value", async () => {
    mocks.getSettings.mockResolvedValue({ max_concurrency: 2 });
    renderForm();
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalled());
    await act(async () => undefined);
    expect(smartIdBox().checked).toBe(true);
  });

  it("keeps a person's choice when the setting arrives after it", async () => {
    const setting = settingLater();
    renderForm();
    fireEvent.click(smartIdBox());
    expect(smartIdBox().checked).toBe(false);

    await setting(true);
    expect(smartIdBox().checked).toBe(false);
  });

  it("sends smart_id false when switched off", async () => {
    renderForm();
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalled());
    fireEvent.click(smartIdBox());
    fillIn();
    await submit();

    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    const sent = mocks.startOrderJob.mock.calls[0][0];
    expect(sent.smart_id).toBe(false);
    // The renamed box is a different switch, left as it was.
    expect(sent.smart_selection).toBe(false);
  });

  it("goes back to the setting on Reset to defaults", async () => {
    mocks.getSettings.mockResolvedValue({ smart_id_default_on: false });
    renderForm();
    await waitFor(() => expect(smartIdBox().checked).toBe(false));
    fireEvent.click(smartIdBox());
    fireEvent.click(spreadBox());
    expect(smartIdBox().checked).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));

    expect(smartIdBox().checked).toBe(false);
    expect(spreadBox().checked).toBe(false);
  });

  it("goes back to an 'on' setting on Reset to defaults", async () => {
    renderForm();
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalled());
    fireEvent.click(smartIdBox());
    expect(smartIdBox().checked).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));

    expect(smartIdBox().checked).toBe(true);
  });
});

describe("Smart ID Selection on Run again (?from=)", () => {
  it("restores off from a run that had it off", async () => {
    mocks.getJob.mockResolvedValue(earlierRun({ smart_id: false }));
    renderForm("/ordering?from=31");
    expect(await screen.findByText(/Filled in from run #31/)).toBeTruthy();
    // The setting (on) loading as well does not undo the run's own value.
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalled());
    await act(async () => undefined);
    expect(smartIdBox().checked).toBe(false);
    expect(mocks.getJob).toHaveBeenCalledWith(31);
  });

  it("restores on from a run that had it on, over an 'off' setting that loads later", async () => {
    const setting = settingLater();
    mocks.getJob.mockResolvedValue(earlierRun({ smart_id: true, smart_selection: true }));
    renderForm("/ordering?from=31");
    expect(await screen.findByText(/Filled in from run #31/)).toBeTruthy();

    await setting(false);

    expect(smartIdBox().checked).toBe(true);
    // The renamed switch comes back from its own saved key.
    expect(spreadBox().checked).toBe(true);
  });

  it("takes the setting for a run from before the switch, even when the setting loads after the run", async () => {
    const setting = settingLater();
    mocks.getJob.mockResolvedValue(earlierRun({}));
    renderForm("/ordering?from=31");
    expect(await screen.findByText(/Filled in from run #31/)).toBeTruthy();
    expect(smartIdBox().checked).toBe(true);

    await setting(false);

    expect(smartIdBox().checked).toBe(false);
  });

  it("takes an 'off' setting that loaded first, for a run from before the switch", async () => {
    mocks.getSettings.mockResolvedValue({ smart_id_default_on: false });
    let answerJob: (job: JobDetail) => void = () => undefined;
    mocks.getJob.mockReturnValue(
      new Promise((resolve) => {
        answerJob = resolve;
      }),
    );
    renderForm("/ordering?from=31");
    await waitFor(() => expect(smartIdBox().checked).toBe(false));

    await act(async () => answerJob(earlierRun({})));

    expect(await screen.findByText(/Filled in from run #31/)).toBeTruthy();
    expect(smartIdBox().checked).toBe(false);
  });
});

describe("Smart ID Selection in the Order preview and the room check", () => {
  it("shows both switches on their own rows", async () => {
    renderForm();
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalled());
    expect(previewRow("Smart ID")).toBe("Enabled");
    expect(previewRow("Spread across accounts")).toBe("Off");

    fireEvent.click(smartIdBox());
    fireEvent.click(spreadBox());

    expect(previewRow("Smart ID")).toBe("Disabled");
    expect(previewRow("Spread across accounts")).toBe("On");
  });

  it("hands the summary the switch, and asks the room check with the order's size, mode and Exclude IDs window", async () => {
    renderForm();
    await waitFor(() => expect(mocks.getSettings).toHaveBeenCalled());
    expect(summary()).toMatchObject({ smartId: true, aiRun: false, requestedUnits: 2 });

    // The link's pid alone is worth asking about: a variant limit can apply.
    fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: LINK } });
    await waitFor(() => expect(mocks.checkRun).toHaveBeenCalled(), { timeout: 2000 });
    expect(lastCheck()).toEqual({
      account_ids: [7, 8],
      variant: "MOBHQSFF6MZK4YNP",
      qty: 1,
      qty_mode: "upto",
      total_quantity: null,
      exclude_ordered_days: null,
    });

    fireEvent.click(screen.getByLabelText(/^Exclude IDs$/));
    fireEvent.change(screen.getByLabelText(/^Quantity Per Order/), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText(/^Mode/), { target: { value: "minimum" } });
    await waitFor(() => expect(lastCheck()).toMatchObject({ exclude_ordered_days: 7, qty: 2, qty_mode: "minimum" }), {
      timeout: 2000,
    });
    expect(summary()).toMatchObject({ requestedUnits: 4 });

    // A window the form refuses is not asked about.
    fireEvent.change(screen.getByLabelText(/Exclude IDs ordered/), { target: { value: "500" } });
    await waitFor(() => expect(lastCheck()).toMatchObject({ exclude_ordered_days: null }), { timeout: 2000 });

    fireEvent.click(smartIdBox());
    fireEvent.click(screen.getByRole("radio", { name: "AI" }));
    expect(summary()).toMatchObject({ smartId: false, aiRun: true });
  });
});
