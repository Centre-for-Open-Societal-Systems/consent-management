import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { PolicyRef } from "../components/PolicyRef";
import { dateTime, purposeText } from "./VerificationsPage";

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

// One consent (staff api): its grants and policies, receipt, the evidence of
// the request it came from, and its revocation. Staff revoke a consent when
// the subject asks the organisation to (subjects have no self-service yet).
export default function ConsentDetailPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();

  const { data: c, isLoading, error } = useQuery({
    queryKey: ["consent", id],
    queryFn: () => api.getConsent(id),
  });

  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [receiptError, setReceiptError] = useState<string | null>(null);

  const revoke = useMutation({
    mutationFn: () => api.revokeConsent(id, reason.trim()),
    onSuccess: () => {
      setConfirming(false);
      setReason("");
      qc.invalidateQueries({ queryKey: ["consent", id] });
    },
  });

  const openReceipt = async () => {
    setReceiptError(null);
    const win = window.open("", "_blank");
    try {
      const doc = await api.getConsentReceipt(id);
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" })
      );
      if (win) win.location.href = url;
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (err) {
      win?.close();
      setReceiptError(errText(err, "Could not fetch the receipt."));
    }
  };

  if (isLoading) return <div className="loading">Loading…</div>;
  if (error || !c)
    return (
      <div className="notice notice-error">
        Could not load the consent. {errText(error, "")} <Link to="/verifications">Back</Link>
      </div>
    );

  const revocation = c.revocations[c.revocations.length - 1];

  return (
    <div>
      <div className="spread">
        <div>
          <h1 style={{ margin: 0 }}>Consent</h1>
          <div className="muted">
            <code className="mono">{c.consent_id}</code>
          </div>
        </div>
        <span className={`badge badge-${c.status}`}>{c.status}</span>
      </div>

      {c.status === "revoked" && (
        <div className="notice notice-error">
          Revoked on {dateTime(c.revoked_at)}
          {revocation?.revoked_by && <> by {revocation.revoked_by}</>}
          {revocation?.originated_by === "subject" && <> at the subject's request</>}
          {revocation?.reason && <>: {revocation.reason}</>}
        </div>
      )}

      <div className="card">
        <h3 className="card-title">Consent</h3>
        <table className="data" style={{ boxShadow: "none" }}>
          <tbody>
            <tr>
              <th style={{ width: 200 }}>Partner</th>
              <td>
                <code className="mono">{c.partner_id}</code>
              </td>
            </tr>
            <tr>
              <th>Subject</th>
              <td>
                <code className="mono">{c.subject_id.type}</code> {c.subject_id.value}
              </td>
            </tr>
            {(c.subject_identifiers ?? []).map((i) => (
              <tr key={i.type}>
                <th>{i.type}</th>
                <td>{i.value}</td>
              </tr>
            ))}
            <tr>
              <th>Purpose</th>
              <td>{purposeText(c.purpose)}</td>
            </tr>
            {c.template && (
              <tr>
                <th>Consent template</th>
                <td>
                  <code className="mono">{c.template}</code>
                </td>
              </tr>
            )}
            <tr>
              <th>Validity</th>
              <td>
                {dateTime(c.valid_from)} – {dateTime(c.valid_until)}
              </td>
            </tr>
            <tr>
              <th>Made</th>
              <td>
                {dateTime(c.created_at)} · <span className="muted">{c.source}</span>
                {c.consent_request_id && (
                  <>
                    {" "}
                    · from <Link to={`/verifications/${c.consent_request_id}`}>request</Link>
                  </>
                )}
              </td>
            </tr>
            <tr>
              <th>Receipt</th>
              <td>
                {c.receipt_id ? (
                  <button className="btn-secondary" onClick={openReceipt}>
                    View signed receipt
                  </button>
                ) : (
                  "—"
                )}
                {receiptError && <div className="notice notice-error">{receiptError}</div>}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3 className="card-title">Data shared, by registry</h3>
        <table className="data" style={{ boxShadow: "none" }}>
          <thead>
            <tr>
              <th>Registry</th>
              <th>Data</th>
              <th>Policy</th>
            </tr>
          </thead>
          <tbody>
            {c.grants.map((g, i) => (
              <tr key={g.data_controller ?? i}>
                <td>
                  <code className="mono">{g.data_controller ?? "—"}</code>
                </td>
                <td>{g.effective_data_scopes.join(", ") || "—"}</td>
                <td className="muted">
                  <PolicyRef
                    partnerId={c.partner_id}
                    policyRef={g.policy_ref}
                    policyVersion={g.policy_version}
                    policyId={g.policy_id}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {c.assurance?.method === "assisted_confirmation" && (
        <div className="card">
          <h3 className="card-title">How it was given</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            Assisted confirmation: after the national-ID login, the person agreed in the partner portal
            {typeof c.assurance.confirmed_by === "string" ? `, raised by partner user ${c.assurance.confirmed_by}` : ""}.
            No signed form and no check by Consent Manager staff.
          </p>
          {typeof c.assurance.statement === "string" && (
            <blockquote style={{ margin: 0 }}>{c.assurance.statement}</blockquote>
          )}
        </div>
      )}

      {c.consent_request_id && c.evidence.length > 0 && (
        <div className="card">
          <h3 className="card-title">Evidence</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            {c.evidence.map((e) => e.filename).join(", ")} — view or download on the{" "}
            <Link to={`/verifications/${c.consent_request_id}`}>request</Link>.
          </p>
        </div>
      )}

      {c.status === "active" && (
        <div className="card">
          <h3 className="card-title">Revoke</h3>
          {!confirming ? (
            <>
              <p className="muted" style={{ marginTop: 0 }}>
                Revoke this consent when the subject asks for it to be withdrawn.
              </p>
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button className="btn-danger" onClick={() => setConfirming(true)}>
                  Revoke…
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label htmlFor="revoke-reason">Reason (required)</label>
                <input
                  id="revoke-reason"
                  type="text"
                  value={reason}
                  autoFocus
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="e.g. the subject asked at the office on 12 May"
                />
              </div>
              <div className="notice notice-error">
                Revoke this consent? It stops at once: requests under it are refused and no new
                consent tokens are issued. This cannot be undone.
              </div>
              {revoke.error && (
                <div className="notice notice-error">{errText(revoke.error, "Failed to revoke.")}</div>
              )}
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button
                  className="btn-secondary"
                  onClick={() => {
                    setConfirming(false);
                    revoke.reset();
                  }}
                  disabled={revoke.isPending}
                >
                  Back
                </button>
                <button
                  className="btn-danger"
                  onClick={() => revoke.mutate()}
                  disabled={revoke.isPending || !reason.trim()}
                >
                  {revoke.isPending ? "Revoking…" : "Confirm revocation"}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
