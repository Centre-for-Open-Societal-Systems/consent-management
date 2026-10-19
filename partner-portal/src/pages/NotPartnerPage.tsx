import { logout } from "../auth";

// Shown when the API answers 403 for /me: the user signed in to the partner
// realm, but the account has no partner_id or no partner role.
export default function NotPartnerPage() {
  return (
    <div className="standalone">
      <div className="card standalone-card">
        <img src="/openg2p-logo.svg" alt="OpenG2P" className="standalone-logo" />
        <h1>Your account is not set up as a partner user</h1>
        <p>
          You are signed in, but your account is not linked to a partner organisation, or it has no
          partner role. Ask the administrator who created your account to link it to your partner
          (the <code className="mono">partner_id</code> attribute) and give it the{" "}
          <code className="mono">PARTNER_OPERATOR</code> or <code className="mono">PARTNER_ADMIN</code>{" "}
          role, then sign in again.
        </p>
        <button className="btn-primary" onClick={logout}>
          Sign out
        </button>
      </div>
    </div>
  );
}
