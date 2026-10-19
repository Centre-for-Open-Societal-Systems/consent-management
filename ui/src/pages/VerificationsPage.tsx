import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { Purpose } from "../api/types";

const LIMIT = 25;

export const VERIFICATION_STATUSES = [
  { value: "pending_verification", label: "Pending verification" },
  { value: "approved", label: "Approved" },
  { value: "rejected", label: "Rejected" },
];

export function purposeText(p: Purpose | string | null | undefined): string {
  if (!p) return "—";
  if (typeof p === "string") return p;
  return (p.name as string) || (p.description as string) || (p.code as string) || "—";
}

export function dateTime(iso?: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// Staff inbox for assisted consents: a partner user raised the request in the
// partner portal and uploaded the form the subject signed. Approvers check the
// form against the subject and the request, then approve (the consent becomes
// active) or reject.
export default function VerificationsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "pending_verification";
  const offset = Number(params.get("offset") ?? 0) || 0;

  const { data, isLoading, error } = useQuery({
    queryKey: ["verifications", status, offset],
    queryFn: () => api.listVerifications({ status, limit: LIMIT, offset }),
    placeholderData: keepPreviousData,
  });

  const go = (next: { status?: string; offset?: number }) => {
    const q = new URLSearchParams();
    q.set("status", next.status ?? status);
    if (next.offset) q.set("offset", String(next.offset));
    setParams(q);
  };

  const total = data?.total ?? 0;

  return (
    <div>
      <div className="spread">
        <h1 style={{ margin: 0 }}>Consent verifications</h1>
        <select
          aria-label="Status"
          value={status}
          onChange={(e) => go({ status: e.target.value, offset: 0 })}
        >
          {VERIFICATION_STATUSES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      <p className="muted" style={{ marginTop: 0, marginBottom: 24 }}>
        Consents a partner obtained in person, with the form the subject signed. Check that the form
        matches the subject and the request, then approve (the consent becomes active) or reject.
      </p>

      {isLoading && <div className="loading">Loading…</div>}
      {error && (
        <div className="notice notice-error">
          Could not load verifications. {error instanceof ApiError ? error.message : ""}
        </div>
      )}
      {data && data.items.length === 0 && (
        <div className="card">
          {status === "pending_verification" ? "Nothing to verify." : "No requests with this status."}
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data">
            <thead>
              <tr>
                <th>Partner</th>
                <th>Subject</th>
                <th>Purpose</th>
                <th>Registries</th>
                <th>Created</th>
                <th>Submitted</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((r) => (
                <tr
                  key={r.id}
                  style={{ cursor: "pointer" }}
                  onClick={() => navigate(`/verifications/${r.id}`)}
                >
                  <td>
                    <Link to={`/verifications/${r.id}`}>
                      <code className="mono">{r.partner_audience ?? "—"}</code>
                    </Link>
                    {r.created_by && <div className="muted" style={{ fontSize: 12 }}>by {r.created_by}</div>}
                  </td>
                  <td>
                    <code className="mono">{r.subject_id?.type}</code> {r.subject_id?.value}
                  </td>
                  <td>
                    {purposeText(r.purpose)}
                    {r.use_case && <div className="muted" style={{ fontSize: 12 }}>{r.use_case}</div>}
                  </td>
                  <td className="muted">{r.grants.map((g) => g.data_controller).join(", ")}</td>
                  <td className="muted">{dateTime(r.created_at)}</td>
                  <td className="muted">{dateTime(r.submitted_at)}</td>
                  <td>
                    <span className={`badge badge-${r.status}`}>{r.status.replace(/_/g, " ")}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {total > LIMIT && (
            <div className="spread" style={{ marginTop: 16 }}>
              <span className="muted">
                {offset + 1}–{Math.min(offset + LIMIT, total)} of {total}
              </span>
              <div className="row">
                <button
                  className="btn-secondary"
                  disabled={offset === 0}
                  onClick={() => go({ offset: Math.max(0, offset - LIMIT) })}
                >
                  Previous
                </button>
                <button
                  className="btn-secondary"
                  disabled={offset + LIMIT >= total}
                  onClick={() => go({ offset: offset + LIMIT })}
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
