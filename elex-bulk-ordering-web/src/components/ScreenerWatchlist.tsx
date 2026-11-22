import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  addScreenerItem,
  checkAllScreenerItems,
  checkScreenerItem,
  deleteScreenerItem,
  errorMessage,
  getScreenerConfig,
  listScreenerItems,
  updateScreenerConfig,
  updateScreenerItem,
} from "../lib/api";
import { formatCurrency, formatDateTime } from "../lib/format";
import type { ScreenerConfig, ScreenerItem } from "../lib/types";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { Checkbox, Field, NumberInput, Select, TextInput } from "./Field";
import { StatusBadge } from "./StatusBadge";
import { Table, type Column } from "./Table";

export function ScreenerWatchlist() {
  const [config, setConfig] = useState<ScreenerConfig>({
    master_email: "",
    banks: [],
    check_every_minutes: 15,
    supercoin_offers: true,
    coupon_offers: true,
    enabled: false,
  });
  const [banksText, setBanksText] = useState("");
  const [items, setItems] = useState<ScreenerItem[]>([]);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [target, setTarget] = useState("");
  const [savingCfg, setSavingCfg] = useState(false);
  const [adding, setAdding] = useState(false);
  const [checkingId, setCheckingId] = useState<number | null>(null);
  const [checkingAll, setCheckingAll] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [cfg, list] = await Promise.all([getScreenerConfig(), listScreenerItems()]);
      setConfig(cfg);
      setBanksText(cfg.banks.join(", "));
      setItems(list);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const saveConfig = async (e: FormEvent) => {
    e.preventDefault();
    setSavingCfg(true);
    setError(null);
    setNotice(null);
    try {
      const banks = banksText
        .split(/[,;\n]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const saved = await updateScreenerConfig({ ...config, banks });
      setConfig(saved);
      setBanksText(saved.banks.join(", "));
      setNotice("Screener settings saved to database.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSavingCfg(false);
    }
  };

  const addItem = async (e: FormEvent) => {
    e.preventDefault();
    setAdding(true);
    setError(null);
    setNotice(null);
    try {
      await addScreenerItem({
        product_url: url.trim(),
        product_title: title.trim() || null,
        target_price: target.trim() ? Number(target) : null,
      });
      setUrl("");
      setTitle("");
      setTarget("");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setAdding(false);
    }
  };

  const checkOne = async (id: number) => {
    setCheckingId(id);
    setError(null);
    try {
      const updated = await checkScreenerItem(id);
      setItems((prev) => prev.map((it) => (it.id === id ? updated : it)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setCheckingId(null);
    }
  };

  const checkAll = async () => {
    setCheckingAll(true);
    setError(null);
    try {
      setItems(await checkAllScreenerItems());
      setNotice("Checked all watchlist products.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setCheckingAll(false);
    }
  };

  const columns: Column<ScreenerItem>[] = [
    {
      key: "product",
      header: "Product",
      render: (r) => (
        <div className="max-w-[260px]">
          <a href={r.product_url} target="_blank" rel="noreferrer" className="font-medium text-accent underline underline-offset-2">
            {r.product_title || r.product_url}
          </a>
          {r.last_error ? <div className="text-[11.5px] text-[var(--status-bad)]">{r.last_error}</div> : null}
        </div>
      ),
    },
    { key: "listed_price", header: "Listed", numeric: true, render: (r) => (r.listed_price != null ? formatCurrency(r.listed_price) : "—") },
    { key: "best_offer", header: "Best offer", render: (r) => <span className="line-clamp-2 max-w-[200px] text-[12px]">{r.best_offer || "—"}</span> },
    { key: "offer_applied", header: "Offer applied", render: (r) => <span className="line-clamp-2 max-w-[180px] text-[12px]">{r.offer_applied || "—"}</span> },
    {
      key: "combos",
      header: "Offer combos (pick one)",
      render: (r) =>
        r.offer_combos && r.offer_combos.length > 0 ? (
          <Select
            aria-label={`Offer combo for ${r.product_title || r.id}`}
            value={r.offer_applied ?? ""}
            onChange={(e) => {
              const picked = r.offer_combos?.find((c) => c.label === e.target.value);
              if (!picked) return;
              void updateScreenerItem(r.id, { offer_applied: picked.label, net_price: picked.net_price }).then(
                (u) => setItems((prev) => prev.map((it) => (it.id === u.id ? u : it))),
                (err) => setError(errorMessage(err)),
              );
            }}
          >
            {r.offer_combos.map((c, idx) => (
              <option key={idx} value={c.label}>
                {formatCurrency(c.net_price)} — {c.label.slice(0, 45)}
              </option>
            ))}
          </Select>
        ) : (
          "—"
        ),
    },
    {
      key: "net_price",
      header: "Net price",
      numeric: true,
      render: (r) => {
        if (r.net_price == null) return "—";
        const hit = r.target_price != null && r.net_price <= r.target_price;
        return <span className={hit ? "font-semibold text-[var(--status-good)]" : "font-medium"}>{formatCurrency(r.net_price)}</span>;
      },
    },
    { key: "target_price", header: "Target", numeric: true, render: (r) => (r.target_price != null ? formatCurrency(r.target_price) : "—") },
    {
      key: "stock",
      header: "Stock",
      render: (r) =>
        r.in_stock === true ? (
          <StatusBadge status="done" label="In stock" />
        ) : r.in_stock === false ? (
          <StatusBadge status="failed" label="Out of stock" />
        ) : (
          <span className="text-muted">—</span>
        ),
    },
    { key: "last_checked_at", header: "Checked", render: (r) => (r.last_checked_at ? formatDateTime(r.last_checked_at) : "Never") },
    {
      key: "actions",
      header: "Actions",
      render: (r) => (
        <div className="flex flex-wrap gap-1">
          <Button size="sm" variant="ghost" loading={checkingId === r.id} onClick={() => void checkOne(r.id)}>
            Check
          </Button>
          <Link
            to={`/ordering?${new URLSearchParams({ product: r.product_url, ...(r.target_price != null ? { target: String(r.target_price) } : {}) }).toString()}`}
            className="inline-flex h-[30px] items-center rounded-control border border-line px-2.5 text-[12px] font-medium text-ink hover:bg-page"
          >
            Order
          </Link>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void deleteScreenerItem(r.id).then(load, (e) => setError(errorMessage(e)))}
          >
            Remove
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}

      <Card title="Watchlist Screener Settings">
        <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" onSubmit={(e) => void saveConfig(e)}>
          <Field label="Master email">
            <TextInput
              value={config.master_email}
              onChange={(e) => setConfig({ ...config, master_email: e.target.value })}
              placeholder="account@domain.com"
            />
          </Field>
          <Field label="Banks you have cards for" hint="Comma-separated (e.g. HDFC, ICICI, SBI, Axis)">
            <TextInput value={banksText} onChange={(e) => setBanksText(e.target.value)} placeholder="HDFC, ICICI, SBI" />
          </Field>
          <Field label="Check every (minutes)">
            <NumberInput
              min={0}
              max={1440}
              value={String(config.check_every_minutes)}
              onChange={(e) => setConfig({ ...config, check_every_minutes: Number(e.target.value) || 0 })}
            />
          </Field>
          <div className="flex flex-col justify-end gap-2">
            <Checkbox
              checked={config.supercoin_offers}
              onChange={(checked) => setConfig({ ...config, supercoin_offers: checked })}
              label="SuperCoin offers"
            />
            <Checkbox
              checked={config.coupon_offers}
              onChange={(checked) => setConfig({ ...config, coupon_offers: checked })}
              label="Coupon / deal offers"
            />
            <Checkbox
              checked={config.enabled}
              onChange={(checked) => setConfig({ ...config, enabled: checked })}
              label="Screener on"
            />
          </div>
          <div className="sm:col-span-2 lg:col-span-4 flex justify-end">
            <Button type="submit" variant="primary" size="sm" loading={savingCfg}>
              Save screener config
            </Button>
          </div>
        </form>
      </Card>

      <Card title={`Add Product to Watchlist (${items.length}/20 slots)`}>
        <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" onSubmit={(e) => void addItem(e)}>
          <Field label="Flipkart Product URL" required className="lg:col-span-2">
            <TextInput value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.flipkart.com/…" />
          </Field>
          <Field label="Label (optional)">
            <TextInput value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Auto-filled on check" />
          </Field>
          <Field label="Target Price (₹)">
            <NumberInput min={0} value={target} onChange={(e) => setTarget(e.target.value)} placeholder="e.g. 14999" />
          </Field>
          <div className="sm:col-span-2 lg:col-span-4 flex justify-end gap-2">
            <Button type="button" size="sm" loading={checkingAll} disabled={items.length === 0} onClick={() => void checkAll()}>
              Check all
            </Button>
            <Button type="submit" size="sm" variant="primary" loading={adding} disabled={!url.trim() || items.length >= 20}>
              Add to watchlist
            </Button>
          </div>
        </form>
      </Card>

      <Card flush title={`Tracked Products (${items.length}/20)`}>
        <Table
          caption="Screener watchlist"
          columns={columns}
          rows={items}
          rowKey={(r) => r.id}
          dense
          minWidth="1200px"
          empty="No products in your watchlist yet. Add up to 20 Flipkart URLs above."
        />
      </Card>
    </div>
  );
}
