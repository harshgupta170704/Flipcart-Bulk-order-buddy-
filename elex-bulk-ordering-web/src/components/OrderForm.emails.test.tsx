import { describe, expect, it } from "vitest";
import { emailsInText } from "./OrderForm";

describe("Upload CSV of email ids", () => {
  it("finds every address in any column, once, lower-cased", () => {
    const csv = "Email,Note\nA@X.com,first\nb@y.in,\n a@x.com ,again\nnot-an-email\n";
    expect(emailsInText(csv)).toEqual(["a@x.com", "b@y.in"]);
    expect(emailsInText("nothing here")).toEqual([]);
  });
});
