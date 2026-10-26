import { NavLink, Outlet } from "react-router-dom";
import { BadgeCheck, FileSearch, Handshake, LogOut, ShieldCheck, Stamp } from "lucide-react";
import { currentUser, getConfig, isAdmin, isApprover, isDevMode, logout } from "../auth";
import "./Layout.css";

// The OpenG2P shell, as in the Master Data UI and the partner portal: a black
// left pane with the full logo and the product name, the menu with icons, and
// the user at the bottom.
const navClass = ({ isActive }: { isActive: boolean }) => `nav-item${isActive ? " active" : ""}`;

export default function Layout() {
  const admin = isAdmin();
  const approver = isApprover();
  const home = admin ? "/partners" : approver ? "/verifications" : "/my/consents";
  return (
    <div className="shell">
      <aside className="sidebar">
        <NavLink to={home} className="brand" title="Consent Manager">
          <img src="/openg2p-logo-horizontal.svg" alt="OpenG2P" className="brand-logo" />
          <span className="brand-product">Consent Manager</span>
        </NavLink>
        <nav className="nav">
          {admin && (
            <NavLink to="/partners" className={navClass}>
              <Handshake size={18} /> Partner policies
            </NavLink>
          )}
          {approver && (
            <NavLink to="/approvals" className={navClass}>
              <Stamp size={18} /> Approvals
            </NavLink>
          )}
          {/* Approvers act on assisted-consent verifications; admins may read. */}
          {(approver || admin) && (
            <NavLink to="/verifications" className={navClass}>
              <BadgeCheck size={18} /> Consent verifications
            </NavLink>
          )}
          {admin && (
            <NavLink to="/decisions" className={navClass}>
              <FileSearch size={18} /> Decisions
            </NavLink>
          )}
          {/* With farmer login on, /my/consents is the farmer's own page (national ID). */}
          {!getConfig().beneficiaryAuth?.enabled && (
            <NavLink to="/my/consents" className={navClass}>
              <ShieldCheck size={18} /> My Consents
            </NavLink>
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
