import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { PartnerPolicy, PolicyUpsert } from "../api/types";

export default function PartnerDetailPage() {
  const { id = "" } = useParams();

  const partner = useQuery({ queryKey: ["partner", id], queryFn: () => api.getPartner(id) });

  if (partner.isLoading) return <div className="loading">Loading…</div>;
  if (partner.error || !partner.data)
    return <div className="notice notice-error">Binding not found.</div>;

  const p = partner.data;

  return (
    <div>
      <div className="spread">
        <div>
          <Link to="/partners" className="muted">
            ← Partner policies
          </Link>
          <h1 style={{ marginTop: 8 }}>{p.name || p.partner_mgmt_id || p.audience}</h1>
        </div>
        <span className={`badge badge-${p.status}`}>{p.status}</span>
      </div>

      <div className="card">
        <h3 className="card-title">Binding</h3>
        <table className="data">
          <tbody>
            <tr>
              <th style={{ width: 220 }}>Partner Management ID</th>
              <td>
                {p.partner_mgmt_id ? (
                  <code className="mono">{p.partner_mgmt_id}</code>
                ) : (
                  <span className="muted">
                    — using audience (<code className="mono">{p.audience}</code>) —
                  </span>
                )}
              </td>
            </tr>
            <tr>
              <th>Controller</th>
              <td>
                <code className="mono">{p.controller_id}</code>
              </td>
            </tr>
            <tr>
              <th>Audience</th>
              <td>
                <code className="mono">{p.audience}</code>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="notice notice-info">
        <strong>Signing keys are managed in Partner Management.</strong> The Consent Manager
        fetches this partner's public keys from PM (by its Partner Management ID) to verify
        signed consent objects. Rotate or revoke keys there.
      </div>

      <PolicySection partnerId={id} />
    </div>
  );
}

// ── Policy (versioned; a widening version awaits AWE approval) ────────────
function PolicySection({ partnerId }: { partnerId: string }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);

  const versions = useQuery({
    queryKey: ["policies", partnerId],
    queryFn: () => api.listPolicies(partnerId),
    retry: false,
  });

  const save = useMutation({
    mutationFn: (data: PolicyUpsert) => api.putPolicy(partnerId, data),
    onSuccess: () => {
      setEditing(false);
      qc.invalidateQueries({ queryKey: ["policies", partnerId] });
    },
  });

  const list = versions.data ?? [];
  const active = list.find((p) => p.status === "active");
  const pending = list.find((p) => p.status === "pending");

  // Show the form when editing, or when there is nothing to display yet.
  if (editing || (list.length === 0 && !versions.isLoading)) {
    return (
      <PolicyForm
        initial={active}
        pending={save.isPending}
        error={save.error instanceof ApiError ? save.error.message : undefined}
        onCancel={list.length > 0 ? () => setEditing(false) : undefined}
        onSave={(data) => save.mutate(data)}
      />
    );
  }

  return (
    <div>
      {pending && (
        <div className="notice notice-pending">
          <strong>Policy v{pending.version} is awaiting approval.</strong> It widens access, so it
          will only take effect once approvers sign off. The active policy below stays in force
          until then.
          {pending.awe_request_id && (
            <>
              {" "}
              <span className="muted">AWE request:</span>{" "}
              <code className="mono">{pending.awe_request_id}</code>
            </>
          )}
        </div>
      )}

      <div className="card">
        <div className="spread">
          <h3 className="card-title" style={{ margin: 0 }}>
            Active policy{" "}
            {active ? (
              <span className="muted">v{active.version}</span>
            ) : (
              <span className="muted">— none —</span>
            )}
          </h3>
          <button className="btn-secondary" onClick={() => setEditing(true)}>
            {active ? "Edit policy" : "Define policy"}
          </button>
        </div>

        {versions.isLoading && <p className="loading">Loading policy…</p>}

        {active ? (
          <table className="data">
            <tbody>
              <PolicyRow label="Allowed data scopes" values={active.allowed_data_scopes} />
              <PolicyRow label="Allowed purposes" values={active.allowed_purposes} />
              <PolicyRow label="Allowed subject ID types" values={active.allowed_subject_id_types} />
              <PolicyRow label="Allowed signing algs" values={active.allowed_signing_algs} />
              <tr>
                <th style={{ width: 220 }}>Max validity</th>
                <td>{humaniseDuration(active.max_validity_duration)}</td>
              </tr>
              <tr>
                <th>Fetch type</th>
                <td>{active.fetch_type}</td>
              </tr>
              {active.fetch_type === "periodic" && (
                <tr>
                  <th>Min interval between fetches</th>
                  <td>{humaniseDuration(active.max_fetch_frequency)}</td>
                </tr>
              )}
              <tr>
                <th>Data life</th>
                <td>{humaniseDuration(active.data_life)}</td>
              </tr>
              <tr>
                <th>Lawful basis</th>
                <td>
                  {active.lawful_basis === "legitimate_interest" ? (
                    <strong>Legitimate interest — no consent is sought</strong>
                  ) : (
                    <>Consent — the subject must grant one</>
                  )}
                </td>
              </tr>
              <tr>
                <th>Subject authentication</th>
                <td>
                  {active.required_auth_method === "otp" ? (
                    <>One-time code</>
                  ) : (
                    <span className="muted">
                      None — a consent alone releases the data
                    </span>
                  )}
                </td>
              </tr>
            </tbody>
          </table>
        ) : (
          !versions.isLoading && (
            <p className="muted">
              No active policy — this binding denies everything until a policy is defined.
            </p>
          )
        )}
      </div>

      {list.length > 0 && <VersionHistory versions={list} />}
    </div>
  );
}

function VersionHistory({ versions }: { versions: PartnerPolicy[] }) {
  return (
    <div className="card">
      <h3 className="card-title">Version history</h3>
      <table className="data">
        <thead>
          <tr>
            <th>Version</th>
            <th>Status</th>
            <th>Scopes</th>
            <th>Effective from</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.id}>
              <td>v{v.version}</td>
              <td>
                <span className={`badge badge-${v.status}`}>{v.status}</span>
              </td>
              <td className="muted">{v.allowed_data_scopes.length} scope(s)</td>
              <td className="muted">
                {v.effective_from ? new Date(v.effective_from).toLocaleString() : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PolicyRow({ label, values }: { label: string; values: string[] }) {
  return (
    <tr>
      <th style={{ width: 220 }}>{label}</th>
      <td>
        <div className="chips">
          {values.length === 0 && <span className="muted">—</span>}
          {values.map((v) => (
            <span key={v} className="chip selected">
              {v}
            </span>
          ))}
        </div>
      </td>
    </tr>
  );
}

const DEFAULT_POLICY: PolicyUpsert = {
  allowed_data_scopes: [],
  allowed_purposes: [],
  allowed_subject_id_types: [],
  allowed_signing_algs: ["EdDSA"],
  max_validity_duration: "P30D",
  fetch_type: "oneshot",
  max_fetch_frequency: null,
  data_life: null,
  // Default ON. A policy that omits this reads as "no second factor", so the
  // safe default has to be the one that asks for more, not less.
  required_auth_method: "otp",
  lawful_basis: "consent",
};

function PolicyForm({
  initial,
  onSave,
  onCancel,
  pending,
  error,
}: {
  initial?: PolicyUpsert;
  onSave: (data: PolicyUpsert) => void;
  onCancel?: () => void;
  pending: boolean;
  error?: string;
}) {
  const [form, setForm] = useState<PolicyUpsert>(initial ?? DEFAULT_POLICY);

  const csv = (k: keyof PolicyUpsert) => (e: React.ChangeEvent<HTMLTextAreaElement>) =>
    setForm((f) => ({
      ...f,
      [k]: e.target.value
        .split(/[\n,]/)
        .map((s) => s.trim())
        .filter(Boolean),
    }));

  const listVal = (v: string[]) => v.join("\n");

  return (
    <form
      className="card"
      onSubmit={(e) => {
        e.preventDefault();
        onSave(form);
      }}
    >
      <h3 className="card-title">{initial ? "Edit policy" : "Define policy"}</h3>
      <p className="muted" style={{ marginTop: -8 }}>
        The policy is the outer bound on every consent. Effective fields returned to the registry
        are always the consent's scope ∩ this policy. One entry per line (or comma-separated).
      </p>

      <div className="field">
        <label>Allowed data scopes</label>
        <textarea value={listVal(form.allowed_data_scopes)} onChange={csv("allowed_data_scopes")} placeholder="farmer_profile.basic&#10;farmer_profile.landholding" />
      </div>
      <div className="field">
        <label>Allowed purposes</label>
        <textarea value={listVal(form.allowed_purposes)} onChange={csv("allowed_purposes")} placeholder="loan_origination&#10;subsidy_verification" />
      </div>
      <div className="field">
        <label>Allowed subject ID types</label>
        <textarea value={listVal(form.allowed_subject_id_types)} onChange={csv("allowed_subject_id_types")} placeholder="national_id&#10;farmer_id" />
      </div>
      <div className="field">
        <label>Allowed signing algorithms</label>
        <textarea value={listVal(form.allowed_signing_algs)} onChange={csv("allowed_signing_algs")} placeholder="EdDSA&#10;ES256" />
      </div>

      <div className="row" style={{ alignItems: "flex-start", gap: 24 }}>
        <div className="field" style={{ flex: 1 }}>
          <label>Max validity</label>
          <input
            type="text"
            value={form.max_validity_duration ?? ""}
            onChange={(e) =>
              setForm((f) => ({ ...f, max_validity_duration: e.target.value.trim() || null }))
            }
            placeholder="P30D"
          />
          <div className="hint">
            ISO-8601 duration — {humaniseDuration(form.max_validity_duration)}
          </div>
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label>Fetch type</label>
          <select
            value={form.fetch_type}
            onChange={(e) => setForm((f) => ({ ...f, fetch_type: e.target.value as PolicyUpsert["fetch_type"] }))}
          >
            <option value="oneshot">One-shot</option>
            <option value="periodic">Periodic</option>
          </select>
        </div>
      </div>

      <div className="field">
        <label>Lawful basis</label>
        <select
          value={form.lawful_basis ?? "consent"}
          onChange={(e) => {
            const basis = e.target.value as PolicyUpsert["lawful_basis"];
            // An internal partner has no consent screen, so there is nowhere to
            // present a code. The backend refuses the combination outright;
            // clearing it here means the form cannot build a body it will reject.
            setForm((f) => ({
              ...f,
              lawful_basis: basis,
              required_auth_method: basis === "consent" ? f.required_auth_method : null,
            }));
          }}
        >
          <option value="consent">Consent — the subject must grant one</option>
          <option value="legitimate_interest">
            Legitimate interest — internal partner, no consent sought
          </option>
        </select>
        {form.lawful_basis === "legitimate_interest" ? (
          <div className="notice notice-error" style={{ marginTop: 8 }}>
            <strong>No subject is asked, and none can refuse.</strong> This partner
            receives data without a consent and without the subject knowing. Everything
            else still binds — the signature, the freshness window, and above all the
            allowed data scopes above, which become the entire ceiling in the consent's
            place. Intended for a partner inside this controller's own organisation
            operating on its own lawful basis. Removing consent needs approval before it
            takes effect.
          </div>
        ) : (
          <div className="hint">
            The subject grants a consent, and that grant narrows whatever this partner
            asks for.
          </div>
        )}
      </div>

      {form.lawful_basis === "consent" && (
      <div className="field">
        <label>Subject authentication</label>
        <select
          value={form.required_auth_method ?? ""}
          onChange={(e) =>
            setForm((f) => ({
              ...f,
              required_auth_method: (e.target.value || null) as PolicyUpsert["required_auth_method"],
            }))
          }
        >
          <option value="otp">One-time code (OTP)</option>
          <option value="">None — the consent alone is enough</option>
        </select>
        <div className="hint">
          {form.required_auth_method === "otp"
            ? "The subject enters a code before a consent to this partner can be approved, and before an already-consented fetch releases anything."
            : "A subject who has consented releases data with no second factor, and the consent screen accepts an identity token instead of a code. Removing the code is a widening, so this version needs approval before it takes effect."}
        </div>
      </div>
      )}

      {form.fetch_type === "periodic" && (
        <div className="row" style={{ alignItems: "flex-start", gap: 24 }}>
          <div className="field" style={{ flex: 1 }}>
            <label>Min interval between fetches</label>
            <input
              type="text"
              value={form.max_fetch_frequency ?? ""}
              onChange={(e) =>
                setForm((f) => ({ ...f, max_fetch_frequency: e.target.value.trim() || null }))
              }
              placeholder="P1D"
            />
            <div className="hint">ISO-8601 duration — {humaniseDuration(form.max_fetch_frequency)}</div>
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Data life</label>
            <input
              type="text"
              value={form.data_life ?? ""}
              onChange={(e) => setForm((f) => ({ ...f, data_life: e.target.value.trim() || null }))}
              placeholder="P30D"
            />
            <div className="hint">ISO-8601 duration — {humaniseDuration(form.data_life)}</div>
          </div>
        </div>
      )}

      {error && <div className="notice notice-error">{error}</div>}

      <div className="notice notice-info">
        Saving a policy that widens access is submitted for approval before it takes effect.
      </div>

      <div className="row">
        <button type="submit" className="btn-primary" disabled={pending}>
          {pending ? "Saving…" : "Save policy"}
        </button>
        {onCancel && (
          <button type="button" className="btn-secondary" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}

// Render an ISO-8601 duration (P1Y, P30D, PT12H, P1DT6H) in plain words.
function humaniseDuration(iso?: string | null): string {
  if (!iso) return "—";
  const m = /^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/.exec(
    iso.trim()
  );
  if (!m) return iso;
  const units: [string, string][] = [
    [m[1], "year"],
    [m[2], "month"],
    [m[3], "week"],
    [m[4], "day"],
    [m[5], "hour"],
    [m[6], "minute"],
    [m[7], "second"],
  ];
  const parts = units
    .filter(([v]) => v)
    .map(([v, label]) => `${v} ${label}${Number(v) > 1 ? "s" : ""}`);
  return parts.length ? parts.join(", ") : iso;
}
