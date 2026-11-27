// @vitest-environment jsdom
/**
 * The Orders page's Run settings dialog (Smart ID, part 1): Smart ID
 * Selection, "Spread orders across accounts" and the run's notes in words,
 * then every other saved setting as it was saved. A run from before Smart ID
 * Selection saved no value for it, and the dialog says so.
 */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BulkOrderRun, JobDetail } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listBulkOrders: vi.fn(),
  listBulkOrderCreators: vi.fn(),
  getJob: vi.fn(),
  getFleet: vi.fn(),
}));

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  ...mocks,
}));

import Orders, { RunSettingsList } from "./Orders";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const NOTE =
  "Smart ID Selection: on — 3 IDs set aside before the run started (2 at their unit limit, 1 ordered recently).";

/** What a dialog row says: the value beside the label. */
function row(scope: HTMLElement, label: string): string | null | undefined {
  return within(scope).getByText(label, { selector: "dt" }).nextElementSibling?.textContent;
}

function listOf(payload: Record<string, unknown> | null): HTMLElement {
  return render(<RunSettingsList payload={payload} />).container;
}

describe("RunSettingsList", () => {
  it("says Smart ID Selection was on, the run's notes line by line, and keeps the other settings", () => {
    const list = listOf({
      product_link: "https://www.flipkart.com/p/itm1",
      qty: 1,
      smart_id: true,
      smart_selection: true,
      notes: [NOTE, "variant limits cannot apply: the link names no variant"],
      account_filter: { statuses: ["session_valid"] },
    });

    expect(row(list, "Smart ID Selection")).toBe("On");
    expect(row(list, "Spread orders across accounts")).toBe("On");
    const notes = within(list).getByText("Notes", { selector: "dt" }).nextElementSibling;
    expect(Array.from(notes?.querySelectorAll("li") ?? []).map((line) => line.textContent)).toEqual([
      NOTE,
      "variant limits cannot apply: the link names no variant",
    ]);
    // Every other key as it was saved.
    expect(row(list, "product link")).toBe("https://www.flipkart.com/p/itm1");
    expect(row(list, "qty")).toBe("1");
    expect(row(list, "account filter")).toBe('{"statuses":["session_valid"]}');
    // Not printed a second time as raw keys.
    expect(within(list).queryByText("smart id", { selector: "dt" })).toBeNull();
    expect(within(list).queryByText("smart selection", { selector: "dt" })).toBeNull();
    expect(within(list).queryByText("notes", { selector: "dt" })).toBeNull();
  });

  it("says whether Skip cards already used was on, and how many cards it left out, in words", () => {
    const list = listOf({ smart_id: true, payment_mode: "card", card_exclude_used: true, cards_kept_out: 100 });

    expect(row(list, "Skip cards already used")).toBe("On");
    expect(row(list, "Cards left out before pairing")).toBe(
      "100 (already used in an order, or kept out by another card rule)",
    );
    expect(within(list).queryByText("card exclude used", { selector: "dt" })).toBeNull();
    expect(within(list).queryByText("cards kept out", { selector: "dt" })).toBeNull();

    const off = listOf({ smart_id: true, payment_mode: "card", card_exclude_used: false });
    expect(within(off).getAllByText("Off", { selector: "dd" }).length).toBeGreaterThan(0);
    expect(row(off, "Skip cards already used")).toBe("Off");
  });

  it("says Off when the run had it off, and nothing about spreading when that was not on", () => {
    const list = listOf({ smart_id: false, qty: 1 });

    expect(row(list, "Smart ID Selection")).toBe("Off");
    expect(within(list).queryByText("Spread orders across accounts", { selector: "dt" })).toBeNull();
    expect(within(list).queryByText("Notes", { selector: "dt" })).toBeNull();
  });

  it("says it is not available for a run from before the switch", () => {
    const list = listOf({ qty: 1, smart_selection: true });

    expect(row(list, "Smart ID Selection")).toBe("not available (older run)");
    expect(row(list, "Spread orders across accounts")).toBe("On");
  });

  it("says each limit change made for the run in one line, and not as a raw key", () => {
    const list = listOf({
      smart_id: true,
      limit_overrides: [
        { limit_id: 4, scope: "brand", value: "vivo", max_units: 3, window_days: 30, enabled: true, saved_max_units: 2, saved_window_days: 30 },
        { limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, window_days: 30, enabled: true },
        { limit_id: 9, scope: "category", value: "Mobile", max_units: 20, window_days: 31, enabled: false },
      ],
    });

    const changes = within(list).getByText("Limits for this order", { selector: "dt" }).nextElementSibling;
    expect(Array.from(changes?.querySelectorAll("li") ?? []).map((line) => line.textContent)).toEqual([
      "brand 'vivo': 3 units per 30 days (saved: 2 per 30)",
      "added for this order: model 'T5x 5G' 1 unit per 30 days",
      "switched off: category 'Mobile'",
    ]);
    expect(within(list).queryByText("limit overrides", { selector: "dt" })).toBeNull();
  });

  it("says nothing about limit changes for a run that made none", () => {
    const list = listOf({ smart_id: true, qty: 1 });
    expect(within(list).queryByText("Limits for this order", { selector: "dt" })).toBeNull();
  });

  it("copes with a run that saved no settings at all", () => {
    const list = listOf(null);
    expect(row(list, "Smart ID Selection")).toBe("not available (older run)");
  });
});

describe("Orders page, Run settings", () => {
  it("opens the run's settings in words", async () => {
    const run = {
      id: 77, type: "order", status: "completed", progress: 100, total_items: 3, done_items: 3, failed_items: 0,
      skipped_items: 2, unconfirmed_items: 0, cancel_requested: false, payload: {}, started_at: "2026-11-24T06:00:00Z",
      finished_at: "2026-11-24T06:01:00Z", platform: "flipkart", mode: "real", product_link: "https://www.flipkart.com/p",
      product_name: "vivo T5 Lite 5G", total_quantity: null, qty_per_order: 1, orders_placed: 1, units_placed: 1,
      orders_unconfirmed: 0, orders_failed: 0, amount_placed: 16000, payment_mode: "cod", card_type: null, ref_code: null,
      started_by: "ops", address_label: "HQ", gst_label: null, target: 18250, parent_card: null,
      execution_version: "high", flipkart_minutes: false,
    } as unknown as BulkOrderRun;
    const detail = {
      ...run,
      payload: { product_link: "https://www.flipkart.com/p", smart_id: false, notes: ["Smart ID Selection: off"] },
      items: [],
    } as unknown as JobDetail;
    mocks.listBulkOrders.mockResolvedValue([run]);
    mocks.listBulkOrderCreators.mockResolvedValue([]);
    mocks.getFleet.mockReturnValue(new Promise(() => undefined));
    mocks.getJob.mockResolvedValue(detail);

    render(
      <MemoryRouter>
        <Orders />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Run settings" }));

    const dialog = await screen.findByRole("dialog", { name: "Run #77 settings" });
    expect(mocks.getJob).toHaveBeenCalledWith(77);
    expect(row(dialog, "Smart ID Selection")).toBe("Off");
    expect(row(dialog, "Notes")).toBe("Smart ID Selection: off");
    expect(row(dialog, "product link")).toBe("https://www.flipkart.com/p");
  });
});

describe("Run settings of a retry", () => {
  it("names the run it retried, in words, not as a raw key", () => {
    render(<RunSettingsList payload={{ retry_of: 820, smart_id: false }} />);
    expect(screen.getByText("Retry of")).toBeTruthy();
    expect(screen.getByText("Run #820: its accounts that got no order")).toBeTruthy();
    expect(screen.queryByText("retry_of")).toBeNull();
  });
});
