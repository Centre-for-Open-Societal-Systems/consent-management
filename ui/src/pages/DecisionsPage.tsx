import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

// Operational/audit status view: the append-only log of recent validate
// decisions (permit/deny + reason). Backed by CM's in-house DecisionLog.
export default function DecisionsPage() {
  const [filter, setFilter] = useState<"" | "permit" | "deny">("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["decisions", filter],
    queryFn: () => api.listDecisions({ decision: filter || undefined, limit: 100 }),
  });

  return (
    <div>
      <div className="spread">
        <h1>Decisions</h1>
        <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
          <option value="">All</option>
          <option value="permit">Permits</option>
          <option value="deny">Denies</option>
        </select>
      </div>

      <p className="muted" style={{ marginTop: -8, marginBottom: 24 }}>
        The append-only record of every validation decision — for operational visibility and audit.
      </p>

      {isLoading && <div className="loading">Loading…</div>}
      {error && <div className="notice notice-error">Could not load decisions.</div>}
      {data && data.length === 0 && <div className="card">No decisions recorded yet.</div>}

      {data && data.length > 0 && (
        <table className="data">
          <thead>
            <tr>
              <th>When</th>
              <th>Decision</th>
              <th>Reason</th>
              <th>Partner</th>
              <th>Controller</th>
              <th>Policy</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.id}>
                <td className="muted">{new Date(d.created_at).toLocaleString()}</td>
                <td>
                  <span className={`badge badge-${d.decision}`}>{d.decision}</span>
                </td>
                <td>
                  <code className="mono">{d.reason_code}</code>
                  {d.detail && <div className="muted" style={{ fontSize: 12 }}>{d.detail}</div>}
                </td>
                <td className="muted">
                  {d.partner_id ? <code className="mono">{d.partner_id.slice(0, 8)}…</code> : "—"}
                </td>
                <td className="muted">
                  {d.data_controller ? <code className="mono">{d.data_controller}</code> : "—"}
                </td>
                <td className="muted">{d.policy_version != null ? `v${d.policy_version}` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
