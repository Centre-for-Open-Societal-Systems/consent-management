import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Artefact, ConsentRequest, Purpose } from "../api/types";

export function purposeLabel(purpose: Purpose): string {
  return (
    (purpose?.name as string) ||
    (purpose?.description as string) ||
    (purpose?.code as string) ||
    "data sharing"
  );
}

export default function MyConsentsPage() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["my-consents"],
    queryFn: () => api.myConsents(),
  });

  // Requests waiting for the subject's answer (answered on /consent/<id>).
  const pending = useQuery({
    queryKey: ["my-consent-requests"],
    queryFn: () => api.myConsentRequests("pending"),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeMyConsent(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-consents"] }),
  });

  return (
    <div>
      <h1>My consents</h1>
      <p className="muted" style={{ marginTop: -8, marginBottom: 24 }}>
        Every consent you have granted for sharing your data. You can withdraw an active consent
        at any time — sharing stops immediately and all parties are notified.
      </p>

      {pending.data && pending.data.length > 0 && (
        <>
          <h3>Waiting for your answer</h3>
          {pending.data.map((r: ConsentRequest) => (
            <div className="card" key={r.id}>
              <div className="spread">
                <div>
                  <h3 style={{ margin: 0 }}>{r.partner_id}</h3>
                  <div className="muted">
                    {purposeLabel(r.purpose)} · asked {formatDate(r.created_at)}
                  </div>
                </div>
                <Link to={`/consent/${r.id}`} className="btn-primary">
                  Review
                </Link>
              </div>
            </div>
          ))}
          <h3 style={{ marginTop: 32 }}>Your consents</h3>
        </>
      )}

      {isLoading && <div className="loading">Loading…</div>}
      {error && <div className="notice notice-error">Could not load your consents.</div>}
      {data && data.length === 0 && <div className="card">You have not granted any consents.</div>}

      {data &&
        data.map((c: Artefact) => (
          <div className="card" key={c.id}>
            <div className="spread" style={{ marginBottom: 12 }}>
              <div>
                <h3 style={{ margin: 0 }}>{c.partner_id}</h3>
                <div className="muted">{purposeLabel(c.purpose)}</div>
              </div>
              <span className={`badge badge-${c.status}`}>{c.status}</span>
            </div>

            <div className="field" style={{ margin: 0 }}>
              <label style={{ fontSize: 13 }}>Data shared</label>
              <div className="chips">
                {c.effective_data_scopes.map((s) => (
                  <span key={s} className="chip selected">
                    {s}
                  </span>
                ))}
              </div>
            </div>

            <div className="row" style={{ marginTop: 16, justifyContent: "space-between" }}>
              <span className="muted" style={{ fontSize: 13 }}>
                {c.valid_until ? `Valid until ${formatDate(c.valid_until)}` : "No expiry set"}
              </span>
              {c.status === "active" && (
                <button
                  className="btn-danger"
                  onClick={() => revoke.mutate(c.id)}
                  disabled={revoke.isPending}
                >
                  Withdraw consent
                </button>
              )}
            </div>
          </div>
        ))}
    </div>
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
