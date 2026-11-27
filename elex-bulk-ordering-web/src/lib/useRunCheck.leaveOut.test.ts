// @vitest-environment jsdom
/**
 * The run check less its selection (`leaveOut`): what decides who a run passes
 * over before it tries anyone. The card preview sends it, so a pool card sheet
 * (no email column) gives its cards only to the IDs the run will try — run
 * #960 (24 Nov 2026) handed 158 of 200 pool cards to IDs it then passed over.
 * It must always be the very question the check asks, minus the selection.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccountSelection, RunCheckResult } from "./types";

const mocks = vi.hoisted(() => ({ checkRun: vi.fn() }));

vi.mock("./api", () => ({
  checkRun: mocks.checkRun,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { RUN_CHECK_DEBOUNCE_MS, useRunCheck, type RunCheckArgs } from "./useRunCheck";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  mocks.checkRun.mockReset();
});

const ALL_CATBAR: AccountSelection = { mode: "filter", filter: { domains: ["catbar.net"] }, count: 722 };
const LINK = "https://www.flipkart.com/vivo-t5-lite/p/itm5420c578ec2df?pid=MOBHQSFFCMZN6GBC";

function result(): RunCheckResult {
  return {
    accounts: 722,
    already_bought: 10,
    set_aside: 190,
    set_aside_at_limit: 0,
    set_aside_short: 0,
    set_aside_ordered_recently: 190,
    units_available: 522,
    limits_apply: false,
    limits: [],
    applicable_limits: [],
  };
}

async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(RUN_CHECK_DEBOUNCE_MS);
  });
}

describe("useRunCheck leaveOut", () => {
  it("is the check's own question without the selection", async () => {
    mocks.checkRun.mockResolvedValue(result());
    const args: RunCheckArgs = {
      brand: "vivo",
      category: "mobile",
      variant: "MOBHQSFFCMZN6GBC",
      selection: ALL_CATBAR,
      totalUnits: 190,
      excludeDays: 7,
      skipBought: true,
      productLink: LINK,
    };
    const hook = renderHook(() => useRunCheck(args));
    await settle();

    const asked = mocks.checkRun.mock.calls[0][0] as Record<string, unknown>;
    const { filter, ...withoutSelection } = asked;
    expect(filter).toEqual({ domains: ["catbar.net"] });
    expect(hook.result.current.leaveOut).toEqual(withoutSelection);
    expect(hook.result.current.leaveOut).toEqual({
      brand: "vivo",
      category: "mobile",
      variant: "MOBHQSFFCMZN6GBC",
      qty: 1,
      qty_mode: "upto",
      total_quantity: 190,
      exclude_ordered_days: 7,
      skip_bought_product: true,
      product_link: LINK,
    });
  });

  it("is there before the check has answered, for any selection, and null with no ID", () => {
    const asking = renderHook(() =>
      useRunCheck({ brand: "", category: "", selection: ALL_CATBAR, excludeDays: 7 }),
    );
    expect(asking.result.current.leaveOut).toEqual({
      qty: 1,
      qty_mode: "upto",
      total_quantity: null,
      exclude_ordered_days: 7,
    });

    // Nothing for the check to ask, yet a wildcard unit limit can still set
    // IDs aside with Smart ID on: the pairing must be told, so it is built.
    const idle = renderHook(() => useRunCheck({ brand: "", category: "", selection: ALL_CATBAR }));
    expect(idle.result.current.leaveOut).toEqual({ qty: 1, qty_mode: "upto", total_quantity: null, exclude_ordered_days: null });
    expect(mocks.checkRun).not.toHaveBeenCalled();
    const none = renderHook(() =>
      useRunCheck({ brand: "vivo", category: "", selection: { mode: "ids", ids: [] }, excludeDays: 7 }),
    );
    expect(none.result.current.leaveOut).toBeNull();
  });

  it("leaves out the bought-product rule without a link", () => {
    const hook = renderHook(() =>
      useRunCheck({ brand: "vivo", category: "", selection: ALL_CATBAR, skipBought: true, productLink: "" }),
    );
    expect(hook.result.current.leaveOut).not.toHaveProperty("skip_bought_product");
    expect(hook.result.current.leaveOut).not.toHaveProperty("product_link");
  });
});
