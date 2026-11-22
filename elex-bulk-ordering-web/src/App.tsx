import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import { AuthProvider, RequireAdmin, RequireAuth } from "./lib/auth";
import { Layout } from "./components/Layout";
import { Card } from "./components/Card";
import Dashboard from "./pages/Dashboard";
import Accounts from "./pages/Accounts";
import AddressBook from "./pages/AddressBook";
import Sessions from "./pages/Sessions";
import Coupons from "./pages/Coupons";
import Ordering from "./pages/Ordering";
import ProductScreener from "./pages/ProductScreener";
import OrderUnits from "./pages/OrderUnits";
import Alerts from "./pages/Alerts";
import Requests from "./pages/Requests";
import UnitLimits from "./pages/UnitLimits";
import Settings from "./pages/Settings";
import Users from "./pages/Users";
import Agents from "./pages/Agents";
import Orders from "./pages/Orders";
import GiftVouchers from "./pages/GiftVouchers";
import CorpAssist from "./pages/CorpAssist";
import EmployeePhones from "./pages/EmployeePhones";
import Inventory from "./pages/Inventory";
import OrderPhones from "./pages/OrderPhones";
import { ConfigsTabs } from "./components/ConfigsTabs";
import ChangePassword from "./pages/ChangePassword";
import ForgotPassword from "./pages/ForgotPassword";
import ResetPassword from "./pages/ResetPassword";
import Login from "./pages/Login";

function NotFound() {
  return (
    <Card title="Page not found" description="That route does not exist in this app.">
      <Link
        to="/"
        className="inline-flex h-[38px] items-center rounded-control border border-line px-4 text-sm font-medium text-ink hover:bg-page"
      >
        Back to the dashboard
      </Link>
    </Card>
  );
}

function AdminsOnly() {
  return (
    <Card title="Admins only" description="Only an admin can open this page. An admin can change your role under Users.">
      <Link
        to="/"
        className="inline-flex h-[38px] items-center rounded-control border border-line px-4 text-sm font-medium text-ink hover:bg-page"
      >
        Back to the dashboard
      </Link>
    </Card>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/forgot-password" element={<ForgotPassword />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route element={<RequireAuth />}>
            <Route element={<Layout />}>
              <Route index element={<Dashboard />} />
              <Route path="dashboard" element={<Dashboard />} />
              <Route element={<ConfigsTabs />}>
                <Route path="accounts" element={<Accounts />} />
                <Route path="platform-accounts" element={<Accounts />} />
                <Route path="sessions" element={<Sessions />} />
                <Route path="coupons" element={<Coupons />} />
                <Route path="addresses" element={<AddressBook />} />
                <Route path="limits" element={<UnitLimits />} />
                <Route path="gift-vouchers" element={<GiftVouchers />} />
                <Route path="order-phones" element={<OrderPhones />} />
              </Route>
              <Route path="hdfc-corpassist" element={<CorpAssist />} />
              <Route path="corpassist" element={<CorpAssist />} />
              <Route path="employee-phones" element={<EmployeePhones />} />
              <Route path="ordering" element={<Ordering />} />
              <Route path="smart-bulk-order" element={<Ordering />} />
              <Route path="products" element={<ProductScreener />} />
              <Route path="screener" element={<ProductScreener />} />
              <Route path="inventory" element={<Inventory />} />
              <Route path="orders" element={<Orders />} />
              <Route path="order-units" element={<OrderUnits />} />
              <Route path="alerts" element={<Alerts />} />
              <Route path="requests" element={<Requests />} />
              <Route path="agents" element={<Agents />} />
              <Route path="change-password" element={<ChangePassword />} />
              {/* Admin-only pages: the API refuses an operator too; this is the courteous half. */}
              <Route element={<RequireAdmin fallback={<AdminsOnly />} />}>
                <Route path="settings" element={<Settings />} />
                <Route path="users" element={<Users />} />
              </Route>
              <Route path="*" element={<NotFound />} />
            </Route>
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
