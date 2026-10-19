import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { REQUEST_STATUSES } from "../api/types";
import Pagination from "../components/Pagination";
import { ErrorNotice, StatusBadge } from "../components/Status";
import { formatDateTime, purposeLabel, statusLabel } from "../format";

const LIMIT = 25;

export default function RequestsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const offset = Number(params.get("offset") ?? 0) || 0;

  const { data, isLoading, error } = useQuery({
    queryKey: ["requests", status, offset],
    queryFn: () => api.listRequests({ status: status || undefined, limit: LIMIT, offset }),
    placeholderData: keepPreviousData,
  });

  const setFilter = (next: { status?: string; offset?: number }) => {
    const p = new URLSearchParams();
    const s = next.status ?? status;
    if (s) p.set("status", s);
    if (next.offset) p.set("offset", String(next.offset));
    setParams(p);
  };

  return (
    <div>
      <div className="spread">
        <h1 style={{ margin: 0 }}>Consent requests</h1>
        <div className="filters">
          <select
            aria-label="Status"
            value={status}
            onChange={(e) => setFilter({ status: e.target.value, offset: 0 })}
          >
            <option value="">All statuses</option>
            {REQUEST_STATUSES.map((s) => (
              <option key={s} value={s}>
                {statusLabel(s)}
              </option>
            ))}
          </select>
          <Link to="/requests/new" className="btn-primary">
            New request
          </Link>
        </div>
      </div>
      <p className="muted page-intro">
        Requests your organisation has raised for a subject's consent. Upload the subject's signed
        consent form and submit the request; Consent Manager staff verify it before the consent
        becomes active.
      </p>

      {isLoading && <div className="loading">Loading…</div>}
      {error && <ErrorNotice error={error} what="requests" />}
      {data && data.items.length === 0 && (
        <div className="card">
          No requests{status ? ` with status "${statusLabel(status)}"` : ""} yet.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data">
            <thead>
              <tr>
                <th>Created</th>
                <th>Subject</th>
                <th>Purpose</th>
                <th>Registries</th>
                <th>Created by</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => navigate(`/requests/${r.id}`)}>
                  <td className="muted">
                    <Link to={`/requests/${r.id}`}>{formatDateTime(r.created_at)}</Link>
                  </td>
                  <td>
                    <code className="mono">{r.subject_id?.type}</code> {r.subject_id?.value}
                  </td>
                  <td>
                    {purposeLabel(r.purpose)}
                    {r.use_case && <div className="muted" style={{ fontSize: 12 }}>{r.use_case}</div>}
                  </td>
                  <td className="muted">{r.grants.map((g) => g.data_controller).join(", ")}</td>
                  <td className="muted">{r.created_by ?? "—"}</td>
                  <td>
                    <StatusBadge status={r.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination
            total={data.total}
            limit={LIMIT}
            offset={offset}
            onChange={(o) => setFilter({ offset: o })}
          />
        </>
      )}
    </div>
  );
}
