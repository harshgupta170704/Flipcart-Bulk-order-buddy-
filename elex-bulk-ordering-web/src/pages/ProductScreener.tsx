/**
 * Product Screener: look at products before buying them. Search Flipkart by
 * name (or paste a link), sort what comes back by price, discount or rating,
 * and open one to see its pictures, list price, discount, stock, variants and
 * every seller — then hand it to Place orders in one click.
 *
 * Every search and every detail opens a real browser on the server, so both run
 * when asked, never per keystroke; the server keeps answers a few minutes.
 */
import { useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { errorMessage, previewProduct, searchProducts } from "../lib/api";
import { formatCount, formatCurrency } from "../lib/format";
import type { ProductPreview as PreviewData, ProductSearchResult } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { TextInput } from "../components/Field";
import { ProductPreview } from "../components/ProductPreview";
import { MAX_QUERY_CHARS, MIN_QUERY_CHARS } from "../components/ProductSearch";
import { ScreenerWatchlist } from "../components/ScreenerWatchlist";

type SortKey = "relevance" | "price_asc" | "price_desc" | "discount" | "rating";

const SORTS: { key: SortKey; label: string }[] = [
  { key: "relevance", label: "Flipkart's order" },
  { key: "price_asc", label: "Price: low to high" },
  { key: "price_desc", label: "Price: high to low" },
  { key: "discount", label: "Biggest discount" },
  { key: "rating", label: "Best rated" },
];

/** Missing figures sort last whichever way the list is sorted. */
function by(value: (r: ProductSearchResult) => number | null | undefined, descending: boolean) {
  return (a: ProductSearchResult, b: ProductSearchResult) => {
    const x = value(a);
    const y = value(b);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return descending ? y - x : x - y;
  };
}

export function sortResults(results: ProductSearchResult[], key: SortKey): ProductSearchResult[] {
  const list = [...results];
  switch (key) {
    case "price_asc":
      return list.sort(by((r) => r.price, false));
    case "price_desc":
      return list.sort(by((r) => r.price, true));
    case "discount":
      return list.sort(by((r) => r.discount_pct, true));
    case "rating":
      return list.sort(by((r) => r.rating, true));
    default:
      return list;
  }
}

function isLink(text: string): boolean {
  return /^https?:\/\/([a-z0-9-]+\.)*flipkart\.com\//i.test(text.trim());
}

function orderLink(link: string): string {
  return `/ordering?${new URLSearchParams({ product: link }).toString()}`;
}

function Price({ price, mrp, discount }: { price: number | null; mrp?: number | null; discount?: number | null }) {
  if (price === null) return <span className="text-[13px] text-muted">Price not shown</span>;
  return (
    <span className="flex flex-wrap items-baseline gap-x-2">
      <span className="text-[15px] font-semibold text-ink">{formatCurrency(price)}</span>
      {mrp ? <span className="text-[12px] text-muted line-through">{formatCurrency(mrp)}</span> : null}
      {discount ? (
        <span className="text-[12px] font-medium" style={{ color: "var(--status-good)" }}>
          {discount}% off
        </span>
      ) : null}
    </span>
  );
}

function ResultCard({ result, active, onOpen }: { result: ProductSearchResult; active: boolean; onOpen: () => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        aria-pressed={active}
        className={`flex h-full w-full flex-col gap-2 rounded-card border bg-surface p-3 text-left transition-colors hover:bg-page ${
          active ? "border-ink" : "border-line"
        }`}
      >
        <div className="flex h-32 items-center justify-center">
          {result.image ? (
            <img src={result.image} alt="" loading="lazy" className="max-h-32 max-w-full object-contain" />
          ) : (
            <span className="text-[12px] text-muted">No picture</span>
          )}
        </div>
        <span className="line-clamp-2 text-[13px] font-medium text-ink">{result.title}</span>
        <Price price={result.price} mrp={result.mrp} discount={result.discount_pct} />
        {result.rating !== null ? <span className="text-[12px] text-ink-2">★ {result.rating.toFixed(1)}</span> : null}
        {result.highlights && result.highlights.length > 0 ? (
          <ul className="list-disc pl-4 text-[12px] text-ink-2">
            {result.highlights.map((line) => (
              <li key={line} className="truncate">
                {line}
              </li>
            ))}
          </ul>
        ) : null}
      </button>
    </li>
  );
}

function Details({
  link,
  preview,
  loading,
  error,
  onVariant,
}: {
  link: string;
  preview: PreviewData | null;
  loading: boolean;
  error: string | null;
  onVariant: (link: string) => void;
}) {
  const images = preview?.images?.length ? preview.images : preview?.image ? [preview.image] : [];
  const [shown, setShown] = useState(0);
  const picture = images[Math.min(shown, images.length - 1)];
  const ready = preview !== null && !preview.error && !loading;

  return (
    <Card
      title="Product details"
      actions={
        <Link
          to={orderLink(link)}
          className="inline-flex h-[38px] items-center rounded-control bg-ink px-4 text-sm font-medium text-surface transition-opacity hover:opacity-90"
        >
          Order this product
        </Link>
      }
    >
      <div className="flex flex-col gap-4">
        {ready && images.length > 0 ? (
          <div className="flex flex-col gap-2">
            <div className="flex h-64 items-center justify-center rounded-card border border-line bg-white p-2">
              <img src={picture} alt={preview?.name ?? ""} className="max-h-60 max-w-full object-contain" />
            </div>
            {images.length > 1 ? (
              <div className="flex gap-2 overflow-x-auto" aria-label="Pictures">
                {images.map((src, index) => (
                  <button
                    key={src}
                    type="button"
                    onClick={() => setShown(index)}
                    aria-label={`Picture ${index + 1}`}
                    className={`size-14 shrink-0 rounded-control border bg-white p-1 ${index === shown ? "border-ink" : "border-line"}`}
                  >
                    <img src={src} alt="" className="size-full object-contain" />
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        {ready && (preview.mrp || preview.discount_pct) ? (
          <Price price={preview.price} mrp={preview.mrp} discount={preview.discount_pct} />
        ) : null}

        {/* Name, brand, price, stock, rating and every seller. */}
        <ProductPreview preview={preview} loading={loading} error={error} />

        {ready && preview.variants && preview.variants.length > 0 ? (
          <div>
            <h3 className="mb-2 text-[13px] font-medium text-ink">
              Variants ({formatCount(preview.variants.length)})
            </h3>
            <div className="flex flex-wrap gap-2">
              {preview.variants.map((variant) => (
                <button
                  key={variant.pid}
                  type="button"
                  onClick={() => onVariant(variant.link)}
                  aria-label={variant.label}
                  className="flex flex-col items-start rounded-control border border-line px-3 py-1 text-left text-[12px] text-ink transition-colors hover:bg-page"
                >
                  <span className="font-medium">{variant.label}</span>
                  {variant.in_stock === false ? (
                    <span className="text-muted">Out of stock</span>
                  ) : variant.price ? (
                    <span className="text-ink-2">
                      {formatCurrency(variant.price)}
                      {variant.discount_pct ? ` · ${variant.discount_pct}% off` : ""}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </Card>
  );
}

export default function ProductScreener() {
  const [tab, setTab] = useState<"search" | "watchlist">("search");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ProductSearchResult[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [sort, setSort] = useState<SortKey>("relevance");

  const [openLink, setOpenLink] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewData | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  /* Only the newest request of each kind may land. */
  const searchTicket = useRef(0);
  const previewTicket = useRef(0);

  const words = query.trim().replace(/\s+/g, " ");
  const pasted = isLink(words);

  const open = async (link: string) => {
    const mine = ++previewTicket.current;
    setOpenLink(link);
    setPreview(null);
    setPreviewError(null);
    setPreviewing(true);
    try {
      const answer = await previewProduct(link);
      if (previewTicket.current === mine) setPreview(answer);
    } catch (err) {
      if (previewTicket.current === mine) setPreviewError(errorMessage(err));
    } finally {
      if (previewTicket.current === mine) setPreviewing(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pasted) {
      void open(words);
      return;
    }
    if (words.length < MIN_QUERY_CHARS) return;
    const mine = ++searchTicket.current;
    setSearching(true);
    setSearchError(null);
    try {
      const answer = await searchProducts(words);
      if (searchTicket.current !== mine) return;
      setResults(answer.results);
      setSearchError(answer.error);
    } catch (err) {
      if (searchTicket.current !== mine) return;
      setResults(null);
      setSearchError(errorMessage(err));
    } finally {
      if (searchTicket.current === mine) setSearching(false);
    }
  };

  const sorted = useMemo(() => (results ? sortResults(results, sort) : null), [results, sort]);

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="text-[20px] font-semibold leading-tight tracking-tight text-ink">Product Screener</h1>
        <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">
          Search Flipkart or monitor up to 20 products in your watchlist for live net prices, bank offers, SuperCoins,
          and target-price hits before placing orders.
        </p>
      </header>

      <div role="tablist" aria-label="Product Screener modes" className="flex w-full max-w-md rounded-control border border-line bg-page p-1">
        {(
          [
            ["search", "Search Flipkart"],
            ["watchlist", "Watchlist (20 slots)"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`flex-1 rounded-[6px] px-4 py-2 text-[13px] font-medium transition-colors ${
              tab === id ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "watchlist" ? (
        <ScreenerWatchlist />
      ) : (
        <>
          <Card>
        <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <label htmlFor="screener-query" className="text-[13px] font-medium text-ink">
              Product name or link
            </label>
            <TextInput
              id="screener-query"
              value={query}
              maxLength={2048}
              placeholder="e.g. vivo t5 lite, or https://www.flipkart.com/…"
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <Button
            type="submit"
            variant="primary"
            loading={searching}
            disabled={!pasted && (words.length < MIN_QUERY_CHARS || words.length > MAX_QUERY_CHARS)}
          >
            {pasted ? "Open" : searching ? "Searching…" : "Search"}
          </Button>
        </form>
      </Card>

      {searchError ? (
        <Alert tone={results && results.length > 0 ? "warning" : "error"} title="Search">
          {searchError}
        </Alert>
      ) : null}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
        <section aria-label="Results" className="min-w-0">
          {sorted === null ? (
            searching ? null : (
              <EmptyState
                title="Find a product"
                description="Type a product name and press Search. Results show price, list price, discount, rating and key specs."
              />
            )
          ) : sorted.length === 0 ? (
            searchError ? null : <EmptyState title="Nothing found" description="Try other words, or paste the product link." />
          ) : (
            <div className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-[13px] text-ink-2">{formatCount(sorted.length)} results</p>
                <label className="flex items-center gap-2 text-[13px] text-ink-2">
                  Sort
                  <select
                    value={sort}
                    onChange={(event) => setSort(event.target.value as SortKey)}
                    className="h-[34px] rounded-control border border-line bg-surface px-2 text-ink"
                  >
                    {SORTS.map((option) => (
                      <option key={option.key} value={option.key}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {sorted.map((result) => (
                  <ResultCard
                    key={result.pid}
                    result={result}
                    active={result.link === openLink}
                    onOpen={() => void open(result.link)}
                  />
                ))}
              </ul>
            </div>
          )}
        </section>

        {openLink ? (
          <aside aria-label="Product details" className="min-w-0">
            <Details
              key={openLink}
              link={openLink}
              preview={preview}
              loading={previewing}
              error={previewError}
              onVariant={(link) => void open(link)}
            />
          </aside>
        ) : null}
      </div>
        </>
      )}
    </div>
  );
}
