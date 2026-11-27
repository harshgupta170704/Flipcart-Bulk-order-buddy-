import { countLabel, formatCount } from "../lib/format";
import type { RetryPlan } from "../lib/types";
import { Alert } from "./Alert";

/** Accounts listed with their last message; the rest are counted. */
const ROWS_SHOWN = 100;

/**
 * What "Retry failed" orders on (GET /jobs/{id}/retry): the run's accounts that
 * got no order, what it leaves out, and the card rule it adds. The server
 * applies the same rules again when the retry starts, so this is what happens.
 */
export function RetryFailedPanel({ plan, cardRun }: { plan: RetryPlan; cardRun: boolean }) {
  const count = plan.accounts.length;
  const leftOut = [
    plan.placed ? `${formatCount(plan.placed)} with an order placed` : "",
    plan.waiting ? `${formatCount(plan.waiting)} waiting for confirmation` : "",
    plan.passed_over ? `${formatCount(plan.passed_over)} passed over on purpose (already bought, limits, target reached)` : "",
  ].filter(Boolean);

  return (
    <div className="flex flex-col gap-2 rounded-control border border-line p-3" aria-label={`Retry of run #${plan.job_id}`}>
      {count === 0 ? (
        <Alert tone="info" title={`Nothing to retry in run #${plan.job_id}`}>
          Every account of that run has an order placed, is waiting for confirmation, or was passed over on purpose.
        </Alert>
      ) : (
        <p className="text-[13px] text-ink">
          <span className="font-semibold">{countLabel(count, "account")}</span> of run #{plan.job_id} got no order and
          will be tried again. Accounts that bought this product since are passed over when the run starts.
        </p>
      )}
      {leftOut.length ? <p className="text-[12px] text-ink-2">Left out: {leftOut.join(", ")}.</p> : null}
      {plan.waiting > 0 ? (
        <Alert tone="warning" title={`${countLabel(plan.waiting, "order")} still waiting for confirmation`}>
          {plan.waiting === 1
            ? "This order may have gone through, so it is not retried: check it on Flipkart first."
            : "These orders may have gone through, so they are not retried: check them on Flipkart first."}{" "}
          The automatic read-back settles them too.
        </Alert>
      ) : null}
      {cardRun && plan.card_cooldown_days ? (
        <p className="text-[12px] text-ink-2">
          Cards: no card that paid for an order since run #{plan.job_id} began is used. &ldquo;Card Eliminate Usage
          Days&rdquo; is set to {plan.card_cooldown_days} and cannot go lower for this retry. Upload the same card sheet:
          used cards are left out of it.
        </p>
      ) : null}
      {plan.units_wanted !== null && plan.units_missing !== null ? (
        <p className="text-[12px] text-ink-2">
          Run #{plan.job_id} asked for {countLabel(plan.units_wanted, "unit")} and got {formatCount(plan.units_bought)}
          {plan.units_missing > 0
            ? `; Total Quantity is set to the ${formatCount(plan.units_missing)} still missing.`
            : ", so nothing is missing."}
        </p>
      ) : null}
      {count > 0 ? (
        <details className="text-[12px]">
          <summary className="cursor-pointer font-medium text-ink">Which accounts, and what happened last time</summary>
          <ul className="mt-1 flex max-h-64 flex-col gap-0.5 overflow-y-auto pl-1">
            {plan.accounts.slice(0, ROWS_SHOWN).map((account) => (
              <li key={account.account_id} className="text-ink-2">
                <span className="text-ink">{account.email ?? `account ${account.account_id}`}</span>
                {" — "}
                {account.message || (account.status === "failed" ? "failed" : "not tried")}
              </li>
            ))}
          </ul>
          {count > ROWS_SHOWN ? (
            <p className="mt-1 text-muted">And {formatCount(count - ROWS_SHOWN)} more.</p>
          ) : null}
        </details>
      ) : null}
    </div>
  );
}
