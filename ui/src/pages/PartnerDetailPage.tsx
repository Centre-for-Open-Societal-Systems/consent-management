import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { RESUBMITTABLE } from "../api/types";
import type { Partner, PartnerPolicy, PolicyMeta, PolicyUpsert } from "../api/types";
import {
  CheckboxGroup,
  DurationInput,
  ListTextarea,
  durationError,
  humaniseDuration,
} from "../components/FormControls";

const FETCH_TYPE_LABELS: Record<string, string> = { oneshot: "One-shot", periodic: "Periodic" };
const fetchTypeLabel = (v: string) => FETCH_TYPE_LABELS[v] ?? v;

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

      <ControllerBindings partner={p} />

      <div className="notice notice-info">
        <strong>Signing keys are managed in Partner Management.</strong> The Consent Manager
        fetches this partner's public keys from PM (by its Partner Management ID) to verify
        signed consent objects. Rotate or revoke keys there.
      </div>

      <PolicySection partnerId={id} />
    </div>
  );
}

// ── All controller bindings of this partner (same audience) ──────────────
// A partner can be bound to several controllers (registries), each binding with
// its own policy. A consent with grants is validated by each controller against
// that controller's binding.
function ControllerBindings({ partner }: { partner: Partner }) {
  const bindings = useQuery({
    queryKey: ["partners", { audience: partner.audience }],
    queryFn: () => api.listPartners({ audience: partner.audience }),
  });

  const addParams = new URLSearchParams({ audience: partner.audience });
  if (partner.partner_mgmt_id) addParams.set("partner_mgmt_id", partner.partner_mgmt_id);
  if (partner.name) addParams.set("name", partner.name);

  return (
    <div className="card">
      <div className="spread">
        <h3 className="card-title" style={{ margin: 0 }}>
          Controller bindings for this partner
        </h3>
        <Link to={`/partners/new?${addParams.toString()}`} className="btn-secondary">
          Add controller binding
        </Link>
      </div>
      <p className="muted">
        Each controller has its own binding and policy. The policy below is for{" "}
        <code className="mono">{partner.controller_id}</code> only.
      </p>
      {bindings.isLoading && <p className="loading">Loading bindings…</p>}
      {bindings.data && (
        <table className="data">
          <thead>
            <tr>
              <th>Controller</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {bindings.data.map((b) => (
              <tr key={b.id}>
                <td>
                  {b.id === partner.id ? (
                    <>
                      <code className="mono">{b.controller_id}</code>{" "}
                      <span className="muted">(this binding)</span>
                    </>
                  ) : (
                    <Link to={`/partners/${b.id}`}>
                      <code className="mono">{b.controller_id}</code>
                    </Link>
                  )}
                </td>
                <td>
                  <span className={`badge badge-${b.status}`}>{b.status}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
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
  // Allowed values for the form (served by the API it validates against).
  const meta = useQuery({ queryKey: ["meta"], queryFn: () => api.getMeta() });

  const save = useMutation({
    mutationFn: (data: PolicyUpsert) => api.putPolicy(partnerId, data),
    onSuccess: () => setEditing(false),
    // Also on error: a failed AWE submission still records a (failed) version.
    onSettled: () => qc.invalidateQueries({ queryKey: ["policies", partnerId] }),
  });
  const resubmit = useMutation({
    mutationFn: (version: number) => api.resubmitPolicy(partnerId, version),
    onSettled: () => qc.invalidateQueries({ queryKey: ["policies", partnerId] }),
  });

  const list = versions.data ?? [];
  const active = list.find((p) => p.status === "active");
  const pending = list.find((p) => p.status === "pending");
  // The newest version, if it ended without taking effect (failed / stale /
  // rejected): surfaced so the admin can resubmit it.
  const latest = list[0];
  const unresolved = latest && RESUBMITTABLE.includes(latest.status) ? latest : undefined;

  // Show the form when editing, or when there is nothing to display yet.
  if (editing || (list.length === 0 && !versions.isLoading)) {
    if (meta.isLoading) return <div className="loading">Loading policy options…</div>;
    if (!meta.data)
      return (
        <div className="notice notice-error">
          Could not load the allowed policy values.{" "}
          {meta.error instanceof ApiError ? meta.error.message : ""}
        </div>
      );
    return (
      <PolicyForm
        meta={meta.data}
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
      {resubmit.error instanceof ApiError && (
        <div className="notice notice-error">{resubmit.error.message}</div>
      )}

      {unresolved && !pending && (
        <div className={`notice ${unresolved.status === "stale" ? "notice-pending" : "notice-error"}`}>
          <strong>
            Policy v{unresolved.version} was {unresolvedLabel(unresolved.status)}.
          </strong>{" "}
          {unresolved.status_reason}{" "}
          {active ? `The active policy (v${active.version}) is unchanged.` : ""}
          <div style={{ marginTop: 8 }}>
            <button
              className="btn-secondary"
              disabled={resubmit.isPending}
              onClick={() => resubmit.mutate(unresolved.version)}
            >
              {resubmit.isPending ? "Resubmitting…" : `Resubmit v${unresolved.version}`}
            </button>
          </div>
        </div>
      )}

      {pending && (
        <div className="notice notice-pending">
          <strong>Policy v{pending.version} is awaiting approval.</strong> It widens access, so it
          will only take effect once approvers sign off. The active policy below stays in force
          until then. Narrowing changes still apply immediately; if one is made meanwhile, this
          version will not be applied on approval (it ends <em>stale</em> and can be resubmitted).
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

        {active?.issues && active.issues.length > 0 && (
          <div className="notice notice-error" style={{ marginTop: 16 }}>
            <strong>This policy has values the current rules reject.</strong> It is still in
            force as stored; fix these when you next edit it:
            <ul style={{ margin: "6px 0 0" }}>
              {active.issues.map((i) => (
                <li key={i}>
                  <code className="mono">{i}</code>
                </li>
              ))}
            </ul>
          </div>
        )}

        {active ? (
          <table className="data">
            <tbody>
              <PolicyRow label="Allowed data scopes" values={active.allowed_data_scopes} />
              <PolicyRow label="Allowed purposes" values={active.allowed_purposes} />
              <PolicyRow label="Allowed subject ID types" values={active.allowed_subject_id_types} />
              <PolicyRow
                label="Allowed signing algs"
                values={active.allowed_signing_algs}
                allowed={meta.data?.signing_algorithms}
              />
              <tr>
                <th style={{ width: 220 }}>Max validity</th>
                <td>
                  <DurationCell value={active.max_validity_duration} />
                </td>
              </tr>
              <tr>
                <th>Fetch type</th>
                <td>
                  {meta.data && !meta.data.fetch_types.includes(active.fetch_type) ? (
                    <span className="chip invalid">{active.fetch_type} (not supported)</span>
                  ) : (
                    fetchTypeLabel(active.fetch_type)
                  )}
                </td>
              </tr>
              {active.fetch_type === "periodic" && (
                <tr>
                  <th>Min interval between fetches</th>
                  <td>
                    <DurationCell value={active.max_fetch_frequency} />
                  </td>
                </tr>
              )}
              <tr>
                <th>Data life</th>
                <td>
                  <DurationCell value={active.data_life} />
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

      {list.length > 0 && (
        <VersionHistory
          versions={list}
          canResubmit={!pending && !resubmit.isPending}
          onResubmit={(v) => resubmit.mutate(v)}
        />
      )}
    </div>
  );
}

function unresolvedLabel(status: string): string {
  switch (status) {
    case "failed":
      return "not submitted for approval";
    case "stale":
      return "approved but not applied";
    default:
      return "rejected";
  }
}

function VersionHistory({
  versions,
  canResubmit,
  onResubmit,
}: {
  versions: PartnerPolicy[];
  canResubmit: boolean;
  onResubmit: (version: number) => void;
}) {
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
            <th />
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.id}>
              <td>v{v.version}</td>
              <td>
                <span className={`badge badge-${v.status}`}>{v.status}</span>
                {v.issues && v.issues.length > 0 && (
                  <span className="chip invalid" title={v.issues.join("\n")} style={{ marginLeft: 8 }}>
                    needs fixing
                  </span>
                )}
                {v.status_reason && (
                  <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                    {v.status_reason}
                  </div>
                )}
              </td>
              <td className="muted">{v.allowed_data_scopes.length} scope(s)</td>
              <td className="muted">
                {v.effective_from ? new Date(v.effective_from).toLocaleString() : "—"}
              </td>
              <td>
                {RESUBMITTABLE.includes(v.status) && (
                  <button
                    className="btn-secondary"
                    disabled={!canResubmit}
                    title={canResubmit ? "Save this version again as a new version" : "Another version is awaiting approval"}
                    onClick={() => onResubmit(v.version)}
                  >
                    Resubmit
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// A stored duration; one the current rules reject is flagged, not hidden.
function DurationCell({ value }: { value?: string | null }) {
  if (durationError(value))
    return <span className="chip invalid">{value} (not a valid duration)</span>;
  return <>{humaniseDuration(value)}</>;
}

// `allowed`, when given, flags values outside it (a policy saved before validation).
function PolicyRow({
  label,
  values,
  allowed,
}: {
  label: string;
  values: string[];
  allowed?: string[];
}) {
  return (
    <tr>
      <th style={{ width: 220 }}>{label}</th>
      <td>
        <div className="chips">
          {values.length === 0 && <span className="muted">—</span>}
          {values.map((v) =>
            allowed && !allowed.includes(v) ? (
              <span key={v} className="chip invalid" title="Not a supported value">
                {v} (not supported)
              </span>
            ) : (
              <span key={v} className="chip selected">
                {v}
              </span>
            )
          )}
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
};

function PolicyForm({
  meta,
  initial,
  onSave,
  onCancel,
  pending,
  error,
}: {
  meta: PolicyMeta;
  initial?: PolicyUpsert;
  onSave: (data: PolicyUpsert) => void;
  onCancel?: () => void;
  pending: boolean;
  error?: string;
}) {
  const [form, setForm] = useState<PolicyUpsert>(() => {
    if (!initial) {
      // Only pre-tick algorithms the verifier accepts.
      const algs = DEFAULT_POLICY.allowed_signing_algs.filter((a) =>
        meta.signing_algorithms.includes(a)
      );
      return { ...DEFAULT_POLICY, allowed_signing_algs: algs };
    }
    // Copy only the policy fields (not id/version/issues of the active version).
    return {
      allowed_data_scopes: initial.allowed_data_scopes ?? [],
      allowed_purposes: initial.allowed_purposes ?? [],
      allowed_subject_id_types: initial.allowed_subject_id_types ?? [],
      allowed_signing_algs: initial.allowed_signing_algs ?? [],
      max_validity_duration: initial.max_validity_duration ?? null,
      fetch_type: initial.fetch_type,
      max_fetch_frequency: initial.max_fetch_frequency ?? null,
      data_life: initial.data_life ?? null,
    };
  });
  const set = <K extends keyof PolicyUpsert>(k: K) => (v: PolicyUpsert[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  // Same rules as the API, so the form cannot submit what it would reject.
  const badAlgs = form.allowed_signing_algs.filter((a) => !meta.signing_algorithms.includes(a));
  const algError =
    form.allowed_signing_algs.length === 0
      ? "Select at least one algorithm."
      : badAlgs.length
        ? `Untick unsupported: ${badAlgs.join(", ")}.`
        : null;
  const fetchTypeOk = meta.fetch_types.includes(form.fetch_type);
  const durationsOk = [form.max_validity_duration, form.max_fetch_frequency, form.data_life].every(
    (d) => !durationError(d)
  );
  const valid = !algError && fetchTypeOk && durationsOk;
  // Also show the periodic fields when a stored value there needs fixing.
  const showPeriodic =
    form.fetch_type === "periodic" ||
    !!durationError(form.max_fetch_frequency) ||
    !!durationError(form.data_life);

  return (
    <form
      className="card"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) onSave(form);
      }}
    >
      <h3 className="card-title">{initial ? "Edit policy" : "Define policy"}</h3>
      <p className="muted" style={{ marginTop: -8 }}>
        The policy is the outer bound on every consent. Effective fields returned to the registry
        are always the consent's scope ∩ this policy. Lists: one entry per line (or
        comma-separated); an empty purpose or subject-ID-type list allows any.
      </p>

      <ListTextarea
        label="Allowed data scopes"
        value={form.allowed_data_scopes}
        onChange={set("allowed_data_scopes")}
        suggestions={meta.known_data_scopes}
        placeholder={"farmer_profile.basic\nfarmer_profile.landholding"}
      />
      <ListTextarea
        label="Allowed purposes"
        value={form.allowed_purposes}
        onChange={set("allowed_purposes")}
        suggestions={meta.known_purposes}
        placeholder={"loan_origination\nsubsidy_verification"}
      />
      <ListTextarea
        label="Allowed subject ID types"
        value={form.allowed_subject_id_types}
        onChange={set("allowed_subject_id_types")}
        suggestions={meta.known_subject_id_types}
        placeholder={"national_id\nfarmer_id"}
      />
      <CheckboxGroup
        label="Allowed signing algorithms"
        options={meta.signing_algorithms}
        value={form.allowed_signing_algs}
        onChange={set("allowed_signing_algs")}
        error={algError}
        hint="JWS algorithms the partner may sign consent objects with."
      />

      <div className="row" style={{ alignItems: "flex-start", gap: 24 }}>
        <div style={{ flex: 1 }}>
          <DurationInput
            label="Max validity"
            value={form.max_validity_duration}
            onChange={set("max_validity_duration")}
          />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label htmlFor="policy-fetch-type">Fetch type</label>
          <select
            id="policy-fetch-type"
            value={form.fetch_type}
            onChange={(e) => set("fetch_type")(e.target.value as PolicyUpsert["fetch_type"])}
          >
            {!fetchTypeOk && (
              <option value={form.fetch_type} disabled>
                {form.fetch_type} (not supported)
              </option>
            )}
            {meta.fetch_types.map((t) => (
              <option key={t} value={t}>
                {fetchTypeLabel(t)}
              </option>
            ))}
          </select>
          {!fetchTypeOk && <div className="field-error">Choose a supported fetch type.</div>}
        </div>
      </div>

      {showPeriodic && (
        <div className="row" style={{ alignItems: "flex-start", gap: 24 }}>
          <div style={{ flex: 1 }}>
            <DurationInput
              label="Min interval between fetches"
              noneLabel="Not set"
              value={form.max_fetch_frequency}
              onChange={set("max_fetch_frequency")}
            />
          </div>
          <div style={{ flex: 1 }}>
            <DurationInput label="Data life" value={form.data_life} onChange={set("data_life")} />
          </div>
        </div>
      )}

      {error && <div className="notice notice-error">{error}</div>}

      <div className="notice notice-info">
        Saving a policy that widens access is submitted for approval before it takes effect.
      </div>

      <div className="row">
        <button type="submit" className="btn-primary" disabled={pending || !valid}>
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
