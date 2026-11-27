// @vitest-environment jsdom
/**
 * The order form's room check (POST /unit-limits/run-check): asked once the
 * typing has settled, with exactly the run's own question (selection, product
 * facts, the order's variant, size, mode, total, "Exclude IDs" window, and
 * this order's limit changes), never asked with nothing to ask about, and its
 * last answer kept for the same product while a newer check is on its way.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccountSelection, ApplicableLimit, LimitOverride, RunCheckResult } from "./types";

const mocks = vi.hoisted(() => ({ checkRun: vi.fn() }));

vi.mock("./api", () => ({
  checkRun: mocks.checkRun,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { RUN_CHECK_DEBOUNCE_MS, refusesChanges, useRunCheck, type RunCheckArgs } from "./useRunCheck";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  // A reset, not a clear: an answer queued by one test must never reach the next.
  mocks.checkRun.mockReset();
});

const IDS: AccountSelection = { mode: "ids", ids: [7, 8] };
const BASE: RunCheckArgs = { brand: "vivo", category: "Mobiles", selection: IDS };
const VIVO: ApplicableLimit = { limit_id: 4, scope: "brand", value: "vivo", max_units: 2, window_days: 30 };

function result(over: Partial<RunCheckResult> = {}): RunCheckResult {
  return {
    accounts: 2,
    set_aside: 0,
    set_aside_at_limit: 0,
    set_aside_short: 0,
    set_aside_ordered_recently: 0,
    units_available: 2,
    limits_apply: true,
    limits: [],
    applicable_limits: [VIVO],
    ...over,
  };
}

/** Past the check's wait, with the answer (if any) landed. */
async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(RUN_CHECK_DEBOUNCE_MS);
  });
}

function asked(call: number): Record<string, unknown> {
  return mocks.checkRun.mock.calls[call][0] as Record<string, unknown>;
}

describe("useRunCheck", () => {
  it("asks once the typing has settled, with the run's own question", async () => {
    mocks.checkRun.mockResolvedValue(result());
    const hook = renderHook(() =>
      useRunCheck({
        ...BASE,
        model: "vivo T5x 5G",
        seller: "SAPHEDretail",
        variant: "MOBHNDTD57UJUK9Q",
        unitsPerOrder: 2,
        qtyMode: "minimum",
        totalUnits: 20,
        excludeDays: 7,
      }),
    );
    expect(mocks.checkRun).not.toHaveBeenCalled();

    await settle();

    expect(mocks.checkRun).toHaveBeenCalledTimes(1);
    // Exactly the contract's fields: the server refuses any other.
    expect(asked(0)).toEqual({
      account_ids: [7, 8],
      brand: "vivo",
      category: "Mobiles",
      model: "vivo T5x 5G",
      seller: "SAPHEDretail",
      variant: "MOBHNDTD57UJUK9Q",
      qty: 2,
      qty_mode: "minimum",
      total_quantity: 20,
      exclude_ordered_days: 7,
    });
    expect(hook.result.current.state).toEqual({ kind: "ready", run: result() });
    expect(hook.result.current.last).toEqual(result());
  });

  it("sends a filter, one order per account and no window as nulls, and leaves out a missing variant", async () => {
    mocks.checkRun.mockResolvedValue(result());
    renderHook(() =>
      useRunCheck({ ...BASE, selection: { mode: "filter", filter: { statuses: ["session_valid"] }, count: 40 } }),
    );
    await settle();

    expect(asked(0)).toEqual({
      filter: { statuses: ["session_valid"] },
      brand: "vivo",
      category: "Mobiles",
      qty: 1,
      qty_mode: "upto",
      total_quantity: null,
      exclude_ordered_days: null,
    });
  });

  it("sends this order's limit changes, and leaves them out when there are none", async () => {
    mocks.checkRun.mockResolvedValue(result());
    const change: LimitOverride = { limit_id: 4, max_units: 1, window_days: 30, enabled: true };
    renderHook(() => useRunCheck({ ...BASE, limitOverrides: [change] }));
    renderHook(() => useRunCheck({ ...BASE, brand: "Samsung", limitOverrides: [] }));
    await settle();

    expect(asked(0).limit_overrides).toEqual([change]);
    expect("limit_overrides" in asked(1)).toBe(false);
  });

  it("hands the changes the saved limits that apply once read, and asks again with them", async () => {
    mocks.checkRun.mockResolvedValue(result());
    const given = vi.fn((applicable: ApplicableLimit[] | null): LimitOverride[] | null =>
      applicable ? [{ limit_id: applicable[0].limit_id, max_units: 1, window_days: 30, enabled: true }] : null,
    );
    renderHook(() => useRunCheck({ ...BASE, limitOverrides: given }));
    expect(given).toHaveBeenCalledWith(null);

    await settle();
    expect("limit_overrides" in asked(0)).toBe(false);
    expect(given).toHaveBeenLastCalledWith([VIVO]);

    await settle();
    expect(mocks.checkRun).toHaveBeenCalledTimes(2);
    expect(asked(1).limit_overrides).toEqual([{ limit_id: 4, max_units: 1, window_days: 30, enabled: true }]);

    // The same question again asks nothing more.
    await settle();
    expect(mocks.checkRun).toHaveBeenCalledTimes(2);
  });

  it("asks nothing with no ID, or with no product fact and no window", async () => {
    const noIds = renderHook(() => useRunCheck({ ...BASE, selection: { mode: "ids", ids: [] } }));
    const nothing = renderHook(() => useRunCheck({ brand: " ", category: "", selection: IDS }));
    await settle();

    expect(mocks.checkRun).not.toHaveBeenCalled();
    expect(noIds.result.current.state).toEqual({ kind: "idle" });
    expect(nothing.result.current.state).toEqual({ kind: "idle" });
  });

  it("asks with an Exclude IDs window alone, or a variant alone", async () => {
    mocks.checkRun.mockResolvedValue(result());
    renderHook(() => useRunCheck({ brand: "", category: "", selection: IDS, excludeDays: 14 }));
    renderHook(() => useRunCheck({ brand: "", category: "", selection: IDS, variant: "MOBHNDTD57UJUK9Q" }));
    await settle();

    expect(mocks.checkRun).toHaveBeenCalledTimes(2);
    expect(asked(0).exclude_ordered_days).toBe(14);
    expect(asked(1).variant).toBe("MOBHNDTD57UJUK9Q");
  });

  it("asks again when the same filter comes to match more accounts", async () => {
    mocks.checkRun.mockResolvedValueOnce(result({ accounts: 542, units_available: 542 }));
    mocks.checkRun.mockResolvedValueOnce(result({ accounts: 544, units_available: 544 }));
    const filter = { statuses: ["session_valid" as const], domains: ["catbar.net"] };
    const hook = renderHook((args: RunCheckArgs) => useRunCheck(args), {
      initialProps: { ...BASE, selection: { mode: "filter", filter, count: 542 } },
    });
    await settle();
    expect(mocks.checkRun).toHaveBeenCalledTimes(1);

    // Two more accounts signed in: the filter is the same, its count is not.
    hook.rerender({ ...BASE, selection: { mode: "filter", filter: { ...filter }, count: 544 } });
    await settle();
    expect(mocks.checkRun).toHaveBeenCalledTimes(2);
    expect(hook.result.current.state).toMatchObject({ kind: "ready", run: { units_available: 544 } });

    // The same filter and count again (a fresh but equal object): nothing to ask.
    hook.rerender({ ...BASE, selection: { mode: "filter", filter: { ...filter }, count: 544 } });
    await settle();
    expect(mocks.checkRun).toHaveBeenCalledTimes(2);
  });

  it("keeps the last answer for the same product while a new check runs, and drops it for another product", async () => {
    mocks.checkRun.mockResolvedValueOnce(result());
    mocks.checkRun.mockReturnValue(new Promise(() => undefined));
    const hook = renderHook((args: RunCheckArgs) => useRunCheck(args), { initialProps: BASE });
    await settle();
    expect(hook.result.current.last?.applicable_limits).toEqual([VIVO]);

    // Another selection, the same product.
    hook.rerender({ ...BASE, selection: { mode: "ids", ids: [7, 8, 9] } });
    await settle();
    expect(hook.result.current.state).toEqual({ kind: "loading" });
    expect(hook.result.current.last?.applicable_limits).toEqual([VIVO]);

    // Another product: its limits are not this one's.
    hook.rerender({ ...BASE, brand: "Samsung", selection: { mode: "ids", ids: [7, 8, 9] } });
    expect(hook.result.current.last).toBeNull();
  });

  it("reports a failed check, and asks again on request", async () => {
    mocks.checkRun.mockRejectedValueOnce(new Error("Server busy."));
    mocks.checkRun.mockResolvedValueOnce(result());
    const hook = renderHook(() => useRunCheck(BASE));
    await settle();
    expect(hook.result.current.state).toEqual({ kind: "error", message: "Server busy.", refusedChanges: false });

    act(() => hook.result.current.retry());
    await settle();
    expect(hook.result.current.state.kind).toBe("ready");
    expect(mocks.checkRun).toHaveBeenCalledTimes(2);
  });

  it("says when the server refused this order's limit changes, and only then", async () => {
    const said = "A brand limit for 'vivo' already applies: change that one instead.";
    // As the server sends it: the refusal names the changes.
    const refusal = Object.assign(new Error(said), { status: 422, detail: { message: said, field: "limit_overrides" } });
    const change: LimitOverride = { limit_id: null, scope: "brand", value: "vivo", max_units: 1, window_days: 30, enabled: true };
    mocks.checkRun.mockRejectedValue(refusal);
    const withChanges = renderHook(() => useRunCheck({ ...BASE, limitOverrides: [change] }));
    const without = renderHook(() => useRunCheck({ ...BASE, brand: "Samsung" }));
    await settle();

    expect(withChanges.result.current.state).toEqual({
      kind: "error",
      message: "A brand limit for 'vivo' already applies: change that one instead.",
      refusedChanges: true,
    });
    // A 422 to a check with no change is not about one.
    expect(without.result.current.state).toMatchObject({ kind: "error", refusedChanges: false });
  });

  it("does not blame the changes for a 422 about something else", async () => {
    const said = "This selection matches 900 accounts, over the limit of 500 for a single run.";
    mocks.checkRun.mockRejectedValue(Object.assign(new Error(said), { status: 422, detail: said }));
    const change: LimitOverride = { limit_id: 4, max_units: 1, window_days: 30, enabled: true };
    const hook = renderHook(() => useRunCheck({ ...BASE, limitOverrides: [change] }));
    await settle();

    expect(hook.result.current.state).toEqual({ kind: "error", message: said, refusedChanges: false });
  });

  it("blames the changes for a malformed one, as a validation error names it", () => {
    const malformed = Object.assign(new Error("bad"), {
      status: 422,
      detail: [{ loc: ["body", "limit_overrides", 0, "max_units"], msg: "Input should be greater than or equal to 1" }],
    });
    const other = Object.assign(new Error("bad"), { status: 422, detail: [{ loc: ["body", "qty"], msg: "too small" }] });

    expect(refusesChanges(malformed)).toBe(true);
    expect(refusesChanges(other)).toBe(false);
    expect(refusesChanges(Object.assign(new Error("x"), { status: 500, detail: { field: "limit_overrides" } }))).toBe(false);
  });

  it("asks about IDs that already bought this product only when the box is ticked and there is a link", async () => {
    mocks.checkRun.mockResolvedValue(result());
    const link = "https://www.flipkart.com/vivo-t5-lite/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q";
    renderHook(() => useRunCheck({ ...BASE, skipBought: true, productLink: ` ${link} ` }));
    renderHook(() => useRunCheck({ ...BASE, brand: "Samsung", skipBought: false, productLink: link }));
    renderHook(() => useRunCheck({ ...BASE, brand: "Apple", skipBought: true, productLink: "" }));
    await settle();

    const bodies = mocks.checkRun.mock.calls.map(([body]) => body);
    expect(bodies[0]).toMatchObject({ skip_bought_product: true, product_link: link });
    expect(bodies[1]).not.toHaveProperty("skip_bought_product");
    expect(bodies[2]).not.toHaveProperty("skip_bought_product");
  });

  it("asks even with no product details when only the already-bought tick can stop IDs", async () => {
    mocks.checkRun.mockResolvedValue(result());
    const link = "https://www.flipkart.com/vivo-t5-lite/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q";
    renderHook(() => useRunCheck({ ...BASE, brand: "", category: "", skipBought: true, productLink: link }));
    await settle();

    expect(mocks.checkRun).toHaveBeenCalledTimes(1);
  });

  it("does not blame the changes for a server that failed", async () => {
    mocks.checkRun.mockRejectedValue(Object.assign(new Error("Internal Server Error"), { status: 500 }));
    const change: LimitOverride = { limit_id: 4, max_units: 1, window_days: 30, enabled: true };
    const hook = renderHook(() => useRunCheck({ ...BASE, limitOverrides: [change] }));
    await settle();

    expect(hook.result.current.state).toMatchObject({ kind: "error", refusedChanges: false });
  });

  it("drops an answer to a question no longer asked", async () => {
    let answerFirst: (run: RunCheckResult) => void = () => undefined;
    mocks.checkRun.mockReturnValueOnce(
      new Promise<RunCheckResult>((resolve) => {
        answerFirst = resolve;
      }),
    );
    mocks.checkRun.mockResolvedValueOnce(result({ units_available: 1 }));
    const hook = renderHook((args: RunCheckArgs) => useRunCheck(args), { initialProps: BASE });
    await settle();

    hook.rerender({ ...BASE, excludeDays: 7 });
    await settle();
    await act(async () => answerFirst(result({ units_available: 99 })));

    expect(hook.result.current.state).toEqual({ kind: "ready", run: result({ units_available: 1 }) });
  });
});
