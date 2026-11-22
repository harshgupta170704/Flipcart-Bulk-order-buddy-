/** AI mode on the wire (step E): its pacing goes only with mode "ai". */
import { afterEach, describe, expect, it, vi } from "vitest";
import { startOrderJob } from "./api";

const base = {
  product_link: "https://www.flipkart.com/p/itm1",
  qty: 1,
  target: 500,
  no_cap: false,
  account_ids: [1],
  payment_mode: "cod" as const,
  cards_file: null,
};
const ai = { max_interval_minutes: 2, min_attempts: 5, scale_up_pct: 30, run_duration: "6h", rehearsal: true };

function sent(): FormData {
  const call = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
  return call[1].body as FormData;
}

afterEach(() => vi.unstubAllGlobals());

describe("AI pacing", () => {
  it("goes with an AI run", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, mode: "ai", ai });
    expect(sent().get("mode")).toBe("ai");
    expect(sent().get("ai_min_attempts")).toBe("5");
    expect(sent().get("ai_scale_up_pct")).toBe("30");
    expect(sent().get("ai_max_interval_minutes")).toBe("2");
    expect(sent().get("ai_run_duration")).toBe("6h");
    expect(sent().get("ai_rehearsal")).toBe("true");
  });

  it("never goes with a mock run", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, mode: "mock", ai });
    expect(sent().get("ai_min_attempts")).toBeNull();
  });
});
