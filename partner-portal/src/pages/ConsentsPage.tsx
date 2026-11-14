import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { CONSENT_STATUSES, type ConsentRequest } from "../api/types";
import Pagination from "../components/Pagination";
import { ErrorNotice, StatusBadge } from "../components/Status";
import { formatDate, formatDateTime, purposeLabel, statusLabel } from "../format";

const LIMIT = 25;
// Requests not yet decided: being prepared by the partner's staff, or waiting
// for Consent Manager staff. Closed ones are in History.
const OPEN_STATUSES = ["pending", "pending_verification"] as const;

function InProgress() {
  const navigate = useNavigate();
  const { data, error } = useQuery({
    queryKey: ["requests", "open"],
    queryFn: async () => {
      const pages = await Promise.all(
        OPEN_STATUSES.map((status) => api.listRequests({ status, limit: 200 })),
      );
      return pages
        .flatMap((p) => p.items)
        .sort((a, b) => b.created_at.localeCompare(a.created_at)) as ConsentRequest[];
    },
  });

  if (error) return <ErrorNotice error={error} what="requests in progress" />;
  if (!data || data.length === 0) return null;
  return (
    <>
      <h2>In progress</h2>
      <table className="data">
        <thead>
          <tr>
            <th>Started</th>
            <th>Subject</th>
            <th>Purpose</th>
            <th>Started by</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {data.map((r) => (
            <tr key={r.id} className="clickable" onClick={() => navigate(`/requests/${r.id}`)}>
              <td className="muted">
                <Link to={`/requests/${r.id}`}>{formatDateTime(r.created_at)}</Link>
              </td>
              <td>
                <code className="mono">{r.subject_id?.type}</code> {r.subject_id?.value}
              </td>
              <td>
                {purposeLabel(r.purpose)}
                {r.template && <div className="muted" style={{ fontSize: 12 }}>{r.template}</div>}
              </td>
              <td className="muted">{r.created_by ?? "—"}</td>
              <td>
                <StatusBadge status={r.status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h2>Consents</h2>
    </>
  );
}

export default function ConsentsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const offset = Number(params.get("offset") ?? 0) || 0;

  const { data, isLoading, error } = useQuery({
    queryKey: ["consents", status, offset],
    queryFn: () => api.listConsents({ status: status || undefined, limit: LIMIT, offset }),
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
        <h1 style={{ margin: 0 }}>Consents</h1>
        <div className="filters">
          <select
            aria-label="Status"
            value={status}
            onChange={(e) => setFilter({ status: e.target.value, offset: 0 })}
          >
            <option value="">All statuses</option>
            {CONSENT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {statusLabel(s)}
              </option>
            ))}
          </select>
          <Link to="/consents/new" className="btn-primary">
            New consent
          </Link>
        </div>
      </div>
      <p className="muted page-intro">
        Acquire a subject's consent with <strong>New consent</strong>; Consent Manager staff check
        it before it becomes active. Requests not yet decided are listed first; every request ever
        raised is in History.
      </p>

      <InProgress />

      {isLoading && <div className="loading">Loading…</div>}
      {error && <ErrorNotice error={error} what="consents" />}
      {data && data.items.length === 0 && (
        <div className="card">
          No consents{status ? ` with status "${statusLabel(status)}"` : ""} yet.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data">
            <thead>
              <tr>
                <th>Consent ID</th>
                <th>Subject</th>
                <th>Purpose</th>
                <th>Registries</th>
                <th>Valid until</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((c) => (
                <tr
                  key={c.consent_id}
                  className="clickable"
                  onClick={() => navigate(`/consents/${encodeURIComponent(c.consent_id)}`)}
                >
                  <td>
                    <Link to={`/consents/${encodeURIComponent(c.consent_id)}`}>
                      <code className="mono">{c.consent_id}</code>
                    </Link>
                  </td>
                  <td>
                    <code className="mono">{c.subject_id?.type}</code> {c.subject_id?.value}
                  </td>
                  <td>
                    {purposeLabel(c.purpose)}
                    {c.template && <div className="muted" style={{ fontSize: 12 }}>{c.template}</div>}
                  </td>
                  <td className="muted">{c.grants.map((g) => g.data_controller).join(", ")}</td>
                  <td className="muted">{formatDate(c.valid_until)}</td>
                  <td>
                    <StatusBadge status={c.status} />
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
