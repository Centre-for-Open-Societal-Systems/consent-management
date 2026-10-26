import { NavLink, Outlet } from "react-router-dom";
import { LogOut, ShieldCheck } from "lucide-react";
import {
  farmerIdentity,
  farmerLoggedIn,
  farmerLogin,
  farmerLoginError,
  farmerLogout,
  isConsentLogin,
} from "../auth";
import "./Layout.css";
import "../pages/ConsentRequestPage.css";

const navClass = ({ isActive }: { isActive: boolean }) => `nav-item${isActive ? " active" : ""}`;

// Shell of the farmer pages (/my/consents, /consent/<id>): the console's OpenG2P
// shell (Layout), for a farmer logged in with national ID (eSignet) instead of
// Keycloak. Nothing shows before the login. A consent login (one request,
// opened from its link) shows only that request: no menu, no /my pages.
export default function FarmerLayout() {
  if (!farmerLoggedIn()) return <FarmerLogin />;
  if (isConsentLogin()) return <Outlet />;
  const me = farmerIdentity();
  return (
    <div className="shell">
      <aside className="sidebar">
        <NavLink to="/my/consents" className="brand" title="Consent Manager">
          <img src="/openg2p-logo-horizontal.svg" alt="OpenG2P" className="brand-logo" />
          <span className="brand-product">Consent Manager</span>
        </NavLink>
        <nav className="nav">
          <NavLink to="/my/consents" className={navClass}>
            <ShieldCheck size={18} /> My Consents
          </NavLink>
        </nav>
        <div className="sidebar-user">
          <div className="user">
            <div className="user-name">{me.name ?? "Farmer"}</div>
            <div className="user-role">National ID {me.id}</div>
          </div>
          <button type="button" className="sign-out" onClick={farmerLogout}>
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

function FarmerLogin() {
  const error = farmerLoginError();
  const one = isConsentLogin();
  return (
    <div className="consent-screen">
      <div className="consent-card">
        <header className="consent-head">
          <img src="/openg2p-logo.svg" alt="OpenG2P" className="consent-logo" />
          <span>Consent Manager</span>
        </header>
        <h1>Log in with your national ID</h1>
        <p className="lead">
          {one
            ? "To see this request for your data and to grant or decline it, log in with your Fayda national ID."
            : "To see who asks for your data and to grant or decline, log in with your Fayda national ID."}{" "}
          You will get a one-time code on the phone registered with your ID.
        </p>
        {error && <div className="notice notice-error">{error}</div>}
        <div className="consent-actions">
          <button className="btn-primary consent-grant" onClick={farmerLogin}>
            Log in with national ID
          </button>
        </div>
      </div>
      <p className="consent-foot muted">Secured by OpenG2P Consent Manager</p>
    </div>
  );
}
