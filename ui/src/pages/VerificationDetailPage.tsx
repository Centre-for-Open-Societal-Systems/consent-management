import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { isApprover } from "../auth";
import type { VerificationEvidence } from "../api/types";
import { dateTime, purposeText } from "./VerificationsPage";

const KIND_LABEL: Record<string, string> = {
  signed_form: "Signed consent form",
  other: "Other document",
};

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

function errText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

// One assisted-consent request: the subject, the data asked for, the evidence
// (view each file in a new tab; fetched with the staff token) and the decision.
export default function VerificationDetailPage() {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const approver = isApprover();

  const { data: r, isLoading, error } = useQuery({
    queryKey: ["verification", id],
    queryFn: () => api.getVerification(id),
  });

  const [confirm, setConfirm] = useState<"approve" | "reject" | null>(null);
  const [note, setNote] = useState("");
  const [fileError, setFileError] = useState<string | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["verification", id] });
    qc.invalidateQueries({ queryKey: ["verifications"] });
  };
  const decide = useMutation({
    mutationFn: (action: "approve" | "reject") =>
      action === "approve"
        ? api.approveVerification(id, note.trim() || undefined)
        : api.rejectVerification(id, note.trim()),
    onSuccess: () => {
      setConfirm(null);
      setNote("");
      refresh();
    },
  });

  const openFile = async (e: VerificationEvidence, download: boolean) => {
    setFileError(null);
    // Open the tab inside the click so popup blockers allow it, then load the blob.
    const win = download ? null : window.open("", "_blank");
    try {
      const blob = await api.getVerificationEvidence(id, e.id);
      const url = URL.createObjectURL(blob);
      if (download) {
        const a = document.createElement("a");
        a.href = url;
        a.download = e.filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } else if (win) {
        win.location.href = url;
      } else {
        window.open(url, "_blank");
      }
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (err) {
      win?.close();
      setFileError(errText(err, "Could not fetch the file."));
    }
  };

  if (isLoading) return <div className="loading">Loading…</div>;
  if (error || !r)
    return (
      <div className="notice notice-error">
        Could not load the request. {errText(error, "")} <Link to="/verifications">Back</Link>
      </div>
    );

  const evidence = r.evidence ?? [];
  const actionable = approver && r.status === "pending_verification";
  const rejectNeedsNote = confirm === "reject" && !note.trim();

  return (
    <div>
      <div className="spread">
        <div>
          <h1 style={{ margin: 0 }}>Consent verification</h1>
          <div className="muted">
            <code className="mono">{r.id}</code>
          </div>
        </div>
        <div className="row">
          <span className={`badge badge-${r.status}`}>{r.status.replace(/_/g, " ")}</span>
          <Link to="/verifications" className="btn-secondary">
            Back to list
          </Link>
        </div>
      </div>

      {r.status === "approved" && (
        <div className="notice notice-info">
          Approved by {r.verified_by ?? "—"} on {dateTime(r.verified_at)}
          {r.consent_id && (
            <>
              {" "}
              · consent <code className="mono">{r.consent_id}</code>
            </>
          )}
          {r.verification_note && <> · {r.verification_note}</>}
        </div>
      )}
      {r.status === "rejected" && (
        <div className="notice notice-error">
          Rejected by {r.verified_by ?? "—"} on {dateTime(r.verified_at)}
          {r.verification_note && <>: {r.verification_note}</>}
        </div>
      )}

      <div className="card">
        <h3 className="card-title">Request</h3>
        <table className="data" style={{ boxShadow: "none" }}>
          <tbody>
            <tr>
              <th style={{ width: 200 }}>Partner</th>
              <td>
                <code className="mono">{r.partner_audience ?? "—"}</code>
                {r.created_by && <span className="muted"> · raised by {r.created_by}</span>}
              </td>
            </tr>
            <tr>
              <th>Subject</th>
              <td>
                <code className="mono">{r.subject_id?.type}</code> {r.subject_id?.value}
              </td>
            </tr>
            <tr>
              <th>Purpose</th>
              <td>{purposeText(r.purpose)}</td>
            </tr>
            {r.use_case && (
              <tr>
                <th>Use case</th>
                <td>
                  <code className="mono">{r.use_case}</code>
                </td>
              </tr>
            )}
            <tr>
              <th>Method</th>
              <td>{r.method === "assisted" ? "Assisted (in person)" : (r.method ?? "—")}</td>
            </tr>
            <tr>
              <th>Validity</th>
              <td>
                {r.valid_from ? new Date(r.valid_from).toLocaleDateString() : "From approval"} –{" "}
                {r.valid_until ? new Date(r.valid_until).toLocaleDateString() : "policy default"}
              </td>
            </tr>
            <tr>
              <th>Person authenticated</th>
              <td>
                {r.subject_authentication?.status === "success" ? (
                  <>
                    <span className="badge badge-approved">yes</span>{" "}
                    {r.subject_authentication.provider ?? "national ID"}
                    {r.subject_authentication.method ? ` · ${r.subject_authentication.method}` : ""} ·{" "}
                    {dateTime(r.subject_authentication.authenticated_at)}
                    <div className="muted" style={{ fontSize: 12 }}>
                      The authenticated national ID matched the subject above.
                    </div>
                  </>
                ) : r.subject_authentication?.previous_success ? (
                  <>
                    <span className="badge badge-approved">yes</span>{" "}
                    {dateTime(r.subject_authentication.previous_success.authenticated_at)}
                  </>
                ) : r.subject_authentication ? (
                  <span className="badge badge-rejected">{r.subject_authentication.status}</span>
                ) : (
                  <span className="muted">No (signed form only)</span>
                )}
              </td>
            </tr>
            <tr>
              <th>Created / submitted</th>
              <td>
                {dateTime(r.created_at)} / {dateTime(r.submitted_at)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3 className="card-title">Data requested</h3>
        {r.grants.map((g) => (
          <div key={g.data_controller} className="field">
            <label style={{ fontSize: 13 }}>
              Registry <code className="mono">{g.data_controller}</code>
            </label>
            <div className="chips">
              {g.data_scopes.map((s) => (
                <span key={s} className="chip selected" style={{ cursor: "default" }}>
                  {s}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <h3 className="card-title">Evidence</h3>
        {evidence.length === 0 ? (
          <div className="muted">No documents.</div>
        ) : (
          <table className="data" style={{ boxShadow: "none" }}>
            <thead>
              <tr>
                <th>Document</th>
                <th>Size</th>
                <th>Uploaded</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {evidence.map((e) => (
                <tr key={e.id}>
                  <td>
                    <div>{e.filename}</div>
                    <div className="muted" style={{ fontSize: 12 }}>
                      {KIND_LABEL[e.kind] ?? e.kind} · {e.content_type} ·{" "}
                      <span title={`SHA-256 ${e.sha256}`}>sha256 {e.sha256?.slice(0, 12)}…</span>
                    </div>
                  </td>
                  <td className="muted">{bytes(e.size_bytes)}</td>
                  <td className="muted">
                    {dateTime(e.uploaded_at)}
                    {e.uploaded_by && <div style={{ fontSize: 12 }}>by {e.uploaded_by}</div>}
                  </td>
                  <td>
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      <button className="btn-secondary" onClick={() => openFile(e, false)}>
                        View
                      </button>
                      <button className="btn-secondary" onClick={() => openFile(e, true)}>
                        Download
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {fileError && (
          <div className="notice notice-error" style={{ marginTop: 12 }}>
            {fileError}
          </div>
        )}
      </div>

      {r.status === "pending_verification" && !approver && (
        <div className="notice notice-info">
          Only users with the approver role can approve or reject this request.
        </div>
      )}

      {actionable && (
        <div className="card">
          <h3 className="card-title">Decision</h3>
          {confirm === null ? (
            <>
              <p className="muted" style={{ marginTop: 0 }}>
                Approve only if the signed form matches the subject and the data and purpose
                requested. Approving creates an active consent.
              </p>
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button className="btn-danger" onClick={() => setConfirm("reject")}>
                  Reject…
                </button>
                <button className="btn-primary" onClick={() => setConfirm("approve")}>
                  Approve…
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label htmlFor="verification-note">
                  {confirm === "approve" ? "Note (optional)" : "Reason for rejecting (required)"}
                </label>
                <input
                  id="verification-note"
                  type="text"
                  value={note}
                  autoFocus
                  onChange={(e) => setNote(e.target.value)}
                  placeholder={
                    confirm === "approve"
                      ? "e.g. form checked against the subject's ID"
                      : "e.g. the signature is missing; the ID number does not match"
                  }
                />
              </div>
              <div className={confirm === "approve" ? "notice notice-pending" : "notice notice-error"}>
                {confirm === "approve"
                  ? "Approve this request? The consent becomes active and the partner can use it."
                  : "Reject this request? The partner sees your reason; it cannot be approved later."}
              </div>
              {decide.error && (
                <div className="notice notice-error">{errText(decide.error, "Failed to submit the decision.")}</div>
              )}
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button
                  className="btn-secondary"
                  onClick={() => {
                    setConfirm(null);
                    decide.reset();
                  }}
                  disabled={decide.isPending}
                >
                  Back
                </button>
                <button
                  className={confirm === "approve" ? "btn-primary" : "btn-danger"}
                  onClick={() => decide.mutate(confirm)}
                  disabled={decide.isPending || rejectNeedsNote}
                >
                  {decide.isPending
                    ? "Submitting…"
                    : confirm === "approve"
                      ? "Confirm approval"
                      : "Confirm rejection"}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
