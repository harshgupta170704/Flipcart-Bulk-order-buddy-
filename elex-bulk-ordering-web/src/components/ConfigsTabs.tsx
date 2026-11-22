import { NavLink, Outlet } from "react-router-dom";

/** The tabs of Platform Accounts & Configs, each its own page and address. */
export const CONFIG_TABS: { to: string; label: string }[] = [
  { to: "/accounts", label: "Accounts" },
  { to: "/gift-vouchers", label: "Gift Vouchers" },
  { to: "/sessions", label: "Account Sessions" },
  { to: "/coupons", label: "Coupons" },
  { to: "/order-phones", label: "Order Phone Numbers" },
  { to: "/addresses", label: "Addresses & GSTs" },
  { to: "/limits", label: "Product Limits" },
];

/**
 * Platform Accounts & Configs: the setup pages under one heading with a tab
 * strip. Each tab keeps its own route, so every existing link and bookmark
 * still lands on the right one.
 */
export function ConfigsTabs() {
  return (
    <div className="flex flex-col gap-5">
      <div
        role="tablist"
        aria-label="Platform Accounts & Configs"
        className="flex w-full overflow-x-auto rounded-control border border-line bg-page p-1"
      >
        {CONFIG_TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            role="tab"
            className={({ isActive }) =>
              `flex-1 whitespace-nowrap rounded-[6px] px-4 py-2 text-center text-[13px] font-medium transition-colors ${
                isActive ? "bg-surface text-ink shadow-sm" : "text-ink-2 hover:text-ink"
              }`
            }
          >
            {({ isActive }) => <span aria-selected={isActive}>{tab.label}</span>}
          </NavLink>
        ))}
      </div>
      <Outlet />
    </div>
  );
}

export default ConfigsTabs;
