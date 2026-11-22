// @vitest-environment jsdom
/**
 * Product Screener: search, sort, open a product's details (pictures, list
 * price, variants, sellers) and hand it to Place orders.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProductPreview, ProductSearchAnswer, ProductSearchResult } from "../lib/types";

const mocks = vi.hoisted(() => ({ searchProducts: vi.fn(), previewProduct: vi.fn() }));

vi.mock("../lib/api", () => ({
  searchProducts: mocks.searchProducts,
  previewProduct: mocks.previewProduct,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import ProductScreener, { sortResults } from "./ProductScreener";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const LINK_A = "https://www.flipkart.com/phone-a/p/itm1?pid=PIDAAAAAA1";
const LINK_B = "https://www.flipkart.com/phone-b/p/itm2?pid=PIDBBBBBB2";

const RESULTS: ProductSearchResult[] = [
  { title: "Phone A", link: LINK_A, pid: "PIDAAAAAA1", price: 22999, rating: 4.1, image: null, mrp: 38499, discount_pct: 40, highlights: ["6 GB RAM | 128 GB ROM"] },
  { title: "Phone B", link: LINK_B, pid: "PIDBBBBBB2", price: 17999, rating: 4.5, image: null, mrp: 30999, discount_pct: 41, highlights: [] },
];
const ANSWER: ProductSearchAnswer = { query: "phone", results: RESULTS, error: null };

const PREVIEW: ProductPreview = {
  url: LINK_A,
  name: "Phone A",
  brand: "Acme",
  category: null,
  color: null,
  image: "https://rukminim1.flixcart.com/a.jpg",
  sku: null,
  price: 22999,
  currency: "INR",
  availability: "https://schema.org/InStock",
  in_stock: true,
  rating: 4.1,
  rating_count: 100,
  seller: "RetailNet",
  error: null,
  default_seller: "RetailNet",
  sellers: [{ name: "RetailNet", price: 22999, rating: 4.4, cod: true, buy_now: true }],
  mrp: 38499,
  discount_pct: 40,
  images: ["https://rukminim1.flixcart.com/a.jpg", "https://rukminim1.flixcart.com/b.jpg"],
  variants: [{ label: "64 GB", link: LINK_B, pid: "PIDBBBBBB2", price: 17999, mrp: 30999, discount_pct: 42, in_stock: true }],
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ProductScreener />
    </MemoryRouter>,
  );
}

describe("sortResults", () => {
  it("sorts by price, discount and rating, leaving the original order alone", () => {
    expect(sortResults(RESULTS, "price_asc").map((r) => r.title)).toEqual(["Phone B", "Phone A"]);
    expect(sortResults(RESULTS, "price_desc").map((r) => r.title)).toEqual(["Phone A", "Phone B"]);
    expect(sortResults(RESULTS, "discount").map((r) => r.title)).toEqual(["Phone B", "Phone A"]);
    expect(sortResults(RESULTS, "rating").map((r) => r.title)).toEqual(["Phone B", "Phone A"]);
    expect(sortResults(RESULTS, "relevance")).toEqual(RESULTS);
  });

  it("puts products without the figure last", () => {
    const unpriced = { ...RESULTS[0], pid: "X", title: "No price", price: null };
    expect(sortResults([unpriced, ...RESULTS], "price_asc").at(-1)?.title).toBe("No price");
  });
});

describe("ProductScreener", () => {
  it("searches, shows list price and discount, opens details and links to Place orders", async () => {
    mocks.searchProducts.mockResolvedValue(ANSWER);
    mocks.previewProduct.mockResolvedValue(PREVIEW);
    renderPage();

    fireEvent.change(screen.getByLabelText("Product name or link"), { target: { value: "phone" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await screen.findByText("2 results");
    expect(mocks.searchProducts).toHaveBeenCalledWith("phone");
    expect(screen.getByText("40% off")).toBeTruthy();
    expect(screen.getByText("6 GB RAM | 128 GB ROM")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Phone A/ }));
    await waitFor(() => expect(mocks.previewProduct).toHaveBeenCalledWith(LINK_A));
    await screen.findByRole("button", { name: "64 GB" });
    expect(screen.getByRole("button", { name: "Picture 2" })).toBeTruthy();
    expect(screen.getByText("₹17,999.00 · 42% off")).toBeTruthy();

    const order = screen.getByRole("link", { name: "Order this product" }) as HTMLAnchorElement;
    expect(order.getAttribute("href")).toBe(`/ordering?product=${encodeURIComponent(LINK_A)}`);

    fireEvent.click(screen.getByRole("button", { name: "64 GB" }));
    await waitFor(() => expect(mocks.previewProduct).toHaveBeenLastCalledWith(LINK_B));
  });

  it("opens a pasted link directly, without searching", async () => {
    mocks.previewProduct.mockResolvedValue(PREVIEW);
    renderPage();
    fireEvent.change(screen.getByLabelText("Product name or link"), { target: { value: LINK_A } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await waitFor(() => expect(mocks.previewProduct).toHaveBeenCalledWith(LINK_A));
    expect(mocks.searchProducts).not.toHaveBeenCalled();
  });

  it("says when the search failed", async () => {
    mocks.searchProducts.mockResolvedValue({ query: "phone", results: [], error: "could not search Flipkart just now" });
    renderPage();
    fireEvent.change(screen.getByLabelText("Product name or link"), { target: { value: "phone" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("could not search Flipkart just now");
  });
});
