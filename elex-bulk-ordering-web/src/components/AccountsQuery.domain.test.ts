/** The email-domain filter (step B): carried in the filter, lower-cased, and part of its key. */
import { describe, expect, it } from "vitest";
import { EMPTY_QUERY, filterKey, isFilterEmpty, isQueryNarrowed, toFilterInput } from "./AccountsQuery";

describe("email domain filter", () => {
  it("goes into the filter lower-cased", () => {
    expect(toFilterInput({ ...EMPTY_QUERY, domain: " SonicLip.in " }).domains).toEqual(["soniclip.in"]);
  });

  it("is left out when blank", () => {
    expect(toFilterInput(EMPTY_QUERY).domains).toBeUndefined();
  });

  it("narrows the filter and changes its key", () => {
    const filter = toFilterInput({ ...EMPTY_QUERY, domain: "a.in" });
    expect(isFilterEmpty(filter)).toBe(false);
    expect(isQueryNarrowed({ ...EMPTY_QUERY, domain: "a.in" })).toBe(true);
    expect(filterKey(filter)).not.toBe(filterKey(toFilterInput(EMPTY_QUERY)));
  });
});
