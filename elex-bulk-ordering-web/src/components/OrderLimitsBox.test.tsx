// @vitest-environment jsdom
/**
 * "Limits for this order" on its own: what it says while the saved limits
 * that apply are not known, the greyed list when changes cannot be made, and
 * which errors show before and after Submit. The form's tests drive the rest
 * (OrderForm.limits.test.tsx).
 */
import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  NO_LIMIT_CHANGES,
  ONLY_ADMIN_LOOSENS,
  validateLimitChanges,
  type LimitChanges,
  type ProductValues,
} from "../lib/limitChanges";
import type { ApplicableLimit, RunCheckResult } from "../lib/types";
import { OrderLimitsBox, type OrderLimitsBoxProps } from "./OrderLimitsBox";

afterEach(cleanup);

const VIVO: ApplicableLimit = { limit_id: 4, scope: "brand", value: "vivo", max_units: 2, window_days: 30 };
const PRODUCT: ProductValues = { brand: "vivo", category: "Mobiles", seller: "", variant: null, title: "vivo T5x 5G" };
const OFF = "Off for this order: each account is still checked at its turn with the saved limits.";

function show(over: Partial<OrderLimitsBoxProps> = {}) {
  return render(
    <OrderLimitsBox
      applicable={[VIVO]}
      check={{ kind: "idle" }}
      onRetry={() => undefined}
      changes={NO_LIMIT_CHANGES}
      onChange={() => undefined}
      active
      inactiveLine={OFF}
      isAdmin
      product={PRODUCT}
      variantLine={null}
      errors={null}
      showErrors={false}
      {...over}
    />,
  );
}

describe("OrderLimitsBox", () => {
  it("says the limits show once there is a product, while there is nothing to ask about", () => {
    show({ applicable: null, check: { kind: "idle" } });
    expect(screen.getByText("The limits that apply show once there is a product and at least one ID.")).toBeTruthy();
  });

  it("says it is reading them while a check is on its way, or an answer about another product is still held", () => {
    show({ applicable: null, check: { kind: "loading" } });
    expect(screen.getByText("Reading the limits that apply to this product…")).toBeTruthy();
    cleanup();

    show({ applicable: null, check: { kind: "ready", run: {} as RunCheckResult } });
    expect(screen.getByText("Reading the limits that apply to this product…")).toBeTruthy();
  });

  it("says a failed check failed, and asks again on request", () => {
    const onRetry = vi.fn();
    show({ applicable: null, check: { kind: "error", message: "Server busy." }, onRetry });
    expect(screen.getByText("Could not read the limits that apply: Server busy.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("says when no saved limit applies, and still offers a line for this order", () => {
    show({ applicable: [] });
    expect(screen.getByText("No saved limit applies to this product.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add a limit for this order" })).toBeTruthy();
  });

  it("greys the saved limits, with why, when changes cannot be made", () => {
    show({ active: false, variantLine: "Variant limits cannot apply to this order: the link names no variant." });
    const box = within(screen.getByRole("region", { name: "Limits for this order" }));

    expect(box.getByText(OFF)).toBeTruthy();
    expect(box.getByText("Variant limits cannot apply to this order: the link names no variant.")).toBeTruthy();
    expect(box.getByText("Brand · vivo").closest("li")?.textContent).toBe("Brand · vivo (2 units / 30 days)");
    expect(box.queryByRole("spinbutton")).toBeNull();
    expect(box.queryByRole("button")).toBeNull();
  });

  it("shows a loosening at once, and every other error only once Submit has been pressed", () => {
    const errors = { saved: { 4: { units: "Units: a whole number, 1 or more.", loosen: ONLY_ADMIN_LOOSENS } }, added: {} };
    const changes = { forLink: "x", edits: { 4: { maxUnits: "0", windowDays: "30", enabled: false } }, added: [] };
    const view = show({ isAdmin: false, errors, changes });
    const line = () => within(screen.getByRole("listitem", { name: "Brand · vivo" }));

    expect(line().getByRole("alert").textContent).toBe(ONLY_ADMIN_LOOSENS);

    view.rerender(
      <OrderLimitsBox
        applicable={[VIVO]}
        check={{ kind: "idle" }}
        onRetry={() => undefined}
        changes={changes}
        onChange={() => undefined}
        active
        inactiveLine={OFF}
        isAdmin={false}
        product={PRODUCT}
        variantLine={null}
        errors={errors}
        showErrors
      />,
    );
    expect(line().getByRole("alert").textContent).toBe(`Units: a whole number, 1 or more. ${ONLY_ADMIN_LOOSENS}`);
    // Switched off by an admin's earlier run: someone else may switch it back on.
    expect((line().getByLabelText("Use Brand · vivo for this order") as HTMLInputElement).disabled).toBe(false);
  });

  it("shows the server's refusal of the changes, and keeps the lines to put it right", () => {
    show({
      check: {
        kind: "error",
        message: "A brand limit for 'vivo' already applies: change that one instead.",
        refusedChanges: true,
      },
    });
    const box = within(screen.getByRole("region", { name: "Limits for this order" }));
    expect(box.getByRole("alert").textContent).toBe("A brand limit for 'vivo' already applies: change that one instead.");
    expect(box.getByLabelText("Units: Brand · vivo")).toBeTruthy();
    expect(box.queryByText(/Could not read the limits/)).toBeNull();
  });
});

/** The box with its changes kept, as the form keeps them. */
function Harness({ product = PRODUCT, applicable = [VIVO] }: { product?: ProductValues; applicable?: ApplicableLimit[] }) {
  const [changes, setChanges] = useState<LimitChanges>(NO_LIMIT_CHANGES);
  return (
    <OrderLimitsBox
      applicable={applicable}
      check={{ kind: "idle" }}
      onRetry={() => undefined}
      changes={changes}
      onChange={(next) => setChanges({ forLink: "x", ...next })}
      active
      inactiveLine={OFF}
      isAdmin
      product={product}
      variantLine={null}
      errors={validateLimitChanges(changes, applicable, product, true)}
      showErrors
    />
  );
}

describe("OrderLimitsBox: one added line per level", () => {
  const FULL: ProductValues = { brand: "vivo", category: "Mobiles", seller: "SAPHEDretail", variant: "MOBHNDTD57UJUK9Q", title: "vivo T5x 5G" };
  const scopes = (label: string) =>
    Array.from((screen.getByLabelText(`Scope: ${label}`) as HTMLSelectElement).options).map((option) => option.value);

  it("never offers a level another added line has, nor the brand a saved limit already covers", () => {
    render(<Harness product={FULL} />);
    const add = screen.getByRole("button", { name: "Add a limit for this order" }) as HTMLButtonElement;

    fireEvent.click(add);
    // The saved "brand vivo" covers the product's brand: the first new line is its category.
    expect((screen.getByLabelText("Scope: Added limit 1") as HTMLSelectElement).value).toBe("category");
    expect(scopes("Added limit 1")).toEqual(["category", "model", "variant", "seller"]);

    fireEvent.click(add);
    expect((screen.getByLabelText("Scope: Added limit 2") as HTMLSelectElement).value).toBe("variant");
    expect(scopes("Added limit 2")).toEqual(["model", "variant", "seller"]);
    expect(scopes("Added limit 1")).toEqual(["category", "model", "seller"]);

    fireEvent.click(add);
    fireEvent.click(add);
    // Category, variant, seller and model each have a line; brand is covered: nothing left.
    expect(screen.getAllByRole("listitem", { name: /^Added limit/ })).toHaveLength(4);
    expect(add.disabled).toBe(true);
  });

  it("marks a line that repeats a saved limit once the limits that apply show it", () => {
    const { rerender } = render(<Harness product={FULL} applicable={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "Add a limit for this order" }));
    expect((screen.getByLabelText("Scope: Added limit 1") as HTMLSelectElement).value).toBe("brand");
    fireEvent.change(screen.getByLabelText("Units: Added limit 1"), { target: { value: "1" } });
    expect(screen.queryByRole("alert")).toBeNull();

    rerender(<Harness product={FULL} applicable={[VIVO]} />);
    expect(within(screen.getByRole("listitem", { name: "Added limit 1" })).getByRole("alert").textContent).toBe(
      "A brand limit for 'vivo' already applies: change that one instead.",
    );
  });
});
