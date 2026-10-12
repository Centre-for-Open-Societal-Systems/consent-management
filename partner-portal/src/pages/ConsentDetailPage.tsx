import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { ErrorNotice, StatusBadge } from "../components/Status";
import { assuranceText, formatDate, formatDateTime, purposeLabel, saveBlob } from "../format";

export default function ConsentDetailPage() {
  const { id = "" } = useParams();
  const { data: c, isLoading, error } = useQuery({
    queryKey: ["consent", id],
    queryFn: () => api.getConsent(id),
  });
  const [downloading, setDownloading] = useState(false);
  const [receiptError, setReceiptError] = useState<string | null>(null);
  const [recordBusy, setRecordBusy] = useState(false);

  const downloadRecord = async () => {
    setReceiptError(null);
    setRecordBusy(true);
    try {
      const { blob, filename } = await api.record(id);
      saveBlob(blob, filename ?? `consent-record-${id}.zip`);
    } catch (e) {
      setReceiptError(e instanceof ApiError ? e.message : "Could not download the consent record.");
    } finally {
      setRecordBusy(false);
    }
  };

  const downloadReceipt = async () => {
    setReceiptError(null);
    setDownloading(true);
    try {
      const { blob } = await api.receipt(id);
      // Pretty-print when it parses as JSON; otherwise save it as received.
      const text = await blob.text();
      let out = text;
      try {
        out = JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        /* keep as is */
      }
      saveBlob(new Blob([out], { type: "application/json" }), `consent-${id}-receipt.json`);
    } catch (e) {
      setReceiptError(e instanceof ApiError ? e.message : "Could not download the receipt.");
    } finally {
      setDownloading(false);
    }
  };

  if (isLoading) return <div className="loading">Loading…</div>;
  if (error instanceof ApiError && error.status === 404)
    return (
      <div className="card">
        Consent not found. <Link to="/consents">Back to consents</Link>
      </div>
    );
  if (error || !c) return <ErrorNotice error={error} what="the consent" />;

  return (
    <div>
      <div className="spread">
        <div>
          <h1 style={{ margin: 0 }}>Consent</h1>
          <div className="muted">
            <code className="mono">{c.consent_id}</code>
          </div>
        </div>
        <div className="row">
          <StatusBadge status={c.status} />
          <button
            className="btn-primary"
            onClick={downloadRecord}
            disabled={recordBusy}
            title="For your records: the receipt, the signed form and the Consent Manager's signed statement tying them together"
          >
            {recordBusy ? "Preparing…" : "Download consent record"}
          </button>
          <button className="btn-secondary" onClick={downloadReceipt} disabled={downloading}>
            {downloading ? "Downloading…" : "Download receipt"}
          </button>
          <Link to="/consents" className="btn-secondary">
            Back to consents
          </Link>
        </div>
      </div>
      {receiptError && <div className="notice notice-error">{receiptError}</div>}
      {c.status === "revoked" && (
        <div className="notice notice-error">
          Revoked{c.revoked_at ? ` on ${formatDateTime(c.revoked_at)}` : ""}. It can no longer be used.
        </div>
      )}

      <div className="card">
        <dl className="facts">
          <dt>Subject</dt>
          <dd>
            <code className="mono">{c.subject_id?.type}</code> {c.subject_id?.value}
          </dd>
          <dt>Purpose</dt>
          <dd>{purposeLabel(c.purpose)}</dd>
          {c.use_case && (
            <>
              <dt>Use case</dt>
              <dd>
                <code className="mono">{c.use_case}</code>
              </dd>
            </>
          )}
          <dt>Valid</dt>
          <dd>
            {formatDate(c.valid_from)} – {formatDate(c.valid_until)}
          </dd>
          <dt>How the subject consented</dt>
          <dd>{assuranceText(c.assurance)}</dd>
          <dt>Created</dt>
          <dd>{formatDateTime(c.created_at)}</dd>
          {c.consent_request_id && (
            <>
              <dt>Request</dt>
              <dd>
                <Link to={`/requests/${c.consent_request_id}`}>
                  <code className="mono">{c.consent_request_id}</code>
                </Link>
              </dd>
            </>
          )}
        </dl>
      </div>

      <div className="card">
        <h3 className="card-title">Data granted</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          Per registry, the data the subject agreed to share and that your policy allowed when the
          consent was granted (effective). Data requested but not effective is shown struck through.
        </p>
        {c.grants.map((g) => {
          const effective = g.effective_data_scopes ?? [];
          const dropped = g.data_scopes.filter((s) => !effective.includes(s));
          return (
            <div className="grant" key={g.data_controller}>
              <div className="grant-head">
                Registry <code className="mono">{g.data_controller}</code>
              </div>
              <div className="chips">
                {effective.map((s) => (
                  <span key={s} className="chip static selected">
                    {s}
                  </span>
                ))}
                {dropped.map((s) => (
                  <span
                    key={s}
                    className="chip static"
                    style={{ textDecoration: "line-through" }}
                    title="Requested, not effective"
                  >
                    {s}
                  </span>
                ))}
                {effective.length === 0 && dropped.length === 0 && <span className="muted">None</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
