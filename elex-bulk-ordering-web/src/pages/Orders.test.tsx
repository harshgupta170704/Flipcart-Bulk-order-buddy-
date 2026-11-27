import { describe, expect, it } from "vitest";
import { paymentLabel, quantityLabel, unitsLabel } from "./Orders";

describe("Orders page labels", () => {
  it("shows units placed against the total asked for", () => {
    expect(unitsLabel({ units_placed: 18, total_quantity: 20 })).toBe("18/20 · 90%");
    expect(unitsLabel({ units_placed: 3, total_quantity: null })).toBe("3");
  });

  it("describes the quantity a run was given", () => {
    expect(quantityLabel({ total_quantity: 100, qty_per_order: 4 })).toBe("100 total · 4 per order");
    expect(quantityLabel({ total_quantity: null, qty_per_order: null })).toBe("—");
  });

  it("names payment modes", () => {
    expect(paymentLabel("cod")).toBe("Cash on Delivery");
    expect(paymentLabel("card")).toBe("Card");
    expect(paymentLabel(null)).toBe("—");
  });
});

import { presetRange, runsCsv, unitsShare } from "./Orders";
import type { BulkOrderRun } from "../lib/types";

describe("Orders page", () => {
  const run = {
    id: 7, type: "order", status: "completed", progress: 100, total_items: 20, done_items: 20, failed_items: 2,
    skipped_items: 0, unconfirmed_items: 0, cancel_requested: false, payload: {}, started_at: "2026-11-17T06:00:00Z",
    finished_at: "2026-11-17T06:01:00Z", platform: "flipkart", mode: "ai", product_link: "https://www.flipkart.com/p",
    product_name: 'Phone, "Blue"', total_quantity: 20, qty_per_order: 1, orders_placed: 6, units_placed: 6,
    orders_unconfirmed: 0, orders_failed: 14, amount_placed: 6000, payment_mode: "cod", card_type: null, ref_code: "R1",
    started_by: "ops", address_label: "HQ", gst_label: null, target: 1000, parent_card: null,
    execution_version: "high", flipkart_minutes: false,
  } as unknown as BulkOrderRun;

  it("shows the share placed of the total, or of the attempts", () => {
    expect(unitsShare(run)).toBe(30);
    expect(unitsShare({ ...run, total_quantity: null, orders_placed: 5, total_items: 10 })).toBe(50);
    expect(unitsShare({ ...run, total_quantity: null, total_items: 0 })).toBe(0);
  });

  it("turns a preset into a start time", () => {
    const now = Date.parse("2026-11-17T12:00:00Z");
    expect(presetRange("24h", now).since).toBe("2026-11-16T12:00:00.000Z");
    expect(presetRange("all", now)).toEqual({});
  });

  it("writes the runs as CSV, quoting text that needs it", () => {
    const csv = runsCsv([run]);
    const [head, row] = csv.trim().split("\n");
    expect(head.startsWith("Run,Created,Platform,Mode")).toBe(true);
    expect(row).toContain('"Phone, ""Blue"""');
    expect(row).toContain("Cash on Delivery");
  });
});

import { canRetryFailed } from "./Orders";

describe("Retry failed button", () => {
  const base = {
    id: 820, type: "order", status: "completed", total_items: 292, done_items: 292, failed_items: 95,
    skipped_items: 0, unconfirmed_items: 9,
  } as unknown as BulkOrderRun;

  it("shows on a finished run with failed, untried or unfinished accounts", () => {
    expect(canRetryFailed(base)).toBe(true);
    expect(canRetryFailed({ ...base, failed_items: 0, skipped_items: 3 })).toBe(true);
    expect(canRetryFailed({ ...base, status: "failed", failed_items: 0, done_items: 250 })).toBe(true);
  });

  it("not while the run is going, nor when every account got an order", () => {
    expect(canRetryFailed({ ...base, status: "running" })).toBe(false);
    expect(canRetryFailed({ ...base, status: "queued" })).toBe(false);
    expect(canRetryFailed({ ...base, failed_items: 0, skipped_items: 0 })).toBe(false);
  });
});
