/**
 * `mode` and `qty_mode` on POST /jobs/order (step A2): sent only when they
 * differ from the server's defaults, so a real "upto" run goes as before.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { previewOrderCards, startOrderJob } from "./api";

const base = {
  product_link: "https://www.flipkart.com/p/itm1",
  qty: 1,
  target: 500,
  no_cap: false,
  account_ids: [1],
  payment_mode: "cod" as const,
  cards_file: null,
};

function sentForm(): FormData {
  const call = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
  return call[1].body as FormData;
}

afterEach(() => vi.unstubAllGlobals());

describe("startOrderJob modes", () => {
  it.each([
    [{}, null, null],
    [{ mode: "real", qty_mode: "upto" }, null, null],
    [{ mode: "mock", qty_mode: "minimum" }, "mock", "minimum"],
  ])("%j sends mode=%s qty_mode=%s", async (extra, mode, qtyMode) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, ...(extra as object) });
    expect(sentForm().get("mode")).toBe(mode);
    expect(sentForm().get("qty_mode")).toBe(qtyMode);
  });
});

describe("Skip cards already used on the wire", () => {
  it("goes as card_exclude_used=true with a card run, never when off or with Cash on Delivery", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, payment_mode: "card", cards_file: new File(["x"], "c.csv"), card_exclude_used: true });
    expect(sentForm().get("card_exclude_used")).toBe("true");
    vi.unstubAllGlobals();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, payment_mode: "card", cards_file: new File(["x"], "c.csv"), card_exclude_used: false });
    expect(sentForm().get("card_exclude_used")).toBeNull();
    vi.unstubAllGlobals();

    // The server refuses card rules on a Cash-on-Delivery run: never sent there.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, card_exclude_used: true });
    expect(sentForm().get("card_exclude_used")).toBeNull();
  });

  it("goes with the card pairing preview when on", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ accounts: [] }), { status: 200 })));
    await previewOrderCards({ cards_file: new File(["x"], "c.csv"), account_ids: [1], card_exclude_used: true });
    expect(sentForm().get("card_exclude_used")).toBe("true");
    vi.unstubAllGlobals();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ accounts: [] }), { status: 200 })));
    await previewOrderCards({ cards_file: new File(["x"], "c.csv"), account_ids: [1] });
    expect(sentForm().get("card_exclude_used")).toBeNull();
  });
});

describe("card filters on the wire", () => {
  it("go with a card run and never with Cash on Delivery", async () => {
    const rules = { card_cooldown_days: 3, card_max_uses: 2, card_least_used_days: null, card_exclude_failed_days: 7 };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, payment_mode: "card", cards_file: new File(["x"], "c.csv"), ...rules });
    expect(sentForm().get("card_cooldown_days")).toBe("3");
    expect(sentForm().get("card_max_uses")).toBe("2");
    expect(sentForm().get("card_least_used_days")).toBeNull();
    expect(sentForm().get("card_exclude_failed_days")).toBe("7");
    vi.unstubAllGlobals();

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 1 }), { status: 201 })));
    await startOrderJob({ ...base, ...rules });
    expect(sentForm().get("card_cooldown_days")).toBeNull();
  });
});
