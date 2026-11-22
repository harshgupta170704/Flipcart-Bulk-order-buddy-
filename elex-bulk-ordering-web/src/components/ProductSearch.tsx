/**
 * "Search Flipkart" above the Product link (26 Oct 2026): type a name, pick
 * the exact variant, and the link is filled in — the product preview then
 * loads as if the link had been pasted. Pasting a link still works as before.
 *
 * A search opens a real browser on the server, so it runs when asked (Search,
 * or Enter), not on every keystroke; the server keeps the same words' answer
 * for a few minutes.
 */
import { useRef, useState, type FormEvent } from "react";
import { errorMessage, searchProducts } from "../lib/api";
import { formatCurrency } from "../lib/format";
import type { ProductSearchResult } from "../lib/types";
import { Button } from "./Button";
import { TextInput } from "./Field";

/** The server's bounds on a query (MIN/MAX_QUERY_CHARS in automation/search.py). */
export const MIN_QUERY_CHARS = 2;
export const MAX_QUERY_CHARS = 100;

export function ProductSearch({ onPick, disabled = false }: { onPick: (link: string) => void; disabled?: boolean }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ProductSearchResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /* Only the newest search may land: an older one finishing late must not
     replace the list the operator is looking at. */
  const request = useRef(0);

  const words = query.trim().replace(/\s+/g, " ");
  const tooShort = words.length < MIN_QUERY_CHARS;

  const search = async (event?: FormEvent) => {
    event?.preventDefault();
    if (tooShort || disabled) return;
    const mine = ++request.current;
    setBusy(true);
    setError(null);
    try {
      const answer = await searchProducts(words);
      if (request.current !== mine) return;
      setResults(answer.results);
      setError(answer.error);
    } catch (err) {
      if (request.current !== mine) return;
      setResults(null);
      setError(errorMessage(err));
    } finally {
      if (request.current === mine) setBusy(false);
    }
  };

  const pick = (result: ProductSearchResult) => {
    onPick(result.link);
    setResults(null);
  };

  return (
    <div className="flex flex-col gap-2">
      <label htmlFor="product-search" className="text-[13px] font-medium text-ink">
        Search Flipkart
      </label>
      {/* Not a <form>: this sits inside the order form, and Enter here must
          search rather than start a run. */}
      <div className="flex gap-2">
        <TextInput
          id="product-search"
          name="product_search"
          type="search"
          autoComplete="off"
          placeholder="Type a product name, e.g. vivo T5 Lite"
          maxLength={MAX_QUERY_CHARS}
          value={query}
          disabled={disabled}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void search(event);
          }}
        />
        <Button type="button" variant="secondary" onClick={() => void search()} loading={busy} disabled={disabled || tooShort || busy}>
          Search
        </Button>
      </div>
      <p className="text-[12px] text-muted">Pick a result to fill in the product link below, or paste a link yourself.</p>
      {error !== null ? (
        <p role="status" className="text-[12px] text-ink-2">
          {error}
        </p>
      ) : null}
      {results !== null && results.length === 0 && error === null ? (
        <p role="status" className="text-[12px] text-ink-2">
          Nothing found for &ldquo;{words}&rdquo;. Try other words, or paste the product link.
        </p>
      ) : null}
      {results !== null && results.length > 0 ? (
        <ul aria-label="Search results" className="flex flex-col divide-y divide-line rounded-control border border-line">
          {results.map((result) => (
            <li key={result.pid}>
              <button
                type="button"
                className="flex w-full items-baseline justify-between gap-3 px-3 py-2 text-left text-[13px] hover:bg-page focus-visible:bg-page"
                onClick={() => pick(result)}
                disabled={disabled}
              >
                <span className="min-w-0 flex-1 text-ink">{result.title}</span>
                <span className="shrink-0 whitespace-nowrap tabular-nums text-ink-2">
                  {result.price === null ? "" : formatCurrency(result.price)}
                  {result.rating === null ? "" : ` · ${result.rating.toFixed(1)}★`}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export default ProductSearch;
