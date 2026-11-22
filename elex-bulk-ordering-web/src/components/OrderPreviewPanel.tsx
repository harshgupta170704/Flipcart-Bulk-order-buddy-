import type { ReactNode } from "react";
import { formatCount } from "../lib/format";
import type { Address, Gst, PaymentMode, ProductPreview, QtyMode, RunMode } from "../lib/types";
import { PAYMENT_MODE_LABELS, usesCard } from "../lib/types";
import { addressPlace, addressStreet } from "./AddressBookData";

/**
 * The run as it stands, beside the form: what will be bought, how many, from
 * whom, at what cap, paid how, shipped where and billed to whom. It reads the
 * same values the form sends, so what it says is what Submit sends.
 */
export interface OrderPreviewPanelProps {
  runMode: RunMode;
  /** The AI pacing in a few words, when the run is in AI mode. */
  aiSummary?: string | null;
  preview: ProductPreview | null;
  totalUnits: number | null;
  unitsPerOrder: number;
  accounts: number;
  seller: string;
  defaultSeller: string | null;
  target: string;
  noCap: boolean;
  qtyMode: QtyMode;
  paymentMode: PaymentMode;
  cartLimit: string;
  maxDeliveryDays: string;
  gstMandatory: boolean;
  phones: number;
  useAddressPhone: boolean;
  excludeDays?: number | null;
  smartSelection?: boolean;
  /** Card filters in words, e.g. ["cooldown 3 days", "max 2 per card"]. */
  cardRules?: string[];
  /** The chosen address and the GST the run bills under, as the picker resolved them. */
  address: Address | null;
  gst: Gst | null;
  /** The live unit-limit usage for this product and selection. */
  limits?: ReactNode;
  /** The Card Type the run uses, and how the bank's step is done. */
  cardType?: string | null;
  authentication?: string | null;
  /** The Submit button and what it says, at the foot of the card. */
  footer?: ReactNode;
  className?: string;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[12px]">
      <dt className="text-ink-2">{label}:</dt>
      <dd className="text-right font-medium text-ink">{children}</dd>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h4 className="text-[12px] font-semibold text-ink">{title}</h4>
      {children}
    </section>
  );
}

const rupees = (value: number) => `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

const MODE_BADGE: Record<RunMode, { label: string; className: string }> = {
  real: { label: "Real", className: "bg-good/15 text-ink" },
  mock: { label: "Mock", className: "bg-warning/25 text-ink" },
  ai: { label: "AI", className: "bg-accent/15 text-ink" },
};

export function OrderPreviewPanel(props: OrderPreviewPanelProps) {
  const { address, gst, preview } = props;
  const badge = MODE_BADGE[props.runMode];

  const cap = props.noCap ? "No cap" : props.target.trim() === "" ? null : rupees(Number(props.target));
  const seller = props.seller.trim() || props.defaultSeller || null;
  const phoneText =
    props.phones > 0
      ? `${formatCount(props.phones)} number${props.phones === 1 ? "" : "s"}, in turn`
      : props.useAddressPhone
        ? "The address's own"
        : null;

  return (
    <aside
      aria-label="Order preview"
      className={`flex flex-col gap-4 rounded-card border border-line bg-surface p-5 ${props.className ?? ""}`}
    >
      <div className="flex items-center justify-between border-b border-line pb-3">
        <h3 className="text-[14px] font-semibold text-ink">Order Preview</h3>
        <span className={`rounded px-2 py-0.5 text-[11px] font-semibold ${badge.className}`}>{badge.label}</span>
      </div>
      {props.aiSummary ? <p className="-mt-2 text-[12px] text-ink-2">AI pacing: {props.aiSummary}</p> : null}

      {preview?.name ? (
        <Section title="Product">
          <div className="flex flex-col gap-1 rounded-control bg-page px-3 py-2.5 text-[12px]">
            <p className="font-medium leading-snug text-ink">{preview.name}</p>
            {[preview.category, preview.brand].filter(Boolean).length > 0 ? (
              <p className="text-ink-2">{[preview.category, preview.brand].filter(Boolean).join(" • ")}</p>
            ) : null}
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-semibold tabular-nums" style={{ color: "var(--status-good)" }}>
                {preview.price != null ? rupees(preview.price) : "Price not read"}
              </span>
              {seller ? <span className="truncate text-accent">by {seller}</span> : null}
            </div>
            {preview.color ? <p className="text-ink-2">{preview.color}</p> : null}
            {preview.in_stock !== null ? (
              <p className="flex items-center gap-1.5 text-ink-2">
                <span
                  aria-hidden="true"
                  className="size-1.5 rounded-full"
                  style={{ backgroundColor: preview.in_stock ? "var(--status-good)" : "var(--status-critical)" }}
                />
                {preview.in_stock ? "In Stock" : "Out of Stock"}
              </p>
            ) : null}
          </div>
          <div className="grid grid-cols-2 gap-2 border-b border-line pb-3 text-[12px]">
            <div>
              <p className="text-ink-2">Total:</p>
              <p className="font-medium text-ink">{props.totalUnits ?? "1 per account"}</p>
            </div>
            <div>
              <p className="text-ink-2">Per Order:</p>
              <p className="font-medium text-ink">{props.unitsPerOrder}</p>
            </div>
          </div>
        </Section>
      ) : null}

      <Section title="Order Details">
        <dl className="flex flex-col gap-1.5">
          <Row label="Platform">FLIPKART</Row>
          <Row label="Payment Mode">{PAYMENT_MODE_LABELS[props.paymentMode] ?? props.paymentMode}</Row>
          {usesCard(props.paymentMode) && props.cardType ? <Row label="Card Type">{props.cardType}</Row> : null}
          {usesCard(props.paymentMode) && props.authentication ? (
            <Row label="Authentication">{props.authentication}</Row>
          ) : null}
          {usesCard(props.paymentMode) && props.cardRules && props.cardRules.length > 0 ? (
            <Row label="Card Filters">{props.cardRules.join(", ")}</Row>
          ) : null}
          {cap ? <Row label="Final Amount Limit (per unit)">{cap}</Row> : null}
          {props.cartLimit.trim() ? <Row label="Cart Amount Limit (per unit)">{rupees(Number(props.cartLimit))}</Row> : null}
          {props.maxDeliveryDays.trim() ? <Row label="Max Delivery Days">{props.maxDeliveryDays}</Row> : null}
          <Row label="Mode">{props.qtyMode === "minimum" ? "Minimum" : "Upto"}</Row>
          <Row label="Email IDs">{formatCount(props.accounts)}</Row>
          <Row label="Smart ID">{props.smartSelection ? "Enabled" : "Disabled"}</Row>
          {props.excludeDays ? <Row label="Exclude IDs">ordered in {props.excludeDays} days</Row> : null}
          {props.gstMandatory ? <Row label="GST Mandatory">Yes</Row> : null}
          {phoneText ? <Row label="Phone">{phoneText}</Row> : null}
          {seller ? <Row label="Seller">{seller}</Row> : null}
        </dl>
      </Section>

      {props.limits ? <Section title="Platform unit limits">{props.limits}</Section> : null}

      <Section title="Delivery & Billing">
        {address ? (
          <div className="rounded-control bg-page px-3 py-2.5 text-[12px] leading-relaxed text-ink">
            <p>{address.name}</p>
            <p>{addressStreet(address)}</p>
            <p>{addressPlace(address)}</p>
          </div>
        ) : (
          <p className="text-[12px] text-ink-2">No address chosen yet.</p>
        )}
        {gst ? (
          <div className="rounded-control bg-page px-3 py-2.5 text-[12px] leading-relaxed text-ink">
            <p className="font-mono tracking-wide">{gst.gstin}</p>
            <p>{gst.business_name}</p>
          </div>
        ) : null}
      </Section>

      {props.footer ? <div className="flex flex-col gap-2 border-t border-line pt-4">{props.footer}</div> : null}
    </aside>
  );
}

export default OrderPreviewPanel;
