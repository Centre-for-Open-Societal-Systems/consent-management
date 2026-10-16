import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";

// Bindings list — each row binds a Partner-Management partner to a controller
// and a data-share policy. Identity/keys live in Partner Management.
export default function PartnersPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["partners"],
    queryFn: () => api.listPartners(),
  });

  return (
    <div>
      <div className="spread">
        <h1>Partner policies</h1>
        <Link to="/partners/new" className="btn-primary">
          New binding
        </Link>
      </div>

      <p className="muted" style={{ marginTop: -8, marginBottom: 24 }}>
        Each binding ties a Partner-Management partner to a data controller and a
        versioned data-share policy — the ceiling on what that partner may ever receive
        from that controller. A partner can be bound to several controllers, each with
        its own policy. Partner identity and signing keys are managed in Partner Management.
      </p>

      {isLoading && <div className="loading">Loading…</div>}
      {error && <div className="notice notice-error">Could not load bindings.</div>}

      {data && data.length === 0 && (
        <div className="card">
          No bindings yet. <Link to="/partners/new">Create your first binding</Link> to define a policy.
        </div>
      )}

      {data && data.length > 0 && (
        <table className="data">
          <thead>
            <tr>
              <th>Label</th>
              <th>Partner (PM)</th>
              <th>Controller</th>
              <th>Audience</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {data.map((p) => (
              <tr key={p.id}>
                <td>
                  <Link to={`/partners/${p.id}`}>{p.name || "—"}</Link>
                </td>
                <td>
                  <code className="mono">{p.partner_mgmt_id || p.audience}</code>
                </td>
                <td>
                  <code className="mono">{p.controller_id}</code>
                </td>
                <td>
                  <code className="mono">{p.audience}</code>
                </td>
                <td>
                  <span className={`badge badge-${p.status}`}>{p.status}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
