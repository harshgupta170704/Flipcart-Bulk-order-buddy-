/**
 * The order's variant and this order's limit changes (Smart ID, part 2): the
 * variant read from the link as the run reads it, the three reasons a variant
 * limit cannot apply, the box's checks (with who may loosen), what goes on the
 * wire, what "Run again" puts back, and how a run's settings say it.
 */
import { describe, expect, it } from "vitest";
import {
  addableScopes,
  changesFromRun,
  hasChanges,
  isChanged,
  limitOverrides,
  NO_LIMIT_CHANGES,
  ONLY_ADMIN_LOOSENS,
  orderVariant,
  overrideLines,
  pidFromLink,
  validateLimitChanges,
  variantNote,
  type AddedLimit,
  type LimitChanges,
  type ProductValues,
} from "./limitChanges";
import type { ApplicableLimit } from "./types";

const LINK = "https://www.flipkart.com/vivo-t5x-5g/p/itm5420c578ec2df?pid=MOBHNDTD57UJUK9Q&lid=LSTMOB";
const VIVO: ApplicableLimit = { limit_id: 4, scope: "brand", value: "vivo", max_units: 2, window_days: 30 };
const MOBILES: ApplicableLimit = { limit_id: 9, scope: "category", value: "", max_units: 20, window_days: 31 };
const PRODUCT: ProductValues = {
  brand: "vivo",
  category: "Mobiles",
  seller: "SAPHEDretail",
  variant: "MOBHNDTD57UJUK9Q",
  title: "vivo T5x 5G (Glossy Black, 128 GB)",
};

function changes(over: Partial<LimitChanges> = {}): LimitChanges {
  return { forLink: LINK, edits: {}, added: [], ...over };
}

function added(over: Partial<AddedLimit> = {}): AddedLimit {
  return { key: 1, scope: "model", value: "T5x 5G", maxUnits: "1", windowDays: "30", ...over };
}

describe("the variant", () => {
  it("is the link's pid, upper-cased, from www and dl.flipkart.com links alike", () => {
    expect(pidFromLink(LINK)).toBe("MOBHNDTD57UJUK9Q");
    expect(pidFromLink("https://dl.flipkart.com/dl/vivo-t5x-5g/p/itm5420c578ec2df?pid=mobhndtd57ujuk9q")).toBe(
      "MOBHNDTD57UJUK9Q",
    );
    expect(pidFromLink("https://www.flipkart.com/vivo/p/itm1/?pid=MOBHNDTD57UJUK9Q")).toBe("MOBHNDTD57UJUK9Q");
  });

  it("is none for a link without a usable pid", () => {
    expect(pidFromLink("https://dl.flipkart.com/s/!abcDEF")).toBeNull();
    expect(pidFromLink("https://www.flipkart.com/p/itm1?pid=AB12")).toBeNull();
    expect(pidFromLink(`https://www.flipkart.com/p/itm1?pid=${"A".repeat(41)}`)).toBeNull();
    expect(pidFromLink("https://www.flipkart.com/p/itm1?pid=MOB-HNDTD57")).toBeNull();
    expect(pidFromLink("not a link")).toBeNull();
  });

  it("names the reason a variant limit cannot apply, and nothing without a product", () => {
    expect(orderVariant({ productLink: LINK })).toEqual({ variant: "MOBHNDTD57UJUK9Q", reason: null });
    expect(orderVariant({ productLink: "" })).toEqual({ variant: null, reason: null });
    expect(orderVariant({ productLink: "https://dl.flipkart.com/s/!abcDEF" }).reason).toBe("the link names no variant");
    expect(orderVariant({ productLink: LINK, size: "UK 8" }).reason).toBe("a size is picked");
    expect(orderVariant({ productLink: LINK, extraLinks: ["https://www.flipkart.com/p/itm2?pid=ACCABCDEF12"] }).reason).toBe(
      "it has several products",
    );
    // A box left blank by "+ Add product URL" adds no product.
    expect(orderVariant({ productLink: LINK, extraLinks: ["  "] }).variant).toBe("MOBHNDTD57UJUK9Q");
  });

  it("says why in one line", () => {
    expect(variantNote(orderVariant({ productLink: LINK, size: "M" }))).toBe(
      "Variant limits cannot apply to this order: a size is picked.",
    );
    expect(variantNote(orderVariant({ productLink: LINK, extraLinks: ["https://x.in/p?pid=ABCDEFG"] }))).toBe(
      "Variant limits cannot apply to this order: it has several products.",
    );
    expect(variantNote(orderVariant({ productLink: "https://www.flipkart.com/p/itm1" }))).toBe(
      "Variant limits cannot apply to this order: the link names no variant.",
    );
    expect(variantNote(orderVariant({ productLink: LINK }))).toBeNull();
  });
});

describe("what counts as a change", () => {
  it("is a line that says anything other than the saved limit", () => {
    expect(isChanged(VIVO, undefined)).toBe(false);
    expect(isChanged(VIVO, { maxUnits: "2", windowDays: "30", enabled: true })).toBe(false);
    expect(isChanged(VIVO, { maxUnits: "02", windowDays: " 30 ", enabled: true })).toBe(false);
    expect(isChanged(VIVO, { maxUnits: "1", windowDays: "30", enabled: true })).toBe(true);
    expect(isChanged(VIVO, { maxUnits: "2", windowDays: "60", enabled: true })).toBe(true);
    expect(isChanged(VIVO, { maxUnits: "2", windowDays: "30", enabled: false })).toBe(true);
    expect(isChanged(VIVO, { maxUnits: "", windowDays: "30", enabled: true })).toBe(true);
  });

  it("counts added lines and changed saved lines that still apply", () => {
    expect(hasChanges(NO_LIMIT_CHANGES, [VIVO])).toBe(false);
    expect(hasChanges(changes({ edits: { 4: { maxUnits: "2", windowDays: "30", enabled: true } } }), [VIVO])).toBe(false);
    expect(hasChanges(changes({ edits: { 4: { maxUnits: "1", windowDays: "30", enabled: true } } }), [VIVO])).toBe(true);
    expect(hasChanges(changes({ edits: { 4: { maxUnits: "1", windowDays: "30", enabled: true } } }), [MOBILES])).toBe(false);
    expect(hasChanges(changes({ added: [added()] }), [])).toBe(true);
    // Not read yet: every edit counts.
    expect(hasChanges(changes({ edits: { 4: { maxUnits: "2", windowDays: "30", enabled: true } } }), null)).toBe(true);
  });
});

describe("the box's checks", () => {
  it("passes tightening by anyone, and nothing at all", () => {
    expect(validateLimitChanges(NO_LIMIT_CHANGES, [VIVO], PRODUCT, false)).toBeNull();
    const tighter = changes({ edits: { 4: { maxUnits: "1", windowDays: "60", enabled: true } } });
    expect(validateLimitChanges(tighter, [VIVO], PRODUCT, false)).toBeNull();
  });

  it("wants whole units of 1 or more and days from 1 to 3650", () => {
    const bad = changes({
      edits: { 4: { maxUnits: "0", windowDays: "3651", enabled: true }, 9: { maxUnits: "1.5", windowDays: "0", enabled: true } },
    });
    const errors = validateLimitChanges(bad, [VIVO, MOBILES], PRODUCT, true);
    expect(errors?.saved[4]).toEqual({ units: "Units: a whole number, 1 or more.", days: "Days: a whole number from 1 to 3650." });
    expect(errors?.saved[9]?.units).toBe("Units: a whole number, 1 or more.");
    expect(errors?.saved[9]?.days).toBe("Days: a whole number from 1 to 3650.");
  });

  it("lets only an admin loosen a limit: more units, fewer days, or a line switched off", () => {
    for (const edit of [
      { maxUnits: "3", windowDays: "30", enabled: true },
      { maxUnits: "2", windowDays: "7", enabled: true },
      { maxUnits: "2", windowDays: "30", enabled: false },
    ]) {
      const loose = changes({ edits: { 4: edit } });
      expect(validateLimitChanges(loose, [VIVO], PRODUCT, false)?.saved[4]?.loosen).toBe(ONLY_ADMIN_LOOSENS);
      expect(validateLimitChanges(loose, [VIVO], PRODUCT, true)).toBeNull();
    }
  });

  it("needs a value on every added line, from the product or typed for a model", () => {
    const empty: ProductValues = { brand: "", category: "", seller: "", variant: null, title: PRODUCT.title };
    const lines = (["brand", "category", "seller", "variant", "model"] as const).map((scope, index) =>
      added({ key: index + 1, scope, value: "" }),
    );
    const errors = validateLimitChanges(changes({ added: lines }), [], empty, true);
    expect(errors?.added[1]?.value).toBe("Fill in Brand above: a brand line takes the product's brand.");
    expect(errors?.added[2]?.value).toBe("Fill in Category above: a category line takes the product's category.");
    expect(errors?.added[3]?.value).toBe("Choose a seller in 1. Product: a seller line takes the run's seller.");
    expect(errors?.added[4]?.value).toBe("This order names no variant, so a variant line cannot apply.");
    expect(errors?.added[5]?.value).toBe("Type a name the product's title contains.");
    // With the product's values, the same lines (but the model's) pass.
    const filled = validateLimitChanges(changes({ added: lines.slice(0, 4) }), [], PRODUCT, true);
    expect(filled).toBeNull();
  });

  it("wants a model name the product's title contains, ignoring case", () => {
    expect(validateLimitChanges(changes({ added: [added({ value: "t5X 5g" })] }), [], PRODUCT, false)).toBeNull();
    expect(validateLimitChanges(changes({ added: [added({ value: "T6 Pro" })] }), [], PRODUCT, false)?.added[1]?.value).toBe(
      "The product's title does not contain “T6 Pro”.",
    );
    expect(
      validateLimitChanges(changes({ added: [added()] }), [], { ...PRODUCT, title: "" }, false)?.added[1]?.value,
    ).toBe("The product's title is not read yet, so a model line cannot be checked.");
  });

  it("checks added lines' units and days too", () => {
    const errors = validateLimitChanges(changes({ added: [added({ maxUnits: "", windowDays: "4000" })] }), [], PRODUCT, true);
    expect(errors?.added[1]).toEqual({ units: "Units: a whole number, 1 or more.", days: "Days: a whole number from 1 to 3650." });
  });

  it("leaves alone a line put back as it was saved, and one that no longer applies", () => {
    const same = changes({ edits: { 4: { maxUnits: "2", windowDays: "30", enabled: true }, 9: { maxUnits: "x", windowDays: "", enabled: true } } });
    expect(validateLimitChanges(same, [VIVO], PRODUCT, false)).toBeNull();
  });
});

describe("no repeated limit", () => {
  it("offers a level no other added line has, and none a saved limit already covers for the product's value", () => {
    // A saved "brand vivo" covers vivo; a saved "Any category" is another limit than "Mobiles".
    expect(addableScopes(NO_LIMIT_CHANGES, [VIVO, MOBILES], PRODUCT)).toEqual(["category", "model", "variant", "seller"]);
    const one = changes({ added: [added({ key: 1, scope: "category", value: "" })] });
    expect(addableScopes(one, [VIVO, MOBILES], PRODUCT)).toEqual(["model", "variant", "seller"]);
    // The line asking keeps its own level.
    expect(addableScopes(one, [VIVO, MOBILES], PRODUCT, 1)).toEqual(["category", "model", "variant", "seller"]);
    // Saved values match ignoring case; only another model line rules out a model line.
    const savedModel: ApplicableLimit = { limit_id: 12, scope: "model", value: "t5x 5g", max_units: 1, window_days: 30 };
    const upper: ApplicableLimit = { ...VIVO, value: "VIVO" };
    expect(addableScopes(NO_LIMIT_CHANGES, [upper, savedModel], PRODUCT)).toEqual(["category", "model", "variant", "seller"]);
  });

  it("marks an added line that repeats a limit that applies, or another added line", () => {
    const repeats = changes({
      added: [
        added({ key: 1, scope: "brand", value: "" }),
        added({ key: 2, scope: "model", value: "T5x 5G" }),
        added({ key: 3, scope: "model", value: "t5x 5g" }),
      ],
    });
    const savedModel: ApplicableLimit = { limit_id: 12, scope: "model", value: "Glossy", max_units: 1, window_days: 30 };
    const errors = validateLimitChanges(repeats, [{ ...VIVO, value: "VIVO" }, savedModel], PRODUCT, true);
    expect(errors?.added[1]?.value).toBe("A brand limit for 'vivo' already applies: change that one instead.");
    expect(errors?.added[2]).toBeUndefined();
    expect(errors?.added[3]?.value).toBe("A model limit for 't5x 5g' is already added for this order.");
  });
});

describe("a switched-off line, and units", () => {
  it("never checks the fields of a line switched off: it goes with its saved numbers", () => {
    const off = changes({ edits: { 4: { maxUnits: "", windowDays: "0", enabled: false } } });
    expect(validateLimitChanges(off, [VIVO], PRODUCT, true)).toBeNull();
    expect(limitOverrides(off, [VIVO], PRODUCT)).toEqual([{ limit_id: 4, max_units: 2, window_days: 30, enabled: false }]);
    // Switching off still loosens, which only an admin may do.
    expect(validateLimitChanges(off, [VIVO], PRODUCT, false)?.saved[4]).toEqual({ loosen: ONLY_ADMIN_LOOSENS });
  });

  it("puts no ceiling on units for an admin, and says the floor plainly", () => {
    const many = changes({ edits: { 4: { maxUnits: "5000", windowDays: "30", enabled: true } } });
    expect(validateLimitChanges(many, [VIVO], PRODUCT, true)).toBeNull();
    const none = changes({ edits: { 4: { maxUnits: "0", windowDays: "30", enabled: true } } });
    expect(validateLimitChanges(none, [VIVO], PRODUCT, true)?.saved[4]?.units).toBe("Units: a whole number, 1 or more.");
  });
});

describe("on the wire", () => {
  it("sends a changed saved limit by its id with units, days and on/off, never its value", () => {
    const list = limitOverrides(
      changes({ edits: { 4: { maxUnits: "1", windowDays: "60", enabled: true }, 9: { maxUnits: "20", windowDays: "31", enabled: false } } }),
      [VIVO, MOBILES],
      PRODUCT,
    );
    expect(list).toEqual([
      { limit_id: 4, max_units: 1, window_days: 60, enabled: true },
      { limit_id: 9, max_units: 20, window_days: 31, enabled: false },
    ]);
  });

  it("leaves out unchanged lines and limits that no longer apply, and sends nothing for no change", () => {
    const edits = { 4: { maxUnits: "2", windowDays: "30", enabled: true }, 9: { maxUnits: "5", windowDays: "31", enabled: true } };
    expect(limitOverrides(changes({ edits }), [VIVO], PRODUCT)).toEqual([]);
    expect(limitOverrides(NO_LIMIT_CHANGES, [VIVO], PRODUCT)).toEqual([]);
    // Not read yet: the server is the judge of every edit.
    expect(limitOverrides(changes({ edits }), null, PRODUCT)).toHaveLength(2);
  });

  it("leaves out only the lines the box marks, and sends the rest", () => {
    const mixed = changes({
      edits: { 4: { maxUnits: "1", windowDays: "30", enabled: true }, 9: { maxUnits: "20", windowDays: "4000", enabled: true } },
      added: [added({ key: 1, scope: "model", value: "T6 Pro" }), added({ key: 2, scope: "variant", value: "" })],
    });
    const errors = validateLimitChanges(mixed, [VIVO, MOBILES], PRODUCT, true);
    expect(Object.keys(errors?.saved ?? {})).toEqual(["9"]);
    expect(Object.keys(errors?.added ?? {})).toEqual(["1"]);
    expect(limitOverrides(mixed, [VIVO, MOBILES], PRODUCT, errors)).toEqual([
      { limit_id: 4, max_units: 1, window_days: 30, enabled: true },
      { limit_id: null, scope: "variant", value: "MOBHNDTD57UJUK9Q", max_units: 1, window_days: 30, enabled: true },
    ]);
  });

  it("sends an added line with its scope and the product's value, or the model name typed", () => {
    const list = limitOverrides(
      changes({
        added: [
          added({ key: 1, scope: "brand", value: "ignored", maxUnits: "3", windowDays: "30" }),
          added({ key: 2, scope: "variant", value: "", maxUnits: "1", windowDays: "90" }),
          added({ key: 3, scope: "model", value: " T5x 5G ", maxUnits: "1", windowDays: "30" }),
        ],
      }),
      [],
      PRODUCT,
    );
    expect(list).toEqual([
      { limit_id: null, scope: "brand", value: "vivo", max_units: 3, window_days: 30, enabled: true },
      { limit_id: null, scope: "variant", value: "MOBHNDTD57UJUK9Q", max_units: 1, window_days: 90, enabled: true },
      { limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, window_days: 30, enabled: true },
    ]);
  });
});

describe("Run again", () => {
  it("puts back a run's changes for its product link", () => {
    const restored = changesFromRun(
      [
        { limit_id: 4, max_units: 3, window_days: 30, enabled: true },
        { limit_id: 9, max_units: 20, window_days: 31, enabled: false },
        { limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, window_days: 30, enabled: true },
        { limit_id: null, scope: "brand", value: "vivo", max_units: 5, window_days: 30, enabled: true },
        { limit_id: null, scope: "nonsense", value: "x", max_units: 1, window_days: 30 },
        "not a change",
      ],
      ` ${LINK} `,
    );
    expect(restored?.forLink).toBe(LINK);
    expect(restored?.edits).toEqual({
      4: { maxUnits: "3", windowDays: "30", enabled: true },
      9: { maxUnits: "20", windowDays: "31", enabled: false },
    });
    expect(restored?.added.map(({ scope, value, maxUnits, windowDays }) => ({ scope, value, maxUnits, windowDays }))).toEqual([
      { scope: "model", value: "T5x 5G", maxUnits: "1", windowDays: "30" },
      // The product's value is taken afresh, as for a line added by hand.
      { scope: "brand", value: "", maxUnits: "5", windowDays: "30" },
    ]);
  });

  it("puts back nothing from a run that saved none", () => {
    expect(changesFromRun(undefined, LINK)).toBeNull();
    expect(changesFromRun([], LINK)).toBeNull();
    expect(changesFromRun([{ limit_id: 4 }], LINK)).toBeNull();
  });
});

describe("in a run's settings", () => {
  it("says each change in one line", () => {
    expect(
      overrideLines([
        { limit_id: 4, scope: "brand", value: "vivo", max_units: 3, window_days: 30, enabled: true, saved_max_units: 2, saved_window_days: 30 },
        { limit_id: null, scope: "model", value: "T5x 5G", max_units: 1, window_days: 30, enabled: true },
        { limit_id: 9, scope: "category", value: "Mobile", max_units: 20, window_days: 31, enabled: false },
        { limit_id: 11, scope: "brand", value: "", max_units: 10, window_days: 1, enabled: true },
      ]),
    ).toEqual([
      "brand 'vivo': 3 units per 30 days (saved: 2 per 30)",
      "added for this order: model 'T5x 5G' 1 unit per 30 days",
      "switched off: category 'Mobile'",
      "any brand: 10 units per 1 day",
    ]);
  });

  it("names a change saved without its limit's name by the limit's id", () => {
    expect(overrideLines([{ limit_id: 4, max_units: 1, window_days: 30, enabled: true }])).toEqual([
      "limit #4: 1 unit per 30 days",
    ]);
    expect(overrideLines([{ limit_id: 9, max_units: 1, window_days: 30, enabled: false }])).toEqual(["switched off: limit #9"]);
    expect(overrideLines(null)).toEqual([]);
    expect(overrideLines(["junk", { limit_id: 4 }])).toEqual([]);
  });
});
