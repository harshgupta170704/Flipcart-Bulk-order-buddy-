import { describe, expect, it } from "vitest";
import { EMPTY_QUANTITY, validateQuantity } from "./QuantityFields";

// Audit of 19 Nov 2026: Max Delivery Days is required, 3 unless changed.
describe("Max Delivery Days", () => {
  it("starts at 3 and is valid as it is", () => {
    expect(EMPTY_QUANTITY.maxDeliveryDays).toBe("3");
    expect(validateQuantity(EMPTY_QUANTITY).maxDeliveryDays).toBeUndefined();
  });

  it("cannot be left blank", () => {
    expect(validateQuantity({ ...EMPTY_QUANTITY, maxDeliveryDays: "" }).maxDeliveryDays).toMatch(/most days/);
  });

  it("must be a whole number of days", () => {
    expect(validateQuantity({ ...EMPTY_QUANTITY, maxDeliveryDays: "0" }).maxDeliveryDays).toBeDefined();
    expect(validateQuantity({ ...EMPTY_QUANTITY, maxDeliveryDays: "2.5" }).maxDeliveryDays).toBeDefined();
  });
});
