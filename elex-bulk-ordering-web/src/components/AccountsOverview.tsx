import { useCallback, useEffect, useState } from "react";
import { errorMessage, getBalanceTotals, getFilterOptions, getLoginHealth } from "../lib/api";
import { formatCount, formatCurrency, formatPercent } from "../lib/format";
import type { BalanceTotals, DashboardScope, FilterOptions, LoginHealth } from "../lib/types";
import { Card } from "./Card";
import { Select } from "./Field";
import { SkeletonRows } from "./Skeleton";

type CountKey = "logged_in" | "logged_out" | "not_registered" | "multiple_attempts" | "blocked" | "other";

const COLUMNS: [CountKey, string, string][] = [
  ["logged_in", "Logged in", "var(--status-good)"],
  ["logged_out", "Logged out", "var(--status-warning)"],
  ["not_registered", "Not registered", "var(--axis)"],
  ["multiple_attempts", "Multiple attempts", "#e8590c"],
  ["blocked", "Blocked", "var(--status-critical)"],
  ["other", "Other", "var(--ink-muted)"],
];

const rupees = (value: number) => `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

function Kpi({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-card border border-line bg-surface p-4">
      <p className="text-[12px] text-ink-2">{label}</p>
      <p className="text-[24px] font-semibold tabular-nums text-ink">{value}</p>
      <p className="text-[12px] text-ink-2">{hint}</p>
    </div>
  );
}

/**
 * The dashboard's "Accounts & SuperCoins": the platform, custom-tag and domain
 * filters, the account logins card (share signed in, a segmented bar and a
 * row per platform), and the SuperCoins applied / SuperCoins balance / gift
 * voucher balance cards beside it.
 */
export function AccountsOverview({ refreshKey = 0 }: { refreshKey?: number }) {
  const [scope, setScope] = useState<DashboardScope>({});
  const [options, setOptions] = useState<FilterOptions | null>(null);
  const [logins, setLogins] = useState<LoginHealth | null>(null);
  const [totals, setTotals] = useState<BalanceTotals | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getFilterOptions().then(setOptions, () => undefined);
  }, []);

  const load = useCallback(async () => {
    try {
      const [l, t] = await Promise.all([getLoginHealth(scope), getBalanceTotals(scope)]);
      setLogins(l);
      setTotals(t);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [scope]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const rows = logins?.platforms ?? [];
  const all = rows.reduce(
    (sum, row) => {
      for (const [key] of COLUMNS) sum[key] = (sum[key] ?? 0) + row[key];
      sum.total = (sum.total ?? 0) + row.total;
      return sum;
    },
    {} as Record<string, number>,
  );
  const share = logins && logins.total > 0 ? (logins.logged_in / logins.total) * 100 : null;

  return (
    <section aria-label="Accounts and SuperCoins" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-[13px] font-semibold text-ink">Accounts &amp; SuperCoins</h2>
        <div className="flex flex-wrap gap-2">
          <Select
            aria-label="Platform"
            className="!h-8 w-auto text-[13px]"
            value={scope.platform ?? ""}
            onChange={(e) => setScope((s) => ({ ...s, platform: e.target.value || undefined }))}
          >
            <option value="">All Platforms</option>
            <option value="flipkart">Flipkart / Shopsy</option>
          </Select>
          <Select
            aria-label="Custom tags"
            className="!h-8 w-auto text-[13px]"
            value={scope.tag === undefined || scope.tag === null ? "" : String(scope.tag)}
            onChange={(e) =>
              setScope((s) => ({
                ...s,
                tag: e.target.value === "" ? null : e.target.value === "untagged" ? "untagged" : Number(e.target.value),
              }))
            }
          >
            <option value="">Custom tags: All</option>
            <option value="untagged">Untagged</option>
            {(options?.tags ?? []).map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name}
              </option>
            ))}
          </Select>
          <Select
            aria-label="Domains"
            className="!h-8 w-auto text-[13px]"
            value={scope.domain ?? ""}
            onChange={(e) => setScope((s) => ({ ...s, domain: e.target.value || undefined }))}
          >
            <option value="">Domains: All</option>
            {(options?.domains ?? []).map((domain) => (
              <option key={domain.value} value={domain.value}>
                {domain.value}
              </option>
            ))}
          </Select>
        </div>
      </div>
      {error ? <p className="text-[13px] text-ink">{error}</p> : null}
      <div className="grid gap-3 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card title="Account logins" description="Email IDs by login status and platform">
          {logins === null ? (
            <SkeletonRows rows={3} />
          ) : (
            <div className="flex flex-col gap-3">
              <p className="text-right">
                <span className="text-[24px] font-semibold tabular-nums text-ink">
                  {share === null ? "—" : formatPercent(share)}
                </span>
                <span className="block text-[12px] text-ink-2">
                  {formatCount(logins.logged_in)} of {formatCount(logins.total)} logged in
                </span>
              </p>
              {logins.total > 0 ? (
                <div className="flex h-2 w-full overflow-hidden rounded-full bg-page" aria-hidden="true">
                  {COLUMNS.map(([key, , color]) => (
                    <div key={key} style={{ width: `${((all[key] ?? 0) / logins.total) * 100}%`, backgroundColor: color }} />
                  ))}
                </div>
              ) : null}
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-[13px]">
                  <thead>
                    <tr className="text-[12px] text-ink-2">
                      <th className="py-1.5 text-left font-normal">Platform</th>
                      {COLUMNS.map(([key, label, color]) => (
                        <th key={key} className="py-1.5 text-right font-normal">
                          <span aria-hidden="true" className="mr-1 inline-block size-1.5 rounded-full align-middle" style={{ backgroundColor: color }} />
                          {label}
                        </th>
                      ))}
                      <th className="py-1.5 text-right font-normal">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.platform} className="border-t border-line">
                        <th scope="row" className="py-1.5 text-left font-medium text-ink">
                          {row.platform}
                        </th>
                        {COLUMNS.map(([key]) => (
                          <td key={key} className="py-1.5 text-right tabular-nums">
                            {formatCount(row[key])}
                          </td>
                        ))}
                        <td className="py-1.5 text-right tabular-nums">{formatCount(row.total)}</td>
                      </tr>
                    ))}
                    <tr className="border-t border-line font-semibold">
                      <th scope="row" className="py-1.5 text-left">
                        All platforms
                      </th>
                      {COLUMNS.map(([key]) => (
                        <td key={key} className="py-1.5 text-right tabular-nums">
                          {formatCount(all[key] ?? 0)}
                        </td>
                      ))}
                      <td className="py-1.5 text-right tabular-nums">{formatCount(all.total ?? 0)}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </Card>
        <div className="flex flex-col gap-3">
          <Kpi
            label="SuperCoins applied"
            value={totals ? formatCurrency(totals.supercoins_applied_amount ?? 0) : "—"}
            hint={
              totals
                ? `On delivered orders · ${formatCount(totals.supercoins_orders_delivered)} order(s)`
                : "On delivered orders"
            }
          />
          <Kpi
            label="SuperCoins balance"
            value={totals ? formatCount(totals.supercoins_balance) : "—"}
            hint={totals ? `Across selected accounts · ${formatCount(totals.accounts_read)} read` : "Across selected accounts"}
          />
          <Kpi
            label="Gift voucher balance"
            value={totals ? rupees(totals.gift_voucher_balance) : "—"}
            hint="Across selected accounts"
          />
        </div>
      </div>
    </section>
  );
}

export default AccountsOverview;
