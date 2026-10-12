import { NavLink, Outlet } from "react-router-dom";
import { ClipboardList, KeyRound, LayoutGrid, LogOut, ShieldCheck } from "lucide-react";
import { ApiError, compositeUrl } from "../api/client";
import { useMe } from "../api/hooks";
import { isDevMode, logout } from "../auth";
import NotPartnerPage from "../pages/NotPartnerPage";
import "./Layout.css";

// The OpenG2P shell, as in the Master Data UI: a black left pane with the full
// logo and the product name, the menu with icons, and the user at the bottom.
const navClass = ({ isActive }: { isActive: boolean }) => `nav-item${isActive ? " active" : ""}`;

export default function Layout() {
  const me = useMe();

  // 403 on /me: signed in, but no partner_id claim or no partner role.
  if (me.error instanceof ApiError && me.error.status === 403) return <NotPartnerPage />;

  return (
    <div className="shell">
      <aside className="sidebar">
        <NavLink to="/requests" className="brand" title="Partner Portal">
          <img src="/openg2p-logo-horizontal.svg" alt="OpenG2P" className="brand-logo" />
          <span className="brand-product">Partner Portal</span>
        </NavLink>
        <nav className="nav">
          <NavLink to="/requests" className={navClass}>
            <ClipboardList size={18} /> Requests
          </NavLink>
          <NavLink to="/consents" className={navClass}>
            <ShieldCheck size={18} /> Consents
          </NavLink>
          <NavLink to="/access" className={navClass}>
            <KeyRound size={18} /> What we can ask for
          </NavLink>
          {compositeUrl() && (
            <NavLink to="/use-cases" className={navClass}>
              <LayoutGrid size={18} /> Use cases
            </NavLink>
          )}
        </nav>
        <div className="sidebar-user">
          {isDevMode() && <span className="dev-pill">dev</span>}
          {me.data && (
            <div className="user" title={me.data.roles.join(", ")}>
              <div className="user-name">{me.data.name || me.data.username}</div>
              <div className="user-partner">{me.data.partner_id}</div>
            </div>
          )}
          <button type="button" className="sign-out" onClick={logout}>
            <LogOut size={16} /> Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <div className="content-inner">
          {me.isLoading && <div className="loading">Loading your account…</div>}
          {me.error && !(me.error instanceof ApiError && me.error.status === 401) && (
            <div className="notice notice-error">
              Could not load your account. {me.error instanceof Error ? me.error.message : ""}
            </div>
          )}
          {me.data && <Outlet />}
        </div>
        <footer className="footer">OpenG2P Consent Manager · partner portal · consent you can prove</footer>
      </main>
    </div>
  );
}
