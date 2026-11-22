/**
 * The picker opens on the row marked default and on nothing else: with none
 * marked it must wait for a choice, as the server refuses a run that names no
 * address — a silent "first row" would bring the removed fallback back.
 */
import { describe, expect, it } from "vitest";
import { defaultRow } from "./AddressBookData";

const unmarkedA = { id: 1, label: "a", is_default: false };
const unmarkedB = { id: 2, label: "b", is_default: false };
const markedB = { id: 2, label: "b", is_default: true };

describe("defaultRow", () => {
  it("returns the row marked default", () => {
    expect(defaultRow([unmarkedA, markedB])).toBe(markedB);
  });

  it("returns null when nothing is marked — never the first row", () => {
    expect(defaultRow([unmarkedA, unmarkedB])).toBeNull();
  });

  it("returns null for an empty list", () => {
    expect(defaultRow([])).toBeNull();
  });
});
