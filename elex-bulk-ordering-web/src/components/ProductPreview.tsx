/**
 * What is about to be ordered, read off the product page.
 *
 * Purely presentational: the host owns the fetch, the debounce and which of the
 * three states this is in. Two things here are deliberate and load-bearing:
 *
 *  - Stock is TRI-state. `null` means the page did not say, which is not the
 *    same as out of stock and is never dressed up as it — the operator is told
 *    "unknown" in muted text and left to decide.
 *  - Every field but the link is scraped, so every field can be missing. A
 *    missing one is dropped from the row rather than rendered as a blank or,
 *    worse, as the word "null".
 *
 * Who sells the product is listed under the facts — every seller the sellers
 * page shows, the default one marked — because choosing another seller is the
 * operator's main lever against a seller that cancels. A list that could not
 * be read is said so quietly: it never stops a run.
 */
import { useState } from "react";
import { formatCount, formatCurrency, formatLink } from "../lib/format";
import type { ProductPreview as ProductPreviewData, SellerOffer } from "../lib/types";
import { Alert } from "./Alert";
import { Card } from "./Card";
import { Spinner } from "./Spinner";

export interface ProductPreviewProps {
  /** What the server read. null before anything has been asked for. */
  preview: ProductPreviewData | null;
  loading: boolean;
  /** A failed REQUEST. A failed READ arrives as `preview.error` instead. */
  error: string | null;
  className?: string;
}

/** Trimmed text, or null — so "" and "   " are both treated as missing. */
function text(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed === "" ? null : trimmed;
}

/**
 * "₹16,499.00" — the ₹ sign, Indian grouping and two decimals, the way every
 * other amount in the app reads (spec NFR-010), the ledger row the order
 * becomes included. Flipkart sells in rupees, so the page's currency code
 * changes nothing.
 */
function formatPrice(price: number | null): string | null {
  return price === null || !Number.isFinite(price) ? null : formatCurrency(price);
}

/** "4.3 / 5 · 47,544 ratings" — either half alone is still worth showing. */
function formatRating(rating: number | null, count: number | null): string | null {
  const parts: string[] = [];
  if (rating !== null && Number.isFinite(rating)) parts.push(`${rating.toFixed(1)} / 5`);
  if (count !== null && Number.isFinite(count) && count > 0) {
    parts.push(`${formatCount(count)} ${count === 1 ? "rating" : "ratings"}`);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

/** Seller names compare as the server compares them: case and spacing aside. */
export function sameSeller(first: string | null | undefined, second: string | null | undefined): boolean {
  const key = (value: string | null | undefined) => (value ?? "").replace(/\s+/g, " ").trim().toLowerCase();
  return key(first) !== "" && key(first) === key(second);
}

/**
 * "₹749.00 · rated 4.3 · Cash on Delivery" — what is known of one seller's
 * offer, each part dropped when the page did not say.
 */
export function sellerSummary(offer: SellerOffer): string {
  const parts: string[] = [];
  const price = formatPrice(offer.price);
  if (price !== null) parts.push(price);
  if (offer.rating !== null && Number.isFinite(offer.rating)) parts.push(`rated ${offer.rating.toFixed(1)}`);
  if (offer.cod === true) parts.push("Cash on Delivery");
  if (offer.cod === false) parts.push("no Cash on Delivery");
  if (offer.buy_now === false) parts.push("Add to cart only");
  return parts.join(" · ");
}

interface Fact {
  label: string;
  value: string;
  /** The price, the one fact that is read before any other. */
  strong?: boolean;
}

function buildFacts(preview: ProductPreviewData): Fact[] {
  const facts: Fact[] = [];
  const push = (label: string, value: string | null, strong?: boolean) => {
    if (value !== null) facts.push({ label, value, strong });
  };

  push("Price", formatPrice(preview.price), true);
  push("Brand", text(preview.brand));
  push("Category", text(preview.category));
  push("Colour", text(preview.color));
  push("Rating", formatRating(preview.rating, preview.rating_count));
  push("SKU", text(preview.sku));
  return facts;
}

interface StockSpec {
  label: string;
  glyph: string;
  /** A ready-to-use CSS colour for the glyph and the label together. */
  color: string;
}

/**
 * The hue is mixed with primary ink rather than used raw: the status green is
 * only ~3.3:1 on the light surface, and mixing it towards whichever ink the
 * theme is using keeps it a readable green in light mode and a readable green
 * in dark mode without a single `dark:` variant. The text label and the glyph
 * are always both there, so the colour is never carrying the meaning alone.
 */
function stockSpec(inStock: boolean | null): StockSpec {
  if (inStock === true) {
    return {
      label: "In stock",
      glyph: "✓",
      color: "color-mix(in oklab, var(--status-good) 80%, var(--ink-primary))",
    };
  }
  if (inStock === false) {
    return {
      label: "Out of stock",
      glyph: "✕",
      color: "color-mix(in oklab, var(--status-critical) 80%, var(--ink-primary))",
    };
  }
  // The page did not say. Not a problem, so not a problem colour.
  return { label: "Stock unknown", glyph: "?", color: "var(--ink-muted)" };
}

const IMAGE_BOX = "size-[104px] shrink-0 rounded-control border border-line bg-page";

export function ProductPreview({ preview, loading, error, className = "" }: ProductPreviewProps) {
  /* Which image URL 404'd, rather than a bare "it broke" flag: a new product's
     image then starts out trusted again with no effect to reset it. */
  const [brokenSrc, setBrokenSrc] = useState<string | null>(null);

  if (loading) {
    return (
      <Card className={className}>
        <p aria-live="polite" className="flex items-center gap-2.5 text-[13px] text-ink-2">
          <Spinner size={14} />
          Loading preview…
        </p>
      </Card>
    );
  }

  // A failed request and a failed read read the same way to the operator, and
  // neither one is a reason to stop: the run opens the page itself.
  const message = error ?? text(preview?.error);
  if (message !== null) {
    return (
      <Alert tone="error" title="Could not read this product page" className={className}>
        {message} The link can still be used — nothing about it could be checked first.
      </Alert>
    );
  }

  if (preview === null) return null;

  const name = text(preview.name);
  const image = text(preview.image);
  const facts = buildFacts(preview);
  const stock = stockSpec(preview.in_stock);
  const sellers = preview.sellers ?? [];
  const defaultSeller = text(preview.default_seller ?? null) ?? text(preview.seller);
  const sellersError = text(preview.sellers_error ?? null);

  return (
    <Card className={className}>
      <div className="flex items-start gap-4">
        {image !== null && image !== brokenSrc ? (
          <img
            src={image}
            alt={name ?? "Product image"}
            width={104}
            height={104}
            loading="lazy"
            decoding="async"
            // A CDN link that has expired or moved must not leave a broken-image
            // icon in the middle of the form.
            onError={() => setBrokenSrc(image)}
            className={`${IMAGE_BOX} object-contain p-1.5`}
          />
        ) : (
          <div
            aria-hidden="true"
            className={`${IMAGE_BOX} flex items-center justify-center text-[11px] text-muted`}
          >
            No image
          </div>
        )}

        <div className="flex min-w-0 flex-col gap-2.5">
          <h3
            className="text-[15px] font-semibold leading-snug text-ink"
            style={{ overflowWrap: "anywhere" }}
          >
            {/* A page that gave no name still has to be identifiable. */}
            {name ?? formatLink(preview.url, 72)}
          </h3>

          <p
            className="flex items-center gap-1.5 text-[13px] font-semibold leading-none"
            style={{ color: stock.color }}
          >
            <span aria-hidden="true">{stock.glyph}</span>
            <span>{stock.label}</span>
          </p>

          {facts.length > 0 ? (
            <dl className="flex flex-wrap gap-x-6 gap-y-2.5">
              {facts.map((fact) => (
                <div key={fact.label} className="min-w-0">
                  <dt className="text-[11px] font-medium uppercase tracking-wide text-muted">
                    {fact.label}
                  </dt>
                  <dd
                    className={`mt-0.5 text-ink ${
                      fact.strong ? "text-[15px] font-semibold tabular-nums" : "text-[13px]"
                    }`}
                    style={{ overflowWrap: "anywhere" }}
                  >
                    {fact.value}
                  </dd>
                </div>
              ))}
            </dl>
          ) : null}

          {sellers.length > 0 ? (
            <div className="min-w-0">
              <p className="text-[11px] font-medium uppercase tracking-wide text-muted">
                {sellers.length === 1 ? "Seller" : `Sellers (${sellers.length})`}
              </p>
              <ul aria-label="Sellers of this product" className="mt-1 flex flex-col gap-1">
                {sellers.map((offer) => {
                  const summary = sellerSummary(offer);
                  return (
                    <li
                      key={offer.name}
                      className="text-[13px] leading-snug text-ink"
                      style={{ overflowWrap: "anywhere" }}
                    >
                      <span className="font-medium">{offer.name}</span>
                      {sameSeller(offer.name, defaultSeller) ? (
                        <span className="text-muted"> (default)</span>
                      ) : null}
                      {summary !== "" ? <span className="text-ink-2"> — {summary}</span> : null}
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : defaultSeller !== null ? (
            <p className="text-[13px] text-ink">
              <span className="text-[11px] font-medium uppercase tracking-wide text-muted">Seller </span>
              {defaultSeller}
            </p>
          ) : null}

          {sellersError !== null ? (
            <p className="text-[12px] leading-snug text-ink-2">
              {sellersError.charAt(0).toUpperCase() + sellersError.slice(1)}. A seller&rsquo;s name can
              still be typed below.
            </p>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

export default ProductPreview;
