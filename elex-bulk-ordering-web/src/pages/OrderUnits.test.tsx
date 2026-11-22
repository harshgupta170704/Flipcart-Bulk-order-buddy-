// @vitest-environment jsdom
/**
 * The ledger page's own pieces rendered for real: the Download CSV menu's
 * keyboard contract, which rows still offer a Cancel, what the card filter
 * says it matches, and the card header that must let the ten-control toolbar
 * wrap rather than run off the page.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";
import { Card } from "../components/Card";
import { OrderTable } from "../components/OrderTable";
import { OrderUnitsFilters, isNarrowed } from "../components/OrderUnitsFilters";
import { DownloadMenu } from "./OrderUnits";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** One ledger row, placed by this tool and read back once; override what a test is about. */
function unit(over: Partial<Order> & { id: number }): Order {
  return {
    order_row_id: over.id,
    unit_position: 1,
    units_in_order: 1,
    source: "run",
    account_email: "units@test.com",
    product_link: null,
    qty: 1,
    model: "vivo T5x 5G",
    color: null,
    pin: "110001",
    amount: 15000,
    target: null,
    order_id: `OD${String(over.id).padStart(18, "0")}`,
    gst_name: null,
    status: "placed",
    payment_mode: "card",
    card_number: "4111 1111 1111 1111",
    delivery_address: null,
    delivery_pincode: null,
    billed_gstin: null,
    billed_business_name: null,
    placed_at: "2026-10-24T10:00:00",
    created_at: "2026-10-24T10:00:00",
    brand: null,
    category: null,
    job_id: 1,
    delivery_state: "in_transit",
    item_id: null,
    unit_id: null,
    seller: null,
    tracking_id: null,
    platform_status: "Shipped",
    delivery_date: null,
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-25T08:00:00",
    open_request_kinds: [],
    ...over,
  };
}

describe("DownloadMenu", () => {
  function openMenu() {
    const trigger = screen.getByRole("button", { name: "Download CSV" });
    fireEvent.click(trigger);
    return { trigger, items: screen.getAllByRole("menuitem") };
  }

  it("puts focus on the first choice when opened and moves it with the arrows, Home and End", () => {
    render(<DownloadMenu busy={null} disabled={false} onPick={vi.fn()} />);
    const { items } = openMenu();
    expect(items).toHaveLength(3);
    expect(document.activeElement).toBe(items[0]);

    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(items[1], { key: "End" });
    expect(document.activeElement).toBe(items[2]);
    fireEvent.keyDown(items[2], { key: "ArrowDown" }); // round the end
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(items[0], { key: "ArrowUp" }); // and round the start
    expect(document.activeElement).toBe(items[2]);
    fireEvent.keyDown(items[2], { key: "Home" });
    expect(document.activeElement).toBe(items[0]);
  });

  it("closes on Escape and hands focus back to the button instead of dropping it on body", () => {
    render(<DownloadMenu busy={null} disabled={false} onPick={vi.fn()} />);
    const { trigger, items } = openMenu();

    fireEvent.keyDown(items[0], { key: "Escape" });

    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger);
  });

  it("picks a layout, closes and hands focus back to the button", () => {
    const onPick = vi.fn();
    render(<DownloadMenu busy={null} disabled={false} onPick={onPick} />);
    const { trigger, items } = openMenu();

    fireEvent.click(items[1]);

    expect(onPick).toHaveBeenCalledWith("out_for_delivery");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("puts focus back on the button when the download ends, if the busy button had dropped it", () => {
    // A pick disables the button for the download; a disabled control cannot
    // hold focus, so browsers drop it to body. jsdom does not implement that
    // rule, so the drop is staged: focus a scratch field, then remove it —
    // jsdom does move focus to body when the focused node leaves the page.
    // Keyed children keep the menu mounted across the rerenders.
    const tree = (scratch: boolean, busy: "csv" | null, disabled: boolean) => (
      <>
        {scratch ? <input key="scratch" aria-label="scratch" /> : null}
        <DownloadMenu key="menu" busy={busy} disabled={disabled} onPick={vi.fn()} />
      </>
    );
    const { rerender } = render(tree(true, null, false));
    const { trigger, items } = openMenu();
    fireEvent.click(items[0]);
    expect(document.activeElement).toBe(trigger);

    rerender(tree(true, "csv", true));
    screen.getByLabelText("scratch").focus();
    rerender(tree(false, "csv", true));
    expect(document.activeElement).toBe(document.body);

    rerender(tree(false, null, false));
    expect(document.activeElement).toBe(trigger);
  });

  it("leaves focus alone when the download ends and the user has moved on", () => {
    const { rerender } = render(
      <>
        <input aria-label="elsewhere" />
        <DownloadMenu busy="csv" disabled={true} onPick={vi.fn()} />
      </>,
    );
    const elsewhere = screen.getByLabelText("elsewhere");
    elsewhere.focus();
    rerender(
      <>
        <input aria-label="elsewhere" />
        <DownloadMenu busy={null} disabled={false} onPick={vi.fn()} />
      </>,
    );
    expect(document.activeElement).toBe(elsewhere);
  });

  it("opens from the button with ArrowDown on the first choice and ArrowUp on the last", () => {
    render(<DownloadMenu busy={null} disabled={false} onPick={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "Download CSV" });

    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    let items = screen.getAllByRole("menuitem");
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(items[0], { key: "Escape" });

    fireEvent.keyDown(trigger, { key: "ArrowUp" });
    items = screen.getAllByRole("menuitem");
    expect(document.activeElement).toBe(items[2]);
  });

  it("names the menu after its button and closes on a press outside or on Tab", () => {
    render(<DownloadMenu busy={null} disabled={false} onPick={vi.fn()} />);
    const { trigger, items } = openMenu();
    const menu = screen.getByRole("menu");
    expect(menu.getAttribute("aria-labelledby")).toBe(trigger.id);
    expect(trigger.getAttribute("aria-controls")).toBe(menu.id);

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();

    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getAllByRole("menuitem")[0], { key: "Tab" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(items[0].isConnected).toBe(false);
  });
});

describe("OrderTable's row Cancel", () => {
  const tableProps = {
    loading: false,
    error: null,
    onRetry: () => undefined,
    filtered: false,
    onClearFilter: () => undefined,
    onView: vi.fn(),
    onCancel: vi.fn(),
  };

  it("is offered only while the unit is still on its way, as the server would accept", () => {
    const states: Order["delivery_state"][] = ["in_transit", "unknown", "delivered", "cancelled", "returned", "failed"];
    render(<OrderTable {...tableProps} orders={states.map((state, i) => unit({ id: i + 1, delivery_state: state }))} />);

    const offered = screen.getAllByRole("button", { name: /^Cancel OD/ }).map((b) => b.getAttribute("aria-label"));
    expect(offered).toEqual(["Cancel OD000000000000000001", "Cancel OD000000000000000002"]);
    // Every row still has its View.
    expect(screen.getAllByRole("button", { name: /^View order OD/ })).toHaveLength(6);
  });

  it("is withheld once a cancel is already asked, and kept for a row with no reading at all", () => {
    render(
      <OrderTable
        {...tableProps}
        orders={[
          unit({ id: 1, open_request_kinds: ["cancel"] }),
          unit({ id: 2, open_request_kinds: ["reschedule"] }),
          unit({ id: 3, delivery_state: undefined as unknown as Order["delivery_state"] }),
        ]}
      />,
    );

    const offered = screen.getAllByRole("button", { name: /^Cancel OD/ }).map((b) => b.getAttribute("aria-label"));
    expect(offered).toEqual(["Cancel OD000000000000000002", "Cancel OD000000000000000003"]);
  });
});

describe("the Card Number filter", () => {
  it("says it is matched on digits, and a value with none still counts as narrowing", () => {
    render(
      <OrderUnitsFilters
        draft={{ date_field: "placed_at" }}
        onChange={() => undefined}
        onApply={() => undefined}
        onReset={() => undefined}
        appliedNarrowed={false}
        options={null}
        applying={false}
        idPrefix="t"
      />,
    );
    const box = screen.getByLabelText("Card Number");
    const help = document.getElementById(box.getAttribute("aria-describedby") ?? "");
    expect(help?.textContent).toMatch(/digits/i);
    expect(help?.textContent).toMatch(/spaces/i);

    // The server matches nothing for "abcd", so the list IS narrowed: Reset must stay on.
    expect(isNarrowed({ date_field: "placed_at", card: "abcd" })).toBe(true);
    expect(isNarrowed({ date_field: "placed_at", card: "   " })).toBe(false);
  });
});

describe("Card's header", () => {
  it("lets a long toolbar wrap inside the card instead of running off it", () => {
    render(
      <Card
        title="Showing 3 of 3 order units"
        actions={
          <>
            <button type="button">One</button>
            <button type="button">Two</button>
          </>
        }
      />,
    );
    const actions = screen.getByRole("button", { name: "One" }).parentElement as HTMLElement;
    // jsdom lays nothing out, so this pins the flex contract instead: the box
    // may shrink (no shrink-0, whose fixed one-line width could never wrap)
    // and wraps its own buttons (flex-wrap) when it does.
    expect(actions.classList.contains("flex")).toBe(true);
    expect(actions.classList.contains("flex-wrap")).toBe(true);
    expect(actions.classList.contains("shrink-0")).toBe(false);
  });
});
