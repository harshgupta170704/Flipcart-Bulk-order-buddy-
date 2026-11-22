import { describe, expect, it } from "vitest";
import { hereAsReturnTo, safeReturnTo } from "./returnTo";

describe("safeReturnTo", () => {
  it("accepts a path inside the app, with its query and hash", () => {
    expect(safeReturnTo("/order-units?job_id=12")).toBe("/order-units?job_id=12");
    expect(safeReturnTo("/alerts?kind=delivery_overdue#top")).toBe("/alerts?kind=delivery_overdue#top");
  });

  it("refuses anything that could leave the app or loop", () => {
    expect(safeReturnTo("https://evil.example/")).toBeNull();
    expect(safeReturnTo("//evil.example/x")).toBeNull();
    expect(safeReturnTo("/\\evil.example")).toBeNull();
    expect(safeReturnTo("javascript:alert(1)")).toBeNull();
    expect(safeReturnTo("/login")).toBeNull();
    expect(safeReturnTo("/login?x=1")).toBeNull();
    expect(safeReturnTo("")).toBeNull();
    expect(safeReturnTo(null)).toBeNull();
    expect(safeReturnTo(undefined)).toBeNull();
  });

  it("turns the current location into a from value", () => {
    expect(hereAsReturnTo({ pathname: "/order-units", search: "?job_id=12", hash: "" })).toBe("/order-units?job_id=12");
  });
});
