// @vitest-environment jsdom
/**
 * The order run's multipart body is the contract with POST /jobs/order in
 * backend/app/api/jobs.py, which refuses a run that states no price cap and
 * one that states both a cap and "no cap" (audit F-23). So the target travels
 * only when there is one, and `no_cap` only when it was ticked — never an
 * empty target, never `no_cap=false` beside a cap.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startOrderJob } from "./api";
import type { OrderJobInput } from "./types";

const base: OrderJobInput = {
  product_link: "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001",
  qty: 1,
  target: 18250,
  no_cap: false,
  account_ids: [7, 8],
  payment_mode: "cod",
  cards_file: null,
  address_id: 1,
  gst_id: null,
};

describe("startOrderJob's price cap fields", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ id: 41 }), { status: 201 }));
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

  it("sends a typed target as the cap and no no_cap field", async () => {
    await startOrderJob(base);

    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/jobs/order");
    expect(sent().get("target")).toBe("18250");
    expect(sent().has("no_cap")).toBe(false);
  });

  it("sends no_cap=true and no target when No price cap is ticked", async () => {
    await startOrderJob({ ...base, target: null, no_cap: true });

    expect(sent().get("no_cap")).toBe("true");
    expect(sent().has("target")).toBe(false);
  });

  it("sends neither for a run that stated nothing, so the server can refuse it by name", async () => {
    await startOrderJob({ ...base, target: null, no_cap: false });

    expect(sent().has("target")).toBe(false);
    expect(sent().has("no_cap")).toBe(false);
  });
});
