// @vitest-environment jsdom
/**
 * Who sells the product, in the preview card (wave 2, package A): every
 * seller the sellers page listed with what is known of its offer, the default
 * one marked; a quiet note when the list could not be read; nothing at all
 * from a preview that carries no seller fields.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ProductPreview as ProductPreviewData } from "../lib/types";
import { ProductPreview, sameSeller, sellerSummary } from "./ProductPreview";

const BASE: ProductPreviewData = {
  url: "https://www.flipkart.com/pigeon-special-3-l-inner-lid-pressure-cooker/p/itm0c2cd3b721e22",
  name: "Pigeon Special 3 L Inner Lid Pressure Cooker",
  brand: "Pigeon",
  category: null,
  color: null,
  image: null,
  sku: null,
  price: 749,
  currency: "INR",
  availability: null,
  in_stock: true,
  rating: null,
  rating_count: null,
  seller: null,
  error: null,
};

afterEach(cleanup);

function renderPreview(preview: ProductPreviewData) {
  render(<ProductPreview preview={preview} loading={false} error={null} />);
}

describe("ProductPreview sellers", () => {
  it("lists every seller with its offer, the default one marked", () => {
    renderPreview({
      ...BASE,
      default_seller: "MYTHANGLORYRetail",
      sellers: [
        { name: "MYTHANGLORYRetail", price: 749, rating: 4.3, cod: true, buy_now: true },
        { name: "HatmanRetails", price: 20499, rating: null, cod: false, buy_now: false },
      ],
      sellers_error: null,
    });

    expect(screen.getByText("Sellers (2)")).toBeTruthy();
    const rows = within(screen.getByRole("list", { name: "Sellers of this product" })).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "MYTHANGLORYRetail (default) — ₹749.00 · rated 4.3 · Cash on Delivery",
      "HatmanRetails — ₹20,499.00 · no Cash on Delivery · Add to cart only",
    ]);
  });

  it("calls a single seller the seller", () => {
    renderPreview({
      ...BASE,
      default_seller: "HatmanRetails",
      sellers: [{ name: "HatmanRetails", price: 20499, rating: null, cod: null, buy_now: null }],
    });

    expect(screen.getByText("Seller")).toBeTruthy();
    expect(screen.getByRole("listitem").textContent).toBe("HatmanRetails (default) — ₹20,499.00");
  });

  it("names the default seller when no list was read", () => {
    renderPreview({ ...BASE, default_seller: "MYTHANGLORYRetail", sellers: [] });

    expect(screen.queryByRole("list", { name: "Sellers of this product" })).toBeNull();
    expect(screen.getByText("MYTHANGLORYRetail")).toBeTruthy();
  });

  it("says quietly when the sellers could not be read", () => {
    renderPreview({ ...BASE, sellers: [], sellers_error: "the sellers of this product could not be read" });

    expect(
      screen.getByText("The sellers of this product could not be read. A seller’s name can still be typed below."),
    ).toBeTruthy();
    // The product itself still reads as usual; this is not the failed-read alert.
    expect(screen.queryByText("Could not read this product page")).toBeNull();
    expect(screen.getByText("Pigeon Special 3 L Inner Lid Pressure Cooker")).toBeTruthy();
  });

  it("shows no seller section for a preview without seller fields", () => {
    renderPreview(BASE);

    expect(screen.queryByRole("list", { name: "Sellers of this product" })).toBeNull();
    expect(screen.queryByText(/could not be read/)).toBeNull();
  });
});

describe("ProductPreview price", () => {
  it("reads like every other amount in the app: ₹, Indian grouping, two decimals (NFR-010)", () => {
    renderPreview({ ...BASE, price: 129900 });

    expect(screen.getByText("Price").nextElementSibling?.textContent).toBe("₹1,29,900.00");
    cleanup();
    renderPreview({ ...BASE, price: 16499.5 });
    expect(screen.getByText("Price").nextElementSibling?.textContent).toBe("₹16,499.50");
  });

  it("leaves the price out when the page did not give one", () => {
    renderPreview({ ...BASE, price: null });

    expect(screen.queryByText("Price")).toBeNull();
  });
});

describe("seller helpers", () => {
  it("compares names as the server does", () => {
    expect(sameSeller("TBL Online", " tbl   online ")).toBe(true);
    expect(sameSeller("TBL", "TBL Online")).toBe(false);
    expect(sameSeller("", "")).toBe(false);
  });

  it("drops each part of an offer the page did not say", () => {
    expect(sellerSummary({ name: "X", price: null, rating: null, cod: null, buy_now: null })).toBe("");
  });

  it("writes an offer's price with two decimals, paise or not", () => {
    expect(sellerSummary({ name: "X", price: 129900, rating: null, cod: null, buy_now: null })).toBe("₹1,29,900.00");
    expect(sellerSummary({ name: "X", price: 749.5, rating: null, cod: null, buy_now: null })).toBe("₹749.50");
  });
});
