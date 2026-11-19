import { NavLink, Outlet } from "react-router-dom";
import { BadgeCheck, ExternalLink, FileSearch, Handshake, LogOut } from "lucide-react";
import { currentUser, getConfig, isAdmin, isApprover, isDevMode, logout } from "../auth";
import "./Layout.css";

// The OpenG2P shell, as in the Master Data UI and the partner portal: a black
// left pane with the full logo and the product name, the menu with icons, and
// the user at the bottom.
const navClass = ({ isActive }: { isActive: boolean }) => `nav-item${isActive ? " active" : ""}`;

// Landing page: Consent Approvals (approvers act there; admins read).
export function homePath(): string {
  return "/verifications";
}

export default function Layout() {
  const admin = isAdmin();
  const approver = isApprover();
  const home = homePath();
  // Partners and their access policies live in Partner Management.
  const pmUrl = getConfig().partnerManagementUiUrl?.trim();
  return (
    <div className="shell">
      <aside className="sidebar">
        <NavLink to={home} className="brand" title="Consent Manager">
          <img src="/openg2p-logo-horizontal.svg" alt="OpenG2P" className="brand-logo" />
          <span className="brand-product">Consent Manager</span>
        </NavLink>
        <nav className="nav">
          {/* Approvers act on assisted-consent verifications; admins may read. */}
          {(approver || admin) && (
            <NavLink to="/verifications" className={navClass}>
              <BadgeCheck size={18} /> Consent Approvals
            </NavLink>
          )}
          {admin && (
            <NavLink to="/decisions" className={navClass}>
              <FileSearch size={18} /> Access Log
            </NavLink>
          )}
          {admin && pmUrl && (
            <a
              href={pmUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="nav-item"
              title="Partners and their access policies are managed in Partner Management (opens in a new tab)"
            >
              <Handshake size={18} /> Partners &amp; policies
              <ExternalLink size={14} className="nav-external" aria-label="opens in a new tab" />
            </a>
          )}
        </nav>
        <div className="sidebar-user">
          {isDevMode() && <span className="dev-pill">dev</span>}
          <div className="user">
            <div className="user-name">{currentUser()}</div>
            <div className="user-role">{admin ? "Administrator" : approver ? "Approver" : "Staff"}</div>
          </div>
          <button type="button" className="sign-out" onClick={logout}>
            <LogOut size={16} /> Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <div className="content-inner">
          <Outlet />
        </div>
        <footer className="footer">OpenG2P Consent Manager · governs verifiable, policy-bound data sharing</footer>
      </main>
    </div>
  );
}
