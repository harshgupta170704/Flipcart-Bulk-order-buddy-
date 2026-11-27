// @vitest-environment jsdom
/**
 * Smart ID Selection in the order run's multipart body (Smart ID, part 1).
 * Unlike the yes-or-no fields around it, `smart_id` travels on every run, on
 * or off: POST /jobs/order reads a missing one as off, so leaving out a true
 * one would turn the switch off without anyone asking. The renamed "Spread
 * orders across accounts" keeps its field, sent only when on.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startOrderJob } from "./api";
import type { OrderJobInput } from "./types";

const base: OrderJobInput = {
  product_link: "https://www.flipkart.com/vivo-t5-lite-5g/p/itm5420c578ec2df?pid=MOBHQSFF6MZK4YNP",
  qty: 1,
  target: 18250,
  no_cap: false,
  account_ids: [7, 8],
  payment_mode: "cod",
  cards_file: null,
  address_id: 1,
  gst_id: null,
};

describe("startOrderJob's smart_id field", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ id: 90 }), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** The multipart body of the last request. */
  function sent(): FormData {
    const init = fetchMock.mock.calls[fetchMock.mock.calls.length - 1][1] as RequestInit;
    return init.body as FormData;
  }

  it("sends smart_id=false when the switch is off", async () => {
    await startOrderJob({ ...base, smart_id: false });

    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/jobs/order");
    expect(sent().getAll("smart_id")).toEqual(["false"]);
  });

  it("sends smart_id=true when the switch is on", async () => {
    await startOrderJob({ ...base, smart_id: true });

    expect(sent().getAll("smart_id")).toEqual(["true"]);
  });

  it("still sends smart_id=false when a caller leaves it out, never nothing", async () => {
    await startOrderJob(base);

    expect(sent().getAll("smart_id")).toEqual(["false"]);
  });

  it("sends this order's limit changes as JSON text, and nothing when there are none", async () => {
    const changes = [
      { limit_id: 4, max_units: 1, window_days: 30, enabled: true },
      { limit_id: null, scope: "model" as const, value: "T5x 5G", max_units: 1, window_days: 30, enabled: true },
    ];
    await startOrderJob({ ...base, smart_id: true, limit_overrides: changes });
    expect(JSON.parse(String(sent().get("limit_overrides")))).toEqual(changes);

    for (const none of [[], null, undefined]) {
      await startOrderJob({ ...base, smart_id: true, limit_overrides: none });
      expect(sent().has("limit_overrides")).toBe(false);
    }
  });

  it("sends smart_selection only when Spread orders across accounts is on, apart from smart_id", async () => {
    await startOrderJob({ ...base, smart_id: false, smart_selection: true });
    expect(sent().get("smart_selection")).toBe("true");
    expect(sent().get("smart_id")).toBe("false");

    await startOrderJob({ ...base, smart_id: true, smart_selection: false });
    expect(sent().has("smart_selection")).toBe(false);
    expect(sent().get("smart_id")).toBe("true");
  });
});
