// @vitest-environment jsdom
/**
 * "Search Flipkart": a search runs when asked (Search or Enter — never a run
 * started by the Enter key), lists name, price and rating, and picking one
 * hands its link on; a failed search says so in plain words.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProductSearchAnswer } from "../lib/types";

const mocks = vi.hoisted(() => ({ searchProducts: vi.fn() }));

vi.mock("../lib/api", () => ({
  searchProducts: mocks.searchProducts,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { ProductSearch } from "./ProductSearch";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const LINK = "https://www.flipkart.com/vivo-t5-lite-44w-5g-twilight-shadow-64-gb/p/itm5420c578ec2df?pid=MOBHQSFF6MZK4YNP";
const ANSWER: ProductSearchAnswer = {
  query: "vivo t5 lite",
  results: [
    { title: "vivo T5 Lite 44W 5G (Wave Blue, 128 GB)", link: LINK.replace("MOBHQSFF6MZK4YNP", "MOBHNDTD57UJUK9Q"), pid: "MOBHNDTD57UJUK9Q", price: 22999, rating: 4.3, image: null },
    { title: "vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)", link: LINK, pid: "MOBHQSFF6MZK4YNP", price: 17999, rating: 4.3, image: null },
  ],
  error: null,
};

function box(): HTMLInputElement {
  return screen.getByLabelText("Search Flipkart") as HTMLInputElement;
}

describe("ProductSearch", () => {
  it("searches when asked, lists the results, and hands the picked link on", async () => {
    mocks.searchProducts.mockResolvedValue(ANSWER);
    const onPick = vi.fn();
    render(<ProductSearch onPick={onPick} />);

    const button = screen.getByRole("button", { name: "Search" }) as HTMLButtonElement;
    fireEvent.change(box(), { target: { value: "v" } });
    expect(button.disabled).toBe(true); // one letter finds everything
    fireEvent.change(box(), { target: { value: "  vivo   t5 lite " } });
    fireEvent.click(button);

    expect(await screen.findByText("vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)")).toBeTruthy();
    expect(mocks.searchProducts).toHaveBeenCalledWith("vivo t5 lite");
    expect(screen.getAllByText(/4\.3★/)).toHaveLength(2);

    fireEvent.click(screen.getByText("vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)"));
    expect(onPick).toHaveBeenCalledWith(LINK);
    expect(screen.queryByRole("list", { name: "Search results" })).toBeNull();
  });

  it("searches on Enter without submitting the order form around it", async () => {
    mocks.searchProducts.mockResolvedValue(ANSWER);
    const submitted = vi.fn((event: Event) => event.preventDefault());
    render(
      <form onSubmit={(event) => submitted(event.nativeEvent)}>
        <ProductSearch onPick={vi.fn()} />
      </form>,
    );
    // jsdom never submits a form on Enter, so the spy above only catches a
    // submit the component makes itself. In a browser, Enter in a box inside
    // the order form submits it — a run started — unless the key's default
    // action is cancelled; fireEvent returns false when it was. Too short a
    // query searches nothing, and must not submit either.
    fireEvent.change(box(), { target: { value: "v" } });
    expect(fireEvent.keyDown(box(), { key: "Enter" })).toBe(false);
    expect(mocks.searchProducts).not.toHaveBeenCalled();

    fireEvent.change(box(), { target: { value: "vivo t5 lite" } });
    expect(fireEvent.keyDown(box(), { key: "a" })).toBe(true); // other keys type as usual
    expect(fireEvent.keyDown(box(), { key: "Enter" })).toBe(false);

    await waitFor(() => expect(mocks.searchProducts).toHaveBeenCalledTimes(1));
    expect(submitted).not.toHaveBeenCalled();
  });

  it("says when the search could not be made, and when it found nothing", async () => {
    mocks.searchProducts.mockResolvedValueOnce({ query: "vivo", results: [], error: "could not search Flipkart just now" });
    render(<ProductSearch onPick={vi.fn()} />);
    fireEvent.change(box(), { target: { value: "vivo" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("could not search Flipkart just now")).toBeTruthy();

    mocks.searchProducts.mockResolvedValueOnce({ query: "zzzz", results: [], error: null });
    fireEvent.change(box(), { target: { value: "zzzz" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/Nothing found for/)).toBeTruthy();

    mocks.searchProducts.mockRejectedValueOnce(new Error("Type 2 to 100 characters to search Flipkart."));
    fireEvent.change(box(), { target: { value: "ab" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Type 2 to 100 characters to search Flipkart.")).toBeTruthy();
  });
});
