import { useCallback, useEffect, useState } from "react";
import { errorMessage, getLoginHealth } from "../lib/api";
import { formatCount, formatPercent } from "../lib/format";
import type { LoginHealth as LoginHealthData } from "../lib/types";
import { Button } from "./Button";
import { Card } from "./Card";
import { SkeletonRows } from "./Skeleton";
import { statusMeta } from "./StatusBadge";

/**
 * Account logins (step G): the share of accounts signed in, and how many are
 * in each other state, so a drop in sign-ins is seen before a run needs them.
 */
export function LoginHealth({ className = "" }: { className?: string }) {
  const [data, setData] = useState<LoginHealthData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getLoginHealth());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const share = data && data.total > 0 ? (data.logged_in / data.total) * 100 : null;
  const shown = data ? data.statuses.filter((row) => row.count > 0) : [];

  return (
    <Card
      title="Account logins"
      description="How many accounts are signed in to Flipkart, and why the rest are not."
      className={className}
    >
      {error ? (
        <p role="status" className="flex flex-wrap items-center gap-2 text-[13px] text-ink-2">
          Could not load the login counts: {error}
          <Button size="sm" variant="ghost" onClick={() => void load()}>
            Try again
          </Button>
        </p>
      ) : data === null ? (
        <SkeletonRows rows={3} />
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-[13px] text-ink">
            <span className="text-[22px] font-semibold tabular-nums">{share === null ? "—" : formatPercent(share)}</span>{" "}
            signed in · {formatCount(data.logged_in)} of {formatCount(data.total)} accounts
          </p>
          {data.total > 0 ? (
            <div className="flex h-2 w-full overflow-hidden rounded bg-page" aria-hidden="true">
              {shown.map((row) => (
                <div
                  key={row.status}
                  className={row.status === "session_valid" ? "bg-good" : row.status === "new" ? "bg-line" : "bg-warning"}
                  style={{ width: `${(row.count / data.total) * 100}%` }}
                  title={`${statusMeta(row.status).label}: ${formatCount(row.count)}`}
                />
              ))}
            </div>
          ) : null}
          <table className="w-full text-[13px]">
            <caption className="sr-only">Accounts by login status</caption>
            <tbody>
              {data.statuses.map((row) => (
                <tr key={row.status} className="border-t border-line">
                  <th scope="row" className="py-1.5 text-left font-normal text-ink-2">
                    {statusMeta(row.status).label}
                  </th>
                  <td className="py-1.5 text-right tabular-nums text-ink">{formatCount(row.count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default LoginHealth;
