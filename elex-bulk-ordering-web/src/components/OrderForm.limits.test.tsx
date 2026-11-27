// @vitest-environment jsdom
/**
 * "Limits for this order" in the order form's Platform Unit Limits (Smart ID,
 * part 2): every saved limit that applies to the product on its own line, its
 * units, days and on/off changed for this one order, lines added for this
 * order, "Use saved limits", who may loosen, the box checked before Submit,
 * and the changes sent with every check and with the order, only with Smart
 * ID on and not in an AI run. Run again puts them back for the same product;
 * another link, Submit and Reset drop them. The order's variant (the link's
 * pid) goes with the checks, and the box says when variant limits cannot apply.
 *
 * The children that load data of their own are stubbed, as in OrderForm.test.tsx;
 * the room check answers with two saved limits that apply.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApplicableLimit, Job, JobDetail, ProductPreview, RunCheckResult } from "../lib/types";

const mocks = vi.hoisted(() => ({
  startOrderJob: vi.fn(),
  previewProduct: vi.fn(),
  getSettings: vi.fn(),
  getJob: vi.fn(),
  checkRun: vi.fn(),
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

// The preview's summary has tests of its own; here it would only repeat the limits' names.
vi.mock("./LimitUsageSummary", () => ({ LimitUsageSummary: () => null }));

import { OrderForm } from "./OrderForm";

const LINK = "https://www.flipkart.com/vivo-t5x-5g/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q";
const OTHER_LINK = "https://www.flipkart.com/vivo-t5x-5g-blue/p/itm5420c578ec2df?pid=MOBHNDTDABCDEF12";
const INVALID = "Platform limits invalid: fix the marked lines under Limits for this order.";
const ONLY_ADMIN = "Only an admin can loosen a limit for one order.";

const PREVIEW: ProductPreview = {
  url: LINK,
  name: "vivo T5x 5G (Glossy Black, 128 GB)",
  brand: "vivo",
  category: "Mobiles",
  color: null,
  image: null,
  sku: null,
  price: 16999,
  currency: "INR",
  availability: null,
  in_stock: true,
  rating: null,
  rating_count: null,
  seller: null,
  error: null,
  default_seller: null,
  sellers: [],
  sellers_error: null,
};

const VIVO: ApplicableLimit = { limit_id: 4, scope: "brand", value: "vivo", max_units: 2, window_days: 30 };
const ANY_CATEGORY: ApplicableLimit = { limit_id: 9, scope: "category", value: "", max_units: 20, window_days: 31 };

const CHECKED: RunCheckResult = {
  accounts: 2,
  set_aside: 0,
  set_aside_at_limit: 0,
  set_aside_short: 0,
  set_aside_ordered_recently: 0,
  units_available: 2,
  limits_apply: true,
  limits: [],
  applicable_limits: [VIVO, ANY_CATEGORY],
};

const startedJob: Job = {
  id: 95,
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
  started_at: "2026-11-24T09:00:00Z",
  finished_at: null,
};

beforeEach(() => {
  mocks.previewProduct.mockResolvedValue(PREVIEW);
  mocks.startOrderJob.mockResolvedValue(startedJob);
  mocks.getSettings.mockResolvedValue({ smart_id_default_on: true });
  mocks.checkRun.mockResolvedValue(CHECKED);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const SLOW = { timeout: 4000 };

function renderForm({ isAdmin = true, path = "/ordering" }: { isAdmin?: boolean; path?: string } = {}) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <OrderForm
        accounts={{ accounts: [], loading: false, error: null, reload: () => undefined, total: 2, truncated: false }}
        runActive={false}
        onStarted={vi.fn()}
        isAdmin={isAdmin}
      />
    </MemoryRouter>,
  );
}

function fillIn(link = LINK) {
  fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: link } });
  fireEvent.change(screen.getByLabelText(/Payment mode/), { target: { value: "cod" } });
  fireEvent.change(screen.getByLabelText(/Target \(max payable per unit\)/), { target: { value: "18250" } });
}

function box(): HTMLElement {
  return screen.getByRole("region", { name: "Limits for this order" });
}

/** The product has loaded (brand filled from it) and the box lists its limits. */
async function ready(): Promise<ReturnType<typeof within>> {
  await waitFor(() => expect((document.querySelector('input[name="brand"]') as HTMLInputElement).value).toBe("vivo"), SLOW);
  await within(box()).findByLabelText("Units: Brand · vivo", {}, SLOW);
  return within(box());
}

function input(scope: ReturnType<typeof within>, label: string): HTMLInputElement {
  return scope.getByLabelText(label) as HTMLInputElement;
}

async function submit(name: RegExp = /^Submit Order$/): Promise<void> {
  const button = screen.getByRole("button", { name }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
}

/** What the order was sent with. */
function sent(): Record<string, unknown> {
  return mocks.startOrderJob.mock.calls[0][0] as Record<string, unknown>;
}

/** The last room check asked. */
function lastCheck(): Record<string, unknown> {
  const calls = mocks.checkRun.mock.calls;
  return calls[calls.length - 1][0] as Record<string, unknown>;
}

describe("Limits for this order: the saved limits that apply", () => {
  it("lists each one on its own line with units, days and an on/off tick", async () => {
    renderForm();
    fillIn();
    const b = await ready();

    expect(input(b, "Units: Brand · vivo").value).toBe("2");
    expect(input(b, "Days: Brand · vivo").value).toBe("30");
    expect(input(b, "Use Brand · vivo for this order").checked).toBe(true);
    expect(input(b, "Units: Any category").value).toBe("20");
    expect(input(b, "Days: Any category").value).toBe("31");
    expect(b.queryByText("Changed")).toBeNull();
    expect(b.queryByRole("button", { name: "Use saved limits" })).toBeNull();
    // The check asked about the order's variant, read from the link.
    expect(lastCheck().variant).toBe("MOBHNDTD57UJUK9Q");
    expect("limit_overrides" in lastCheck()).toBe(false);
  });

  it("marks a changed line, sends it with the next check and with the order, then clears it", async () => {
    renderForm();
    fillIn();
    const b = await ready();

    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });
    expect(b.getByText("Changed")).toBeTruthy();
    expect(b.getByText("Saved: 2 units / 30 days")).toBeTruthy();
    const change = [{ limit_id: 4, max_units: 1, window_days: 30, enabled: true }];
    await waitFor(() => expect(lastCheck().limit_overrides).toEqual(change), SLOW);

    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toEqual(change);
    expect(sent().smart_id).toBe(true);
    // The next order starts from the saved limits again.
    await waitFor(() => expect(within(box()).queryByText("Changed")).toBeNull());
  });

  it("drops every change on Use saved limits", async () => {
    renderForm();
    fillIn();
    const b = await ready();
    fireEvent.change(input(b, "Days: Any category"), { target: { value: "60" } });
    fireEvent.click(b.getByRole("button", { name: "Add a limit for this order" }));

    fireEvent.click(b.getByRole("button", { name: "Use saved limits" }));

    expect(input(b, "Days: Any category").value).toBe("31");
    expect(b.queryByRole("listitem", { name: "Added limit 1" })).toBeNull();
    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toBeNull();
  });
});

describe("Limits for this order: lines added for this order", () => {
  it("takes the product's value where it can, a model name from the title, and is checked before Submit", async () => {
    renderForm();
    fillIn();
    const b = await ready();

    fireEvent.click(b.getByRole("button", { name: "Add a limit for this order" }));
    // The saved "brand vivo" covers the product's brand; "Any category" is another
    // limit than one for "Mobiles", so the first new line is the category's.
    const first = within(b.getByRole("listitem", { name: "Added limit 1" }));
    expect((first.getByLabelText("Scope: Added limit 1") as HTMLSelectElement).value).toBe("category");
    expect(first.getByText("Mobiles")).toBeTruthy();
    fireEvent.change(first.getByLabelText("Units: Added limit 1"), { target: { value: "1" } });

    fireEvent.click(b.getByRole("button", { name: "Add a limit for this order" }));
    const second = within(b.getByRole("listitem", { name: "Added limit 2" }));
    // No second category line, and no brand line for the vivo a saved limit covers.
    expect(
      Array.from((second.getByLabelText("Scope: Added limit 2") as HTMLSelectElement).options).map((option) => option.value),
    ).toEqual(["model", "variant", "seller"]);
    fireEvent.change(second.getByLabelText("Scope: Added limit 2"), { target: { value: "model" } });
    fireEvent.change(second.getByLabelText("Model name: Added limit 2"), { target: { value: "T6 Pro" } });
    fireEvent.change(second.getByLabelText("Units: Added limit 2"), { target: { value: "1" } });

    await submit();
    expect(await screen.findByText(INVALID)).toBeTruthy();
    expect(second.getByRole("alert").textContent).toBe("The product's title does not contain “T6 Pro”.");
    expect(mocks.startOrderJob).not.toHaveBeenCalled();

    fireEvent.change(second.getByLabelText("Model name: Added limit 2"), { target: { value: "t5x 5g" } });
    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toEqual([
      { limit_id: null, scope: "category", value: "Mobiles", max_units: 1, window_days: 30, enabled: true },
      { limit_id: null, scope: "model", value: "t5x 5g", max_units: 1, window_days: 30, enabled: true },
    ]);
  });

  it("refuses to send units or days out of range", async () => {
    renderForm();
    fillIn();
    const b = await ready();
    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "0" } });
    fireEvent.change(input(b, "Days: Brand · vivo"), { target: { value: "4000" } });

    await submit();

    expect(await screen.findByText(INVALID)).toBeTruthy();
    expect(within(b.getByRole("listitem", { name: "Brand · vivo" })).getByRole("alert").textContent).toBe(
      "Units: a whole number, 1 or more. Days: a whole number from 1 to 3650.",
    );
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
    // A change that does not pass is never sent with a check either.
    for (const call of mocks.checkRun.mock.calls) expect("limit_overrides" in call[0]).toBe(false);
  });
});

describe("Limits for this order: what goes with the check", () => {
  it("still sends the changes that hold while another line is wrong, and marks only that one", async () => {
    renderForm();
    fillIn();
    const b = await ready();

    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });
    fireEvent.change(input(b, "Days: Any category"), { target: { value: "4000" } });

    await waitFor(
      () => expect(lastCheck().limit_overrides).toEqual([{ limit_id: 4, max_units: 1, window_days: 30, enabled: true }]),
      SLOW,
    );
    await submit();
    expect(await screen.findByText(INVALID)).toBeTruthy();
    expect(within(b.getByRole("listitem", { name: "Any category" })).getByRole("alert").textContent).toBe(
      "Days: a whole number from 1 to 3650.",
    );
    expect(within(b.getByRole("listitem", { name: "Brand · vivo" })).queryByRole("alert")).toBeNull();
    expect(mocks.startOrderJob).not.toHaveBeenCalled();
  });

  it("never lets a switched-off line's fields block Submit: it goes with its saved numbers", async () => {
    renderForm({ isAdmin: true });
    fillIn();
    const b = await ready();

    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "" } });
    fireEvent.click(input(b, "Use Brand · vivo for this order"));
    expect(input(b, "Units: Brand · vivo").disabled).toBe(true);

    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toEqual([{ limit_id: 4, max_units: 2, window_days: 30, enabled: false }]);
  });

  it("shows the server's refusal of a change in the box", async () => {
    const refusal = "A brand limit for 'vivo' cannot be changed for this order: try again.";
    mocks.checkRun.mockImplementation((body: { limit_overrides?: unknown }) =>
      body.limit_overrides
        ? Promise.reject(
            Object.assign(new Error(refusal), { status: 422, detail: { message: refusal, field: "limit_overrides" } }),
          )
        : Promise.resolve(CHECKED),
    );
    renderForm();
    fillIn();
    const b = await ready();

    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });

    expect((await b.findByText(refusal, {}, SLOW)).getAttribute("role")).toBe("alert");
    // The lines stay, so the change can be put right.
    expect(input(b, "Units: Brand · vivo").value).toBe("1");
  });
});

describe("Limits for this order: who may loosen", () => {
  it("lets someone who is not an admin tighten a limit but not loosen it", async () => {
    renderForm({ isAdmin: false });
    fillIn();
    const b = await ready();

    expect(b.getByText(ONLY_ADMIN)).toBeTruthy();
    expect(input(b, "Use Brand · vivo for this order").disabled).toBe(true);
    expect(input(b, "Units: Brand · vivo").max).toBe("2");
    expect(input(b, "Days: Brand · vivo").min).toBe("30");

    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "5" } });
    // Said at once, on the line.
    expect(within(b.getByRole("listitem", { name: "Brand · vivo" })).getByRole("alert").textContent).toBe(ONLY_ADMIN);
    await submit();
    expect(await screen.findByText(INVALID)).toBeTruthy();
    expect(mocks.startOrderJob).not.toHaveBeenCalled();

    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });
    fireEvent.change(input(b, "Days: Brand · vivo"), { target: { value: "45" } });
    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toEqual([{ limit_id: 4, max_units: 1, window_days: 45, enabled: true }]);
    for (const call of mocks.checkRun.mock.calls) {
      const changes = (call[0] as { limit_overrides?: { max_units: number }[] }).limit_overrides ?? [];
      expect(changes.every((change) => change.max_units <= 2)).toBe(true);
    }
  });

  it("lets an admin loosen a limit and switch a line off", async () => {
    renderForm({ isAdmin: true });
    fillIn();
    const b = await ready();
    expect(b.queryByText(ONLY_ADMIN)).toBeNull();

    fireEvent.click(input(b, "Use Brand · vivo for this order"));
    // No ceiling on units: any whole number from 1.
    fireEvent.change(input(b, "Units: Any category"), { target: { value: "5000" } });

    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toEqual([
      { limit_id: 4, max_units: 2, window_days: 30, enabled: false },
      { limit_id: 9, max_units: 5000, window_days: 31, enabled: true },
    ]);
  });
});

describe("Limits for this order with Smart ID off, and in an AI run", () => {
  it("shows the saved limits greyed with Smart ID off, sends no change, and keeps the changes for when it is on", async () => {
    renderForm();
    fillIn();
    const b = await ready();
    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });

    fireEvent.click(screen.getByLabelText("Smart ID Selection"));

    expect(b.getByText("Off for this order: each account is still checked at its turn with the saved limits.")).toBeTruthy();
    expect(b.queryByLabelText("Units: Brand · vivo")).toBeNull();
    // Asked again without the variant (off is exactly as before), then greyed.
    expect((await b.findByText("Brand · vivo", {}, SLOW)).closest("li")?.textContent).toBe("Brand · vivo (2 units / 30 days)");
    expect(b.queryByRole("button", { name: "Add a limit for this order" })).toBeNull();
    const asked = mocks.checkRun.mock.calls.length;
    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().smart_id).toBe(false);
    expect(sent().limit_overrides).toBeNull();
    for (const call of mocks.checkRun.mock.calls.slice(asked)) expect("limit_overrides" in call[0]).toBe(false);
  });

  it("brings the changes back when Smart ID is turned on again", async () => {
    renderForm();
    fillIn();
    const b = await ready();
    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });
    fireEvent.click(screen.getByLabelText("Smart ID Selection"));
    fireEvent.click(screen.getByLabelText("Smart ID Selection"));

    expect(input(b, "Units: Brand · vivo").value).toBe("1");
    expect(b.getByText("Changed")).toBeTruthy();
  });

  it("uses the saved limits in an AI run", async () => {
    renderForm();
    fillIn();
    const b = await ready();
    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });

    fireEvent.click(screen.getByRole("radio", { name: "AI" }));

    expect(b.getByText("AI runs use the saved limits: each account is still checked at its turn.")).toBeTruthy();
    expect(b.queryByLabelText("Units: Brand · vivo")).toBeNull();
    await submit(/^Submit AI Order$/);
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().mode).toBe("ai");
    expect(sent().limit_overrides).toBeNull();
  });
});

describe("Limits for this order: which product they belong to", () => {
  it("drops the changes when the product link changes", async () => {
    renderForm();
    fillIn();
    const b = await ready();
    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });
    fireEvent.click(b.getByRole("button", { name: "Add a limit for this order" }));

    fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: OTHER_LINK } });
    const again = await ready();

    expect(input(again, "Units: Brand · vivo").value).toBe("2");
    expect(again.queryByText("Changed")).toBeNull();
    expect(again.queryByRole("listitem", { name: "Added limit 1" })).toBeNull();

    // Dropped, not hidden: the first link back brings nothing back.
    fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: LINK } });
    const back = await ready();
    expect(input(back, "Units: Brand · vivo").value).toBe("2");
    expect(back.queryByRole("listitem", { name: "Added limit 1" })).toBeNull();
  });

  it("clears them on Reset to defaults", async () => {
    renderForm();
    fillIn();
    const b = await ready();
    fireEvent.change(input(b, "Units: Brand · vivo"), { target: { value: "1" } });

    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));
    fillIn();
    const again = await ready();

    expect(input(again, "Units: Brand · vivo").value).toBe("2");
  });

  it("puts back an earlier run's changes on Run again, for its product link", async () => {
    const detail: JobDetail = {
      ...startedJob,
      id: 31,
      status: "completed",
      items: [],
      payload: {
        product_link: LINK,
        payment_mode: "cod",
        target: 18250,
        smart_id: true,
        limit_overrides: [
          { limit_id: 4, max_units: 1, window_days: 30, enabled: true },
          { limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, window_days: 15, enabled: true },
        ],
      },
    };
    mocks.getJob.mockResolvedValue(detail);
    renderForm({ path: "/ordering?from=31" });
    expect(await screen.findByText(/Filled in from run #31/)).toBeTruthy();
    const b = await ready();

    expect(input(b, "Units: Brand · vivo").value).toBe("1");
    expect(b.getByText("Changed")).toBeTruthy();
    expect((b.getByLabelText("Model name: Added limit 1") as HTMLInputElement).value).toBe("T5x 5G");
    await waitFor(
      () =>
        expect(lastCheck().limit_overrides).toEqual([
          { limit_id: 4, max_units: 1, window_days: 30, enabled: true },
          { limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, window_days: 15, enabled: true },
        ]),
      SLOW,
    );

    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toEqual([
      { limit_id: 4, max_units: 1, window_days: 30, enabled: true },
      { limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, window_days: 15, enabled: true },
    ]);
  });

  it("does not carry an earlier run's changes to another product link", async () => {
    mocks.getJob.mockResolvedValue({
      ...startedJob,
      id: 31,
      status: "completed",
      items: [],
      payload: {
        product_link: LINK,
        payment_mode: "cod",
        target: 18250,
        smart_id: true,
        limit_overrides: [{ limit_id: 4, max_units: 1, window_days: 30, enabled: true }],
      },
    } satisfies JobDetail);
    renderForm({ path: "/ordering?from=31" });
    expect(await screen.findByText(/Filled in from run #31/)).toBeTruthy();
    await ready();

    fireEvent.change(screen.getByLabelText(/Product URL/), { target: { value: OTHER_LINK } });
    const b = await ready();

    expect(input(b, "Units: Brand · vivo").value).toBe("2");
    await submit();
    await waitFor(() => expect(mocks.startOrderJob).toHaveBeenCalledTimes(1));
    expect(sent().limit_overrides).toBeNull();
  });
});

describe("The order's variant", () => {
  it("goes with the check only with Smart ID on, AI runs included", async () => {
    renderForm();
    fillIn();
    await ready();
    expect(lastCheck().variant).toBe("MOBHNDTD57UJUK9Q");

    // Off is exactly as before: no variant limits.
    fireEvent.click(screen.getByLabelText("Smart ID Selection"));
    await waitFor(() => expect("variant" in lastCheck()).toBe(false), SLOW);

    fireEvent.click(screen.getByRole("radio", { name: "AI" }));
    fireEvent.click(screen.getByLabelText("Smart ID Selection"));
    await waitFor(() => expect(lastCheck().variant).toBe("MOBHNDTD57UJUK9Q"), SLOW);
  });

  it("is said not to apply only with Smart ID on", async () => {
    renderForm();
    fillIn("https://www.flipkart.com/vivo-t5x-5g/p/itm5420c578ec2df");
    const line = "Variant limits cannot apply to this order: the link names no variant.";
    expect(await within(box()).findByText(line, {}, SLOW)).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Smart ID Selection"));
    expect(within(box()).queryByText(line)).toBeNull();
  });

  it("is not sent, and the box says why, for an order of several products", async () => {
    renderForm();
    fillIn();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "+ Add product URL" }));
    fireEvent.change(screen.getByLabelText(/Additional product URL 2/), {
      target: { value: "https://www.flipkart.com/p/itm2?pid=ACCABCDEF12" },
    });

    expect(
      within(box()).getByText("Variant limits cannot apply to this order: it has several products."),
    ).toBeTruthy();
    await waitFor(() => expect("variant" in lastCheck()).toBe(false), SLOW);
  });

  it("is not sent, and the box says why, for a link that names no variant", async () => {
    renderForm();
    fillIn("https://www.flipkart.com/vivo-t5x-5g/p/itm5420c578ec2df");
    expect(
      await within(box()).findByText("Variant limits cannot apply to this order: the link names no variant.", {}, SLOW),
    ).toBeTruthy();
    await waitFor(() => expect(mocks.checkRun).toHaveBeenCalled(), SLOW);
    expect("variant" in lastCheck()).toBe(false);
  });
});
