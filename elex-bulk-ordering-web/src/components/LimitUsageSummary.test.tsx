// @vitest-environment jsdom
/**
 * The order preview's unit-limit summary, given the form's room check (the
 * check itself is useRunCheck's, tested in useRunCheck.test.ts). The answer
 * holds in every mode: with Smart ID Selection on it says how many IDs are set
 * aside before the run starts, and why; off, and in an AI run, it says each ID
 * is checked at its turn instead. The units available, and a warning when the
 * run asks for more, show in every mode. A limit's `blocked` is a yes or no
 * for the whole selection, never read as "1 account".
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LimitUsage, RunCheckResult } from "../lib/types";
import type { RunCheckState } from "../lib/useRunCheck";
import { LimitUsageSummary, setAsideLine, type LimitUsageSummaryProps } from "./LimitUsageSummary";

afterEach(() => {
  cleanup();
});

const OFF_LINE = "Smart ID Selection is off: each ID is checked when its turn comes.";
const AI_LINE = "In an AI run nothing is set aside before it starts: each ID is checked when its turn comes.";
const SHORT_TITLE = "Fewer units are available than this run asks for";

const VIVO: LimitUsage = {
  limit_id: 4,
  scope: "brand",
  value: "vivo",
  max_units: 2,
  window_days: 30,
  used: 6,
  remaining: 14,
  blocked: false,
};

const ANY_CATEGORY: LimitUsage = {
  limit_id: 9,
  scope: "category",
  value: "",
  max_units: 20,
  window_days: 31,
  used: 200,
  remaining: 0,
  blocked: true,
};

/** Ten IDs: two at their limit, one ordered recently, seven that can buy one unit each. */
function result(over: Partial<RunCheckResult> = {}): RunCheckResult {
  return {
    accounts: 10,
    set_aside: 3,
    set_aside_at_limit: 2,
    set_aside_short: 0,
    set_aside_ordered_recently: 1,
    units_available: 7,
    limits_apply: true,
    limits: [VIVO],
    applicable_limits: [],
    ...over,
  };
}

function ready(over: Partial<RunCheckResult> = {}): RunCheckState {
  return { kind: "ready", run: result(over) };
}

function summary(over: Partial<LimitUsageSummaryProps> = {}) {
  return <LimitUsageSummary check={ready()} onRetry={() => undefined} requestedUnits={7} smartId {...over} />;
}

function show(over: Partial<LimitUsageSummaryProps> = {}) {
  return render(summary(over));
}

describe("LimitUsageSummary with Smart ID Selection on", () => {
  it("says how many IDs are set aside and why, with the units available and the limits", () => {
    show();

    expect(screen.getByText("3 IDs will be set aside (2 at their unit limit, 1 ordered recently).")).toBeTruthy();
    expect(screen.getByText("Units available: 7")).toBeTruthy();
    expect(screen.getByText("Brand · vivo")).toBeTruthy();
    expect(screen.getByText(/14 left, 6 already used/)).toBeTruthy();
    expect(screen.queryByText(OFF_LINE)).toBeNull();
    expect(screen.queryByText(/No unit limit applies/)).toBeNull();
  });

  it("says when no ID is set aside", () => {
    show({ check: ready({ set_aside: 0, set_aside_at_limit: 0, set_aside_ordered_recently: 0, units_available: 10 }) });
    expect(screen.getByText("No ID will be set aside.")).toBeTruthy();
  });

  it("names all three reasons, each with its own count", () => {
    show({
      check: ready({ set_aside: 6, set_aside_at_limit: 2, set_aside_short: 3, set_aside_ordered_recently: 1 }),
    });

    expect(
      screen.getByText(
        "6 IDs will be set aside (2 at their unit limit, 3 with too little room for one order, 1 ordered recently).",
      ),
    ).toBeTruthy();
  });

  it("says when no unit limit applies to the product, and still gives the units", () => {
    show({
      check: ready({
        set_aside: 1,
        set_aside_at_limit: 0,
        set_aside_ordered_recently: 1,
        units_available: 9,
        limits_apply: false,
        limits: [],
      }),
      requestedUnits: 9,
    });

    expect(screen.getByText("1 ID will be set aside (1 ordered recently).")).toBeTruthy();
    expect(screen.getByText("Units available: 9")).toBeTruthy();
    expect(screen.getByText("No unit limit applies to this product")).toBeTruthy();
    expect(screen.queryByText(SHORT_TITLE)).toBeNull();
  });

  it("never reads a limit's blocked yes-or-no as a count of accounts", () => {
    const { container } = show({ check: ready({ limits: [VIVO, ANY_CATEGORY] }) });

    expect(screen.getByText("Any category")).toBeTruthy();
    expect(container.textContent).not.toMatch(/\baccounts? blocked\b/);
    expect(container.textContent).not.toMatch(/1 account/);
  });

  it("names a limit added for this order only", () => {
    show({
      check: ready({
        limits: [{ ...VIVO, limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, used: 0, remaining: 7 }],
      }),
    });

    const line = screen.getByText("Model · T5x 5G").closest("li");
    expect(line?.textContent).toContain("this order only");
    expect(line?.textContent).toContain("7 left, 0 already used");
  });

  it("warns when the run asks for more units than are available, without blaming a limit", () => {
    show({ requestedUnits: 10 });

    // The whole warning box: its title and what it says.
    const warning = screen.getByText(SHORT_TITLE).closest('[role="status"]');
    expect(warning).not.toBeNull();
    expect(warning?.textContent).toContain(
      "The selected IDs can take 7 units, but the run asks for 10 units, so the last 3 units cannot be ordered. Select more IDs or ask for fewer units.",
    );
    // The shortfall may come from Exclude IDs as much as from a limit.
    expect(warning?.textContent ?? "").not.toMatch(/limit/i);
  });

  it("says nothing while there is nothing to check", () => {
    const { container } = show({ check: { kind: "idle" } });
    expect(container.textContent).toBe("");
  });

  it("says it is checking while the check is on its way", () => {
    show({ check: { kind: "loading" } });
    expect(screen.getByText("Checking unit limits for the selected accounts…")).toBeTruthy();
  });

  it("never says the run can still start when the server refused this order's limit changes", () => {
    const { container } = show({
      check: {
        kind: "error",
        message: "A brand limit for 'vivo' already applies: change that one instead.",
        refusedChanges: true,
      },
    });

    expect(screen.getByRole("alert").textContent).toBe(
      "This order’s limit changes were refused: A brand limit for 'vivo' already applies: change that one instead.",
    );
    expect(container.textContent).not.toMatch(/can still be started/);
    // Asking again would be refused the same way: the change is what has to move.
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
  });

  it("reports a failed check and asks again on request", () => {
    const onRetry = vi.fn();
    show({ check: { kind: "error", message: "Server busy." }, onRetry });

    expect(screen.getByText(/Could not check the unit limits: Server busy\./)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("LimitUsageSummary with Smart ID Selection off", () => {
  it("says each ID is checked at its turn, and still gives the units and the limits", () => {
    show({ smartId: false, check: ready({ limits: [VIVO, ANY_CATEGORY] }) });

    expect(screen.getByText(OFF_LINE)).toBeTruthy();
    expect(screen.getByText("Units available: 7")).toBeTruthy();
    expect(screen.getByText("Brand · vivo")).toBeTruthy();
    expect(screen.getByText("Any category")).toBeTruthy();
    // Nothing is set aside before an off run, so no count of IDs is given.
    expect(screen.queryByText(/set aside/)).toBeNull();
  });

  it("still warns when the run asks for more units than are available", () => {
    show({ smartId: false, requestedUnits: 10 });

    expect(screen.getByText(SHORT_TITLE)).toBeTruthy();
    expect(screen.getByText(/can take 7 units, but the run asks for 10 units, so the last 3 units cannot be ordered/)).toBeTruthy();
  });

  it("says when no unit limit applies", () => {
    show({
      smartId: false,
      requestedUnits: 10,
      check: ready({
        set_aside: 0,
        set_aside_at_limit: 0,
        set_aside_ordered_recently: 0,
        units_available: 10,
        limits_apply: false,
        limits: [],
      }),
    });

    expect(screen.getByText("No unit limit applies to this product")).toBeTruthy();
    expect(screen.getByText("Units available: 10")).toBeTruthy();
    expect(screen.queryByText(SHORT_TITLE)).toBeNull();
  });

  it("says so in every state of the check", () => {
    for (const check of [{ kind: "idle" }, { kind: "loading" }, { kind: "error", message: "x" }] as RunCheckState[]) {
      const view = show({ smartId: false, check });
      expect(screen.getByText(OFF_LINE)).toBeTruthy();
      view.unmount();
    }
  });

  it("changes only what is said when the switch is flipped", () => {
    const view = show();
    view.rerender(summary({ smartId: false }));
    expect(screen.getByText(OFF_LINE)).toBeTruthy();
    expect(screen.queryByText(/set aside/)).toBeNull();
    expect(screen.getByText("Units available: 7")).toBeTruthy();

    view.rerender(summary({ smartId: true }));
    expect(screen.queryByText(OFF_LINE)).toBeNull();
    expect(screen.getByText("3 IDs will be set aside (2 at their unit limit, 1 ordered recently).")).toBeTruthy();
  });
});

describe("LimitUsageSummary in an AI run", () => {
  it("sets nothing aside before the run even with Smart ID on, and still gives the units and the warning", () => {
    show({ aiRun: true, requestedUnits: 10 });

    expect(screen.getByText(AI_LINE)).toBeTruthy();
    expect(screen.getByText("Units available: 7")).toBeTruthy();
    expect(screen.getByText(SHORT_TITLE)).toBeTruthy();
    expect(screen.queryByText(/will be set aside/)).toBeNull();
    expect(screen.queryByText(OFF_LINE)).toBeNull();
  });

  it("says Smart ID is off when it is off", () => {
    show({ aiRun: true, smartId: false, check: { kind: "idle" } });
    expect(screen.getByText(OFF_LINE)).toBeTruthy();
    expect(screen.queryByText(AI_LINE)).toBeNull();
  });
});

describe("setAsideLine", () => {
  const none = { set_aside: 0, set_aside_at_limit: 0, set_aside_short: 0, set_aside_ordered_recently: 0 };

  it("names the count and only the reasons that apply", () => {
    expect(setAsideLine(none)).toBe("No ID will be set aside.");
    expect(setAsideLine({ ...none, set_aside: 1, set_aside_at_limit: 1 })).toBe(
      "1 ID will be set aside (1 at its unit limit).",
    );
    expect(setAsideLine({ ...none, set_aside: 1, set_aside_short: 1 })).toBe(
      "1 ID will be set aside (1 with too little room for one order).",
    );
    expect(setAsideLine({ ...none, set_aside: 5, set_aside_short: 2, set_aside_ordered_recently: 3 })).toBe(
      "5 IDs will be set aside (2 with too little room for one order, 3 ordered recently).",
    );
    expect(
      setAsideLine({ set_aside: 1500, set_aside_at_limit: 1000, set_aside_short: 300, set_aside_ordered_recently: 200 }),
    ).toBe("1,500 IDs will be set aside (1,000 at their unit limit, 300 with too little room for one order, 200 ordered recently).");
  });
});

describe("LimitUsageSummary and IDs that already bought this product", () => {
  it("says how many IDs already bought it, apart from the ones set aside", () => {
    show({ check: ready({ already_bought: 2 }) });

    expect(screen.getByText("2 IDs already bought this product and will be passed over.")).toBeTruthy();
    expect(screen.getByText("3 IDs will be set aside (2 at their unit limit, 1 ordered recently).")).toBeTruthy();
  });

  it("says it with Smart ID Selection off too: the run takes them out either way", () => {
    show({ smartId: false, check: ready({ already_bought: 1 }) });

    expect(screen.getByText("1 ID already bought this product and will be passed over.")).toBeTruthy();
  });

  it("says nothing when none did, or when an older server does not say", () => {
    show({ check: ready({ already_bought: 0 }) });
    cleanup();
    show();

    expect(screen.queryByText(/already bought this product/)).toBeNull();
  });
});

describe("LimitUsageSummary: how many IDs the run will try", () => {
  it("counts every ID the run will not order on, with Smart ID on", () => {
    // Run #960 in small: 10 selected, 2 already bought, 3 set aside.
    show({ check: ready({ already_bought: 2 }) });

    expect(screen.getByText("IDs the run will try: 5 of 10 selected")).toBeTruthy();
  });

  it("with Smart ID off, says the IDs Exclude IDs passes over and counts only those and the bought", () => {
    show({ smartId: false, check: ready({ already_bought: 2 }) });

    expect(screen.getByText("1 ID ordered recently (Exclude IDs) and will be passed over.")).toBeTruthy();
    // The two at their unit limit are still tried (and skipped) at their turn.
    expect(screen.getByText("IDs the run will try: 7 of 10 selected")).toBeTruthy();
    expect(screen.queryByText(/set aside/)).toBeNull();
  });

  it("says nothing of it when no ID is left out", () => {
    show({
      check: ready({ set_aside: 0, set_aside_at_limit: 0, set_aside_ordered_recently: 0, already_bought: 0 }),
    });

    expect(screen.queryByText(/IDs the run will try/)).toBeNull();
    expect(screen.queryByText(/ordered recently \(Exclude IDs\)/)).toBeNull();
  });
});
