import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { useAccess, useRequestSettings } from "../api/hooks";
import type { ConsentRequestCreate, ScopePolicy } from "../api/types";
import { CheckboxGroup, CodeSelect, codeLabels } from "../components/FormControls";
import { ErrorNotice } from "../components/Status";
import TemplateRequestForm from "../components/TemplateRequestForm";
import { dayEnd, dayStart, today } from "../format";

// The ID types a partner user can enter when Master Data has no list of
// subject ID types; types allowed by the partner's policies are added to these.
const BASE_ID_TYPES = ["FAYDA_FAN", "FARMER_ID"];

// At the Exchange (consent_by "template") a request names a consent template
// offered to the partner; elsewhere it names registries and data within the
// scope policies.
export default function NewRequestPage() {
  const settings = useRequestSettings();
  if (settings.isLoading) return <div className="loading">Loading…</div>;
  if (settings.error) return <ErrorNotice error={settings.error} what="the request settings" />;
  if (settings.data?.consent_by === "template") return <TemplateRequestForm />;
  return <ScopeRequestForm />;
}

function ScopeRequestForm() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params] = useSearchParams();

  const access = useAccess();

  const [idType, setIdType] = useState("FAYDA_FAN");
  const [idValue, setIdValue] = useState("");
  // Other identifiers the CM requires (e.g. FARMER_ID in Agri Stack).
  const settings = useRequestSettings();
  const requiredIds = (settings.data?.required_subject_identifiers ?? []).filter((t) => t !== idType);
  const [extraIds, setExtraIds] = useState<Record<string, string>>({});
  const [purpose, setPurpose] = useState("");
  const [scopes, setScopes] = useState<Record<string, string[]>>({});
  const [validFrom, setValidFrom] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [templateLabel, setTemplateLabel] = useState(params.get("template") ?? "");
  const [touched, setTouched] = useState(false);

  const list: ScopePolicy[] = useMemo(() => access.data?.scope_policies ?? [], [access.data]);
  const byController = useMemo(
    () => Object.fromEntries(list.map((b) => [b.data_controller, b])),
    [list]
  );
  const selected = Object.entries(scopes)
    .filter(([, s]) => s.length > 0)
    .map(([c]) => c);
  // Registries asked for that this partner has no policy for.
  const unbound = Object.keys(scopes).filter((c) => !byController[c] && scopes[c].length > 0);

  // Master Data code lists (null: free text).
  const mdsIdTypes = settings.data?.subject_id_types ?? null;
  const mdsPurposes = settings.data?.purposes ?? null;
  const idTypeLabels = useMemo(() => codeLabels(mdsIdTypes), [mdsIdTypes]);
  const purposeLabels = useMemo(() => codeLabels(mdsPurposes), [mdsPurposes]);

  const idTypeOptions = useMemo(() => {
    if (mdsIdTypes) return mdsIdTypes.values.map((v) => v.code);
    const s = new Set(BASE_ID_TYPES);
    list.forEach((b) => b.allowed_subject_id_types.forEach((t) => s.add(t)));
    return [...s];
  }, [list, mdsIdTypes]);

  // The purposes the chosen registries' policies allow; a policy that allows
  // any purpose (empty list) offers the Master Data list, else free text.
  const selectedKey = selected.join(",");
  const purposeOptions = useMemo(() => {
    const chosen = selectedKey ? selectedKey.split(",") : [];
    const from = chosen.length ? chosen.map((c) => byController[c]).filter(Boolean) : list;
    if (from.some((b) => b.allowed_purposes.length === 0))
      return mdsPurposes ? mdsPurposes.values.map((v) => v.code) : null;
    const s = new Set<string>();
    from.forEach((b) => b.allowed_purposes.forEach((p) => s.add(p)));
    return [...s].sort();
  }, [selectedKey, byController, list, mdsPurposes]);

  // ── Validation (the API checks again against the policies) ──────────
  const problems: string[] = [];
  if (!idValue.trim()) problems.push("Enter the subject's ID.");
  for (const t of requiredIds) if (!(extraIds[t] ?? "").trim()) problems.push(`Enter the subject's ${t}.`);
  if (!purpose.trim()) problems.push("Choose a purpose.");
  if (mdsIdTypes && !idTypeOptions.includes(idType))
    problems.push(`Subject ID type ${idType} is not in Master Data list ${mdsIdTypes.list_code}.`);
  if (mdsPurposes && purpose && !mdsPurposes.values.some((v) => v.code === purpose))
    problems.push(`Purpose "${purpose}" is not in Master Data list ${mdsPurposes.list_code}.`);
  if (selected.length === 0) problems.push("Choose the data to request from at least one registry.");
  if (unbound.length)
    problems.push(`Your organisation has no policy for: ${unbound.join(", ")}. Remove those registries.`);
  for (const c of selected) {
    const b = byController[c];
    if (!b) continue;
    const bad = scopes[c].filter((s) => !b.allowed_data_scopes.includes(s));
    if (bad.length) problems.push(`${c}: you may not ask for ${bad.join(", ")} — untick them.`);
    if (purpose && b.allowed_purposes.length && !b.allowed_purposes.includes(purpose))
      problems.push(`${c}: purpose "${purpose}" is not allowed for this registry.`);
    if (b.allowed_subject_id_types.length && !b.allowed_subject_id_types.includes(idType))
      problems.push(`${c}: subject ID type ${idType} is not accepted by this registry.`);
  }
  if (validFrom && validUntil && validUntil < validFrom)
    problems.push("“Valid until” must be on or after “Valid from”.");
  if (validUntil && validUntil < today()) problems.push("“Valid until” is in the past.");

  const create = useMutation({
    mutationFn: () => {
      const body: ConsentRequestCreate = {
        subject_id: { type: idType, value: idValue.trim() },
        subject_identifiers: requiredIds.map((t) => ({ type: t, value: (extraIds[t] ?? "").trim() })),
        purpose: { code: purpose.trim() },
        grants: selected.map((c) => ({ data_controller: c, data_scopes: scopes[c] })),
      };
      if (validFrom || validUntil) {
        body.validity = {};
        if (validFrom) body.validity.valid_from = dayStart(validFrom);
        if (validUntil) body.validity.valid_until = dayEnd(validUntil);
      }
      if (templateLabel.trim()) body.template = templateLabel.trim();
      return api.createRequest(body);
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["requests"] });
      navigate(`/requests/${r.id}`);
    },
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (problems.length === 0) create.mutate();
  };

  return (
    <div>
      <div className="spread">
        <h1 style={{ margin: 0 }}>New consent</h1>
        <Link to="/consents" className="btn-secondary">
          Back to consents
        </Link>
      </div>
      <p className="muted page-intro">
        Raise a request while the subject is with you. Next, upload the consent form the subject
        signed and submit the request for verification.
      </p>

      {access.isLoading && <div className="loading">Loading your policies…</div>}
      {access.error && <ErrorNotice error={access.error} what="your policies" />}
      {access.data && list.length === 0 && (
        <div className="card">
          Your organisation has no active data-share policy, so it cannot request consent yet.
        </div>
      )}

      {access.data && list.length > 0 && (
        <form onSubmit={onSubmit}>
          <div className="card">
            <h3 className="card-title">Subject</h3>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="id-type">ID type</label>
                <CodeSelect id="id-type" value={idType} onChange={setIdType} options={idTypeOptions} labels={idTypeLabels} />
              </div>
              <div className="field">
                <label htmlFor="id-value">ID number</label>
                <input
                  id="id-value"
                  type="text"
                  autoComplete="off"
                  value={idValue}
                  onChange={(e) => setIdValue(e.target.value)}
                  placeholder="As on the subject's ID"
                />
              </div>
            </div>
            {requiredIds.map((t) => (
              <div className="grid-2" key={t}>
                <div className="field">
                  <label htmlFor={`id-${t}`}>{t}</label>
                  <input
                    id={`id-${t}`}
                    type="text"
                    autoComplete="off"
                    value={extraIds[t] ?? ""}
                    onChange={(e) => setExtraIds({ ...extraIds, [t]: e.target.value })}
                    placeholder={`The subject's ${t}`}
                  />
                  <div className="hint">Required. Consent Manager staff check it in the registry before approving.</div>
                </div>
              </div>
            ))}
          </div>

          <div className="card">
            <h3 className="card-title">Purpose and data</h3>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="purpose">Purpose</label>
                <CodeSelect
                  id="purpose"
                  value={purpose}
                  onChange={setPurpose}
                  options={purposeOptions}
                  labels={purposeLabels}
                  placeholder="e.g. credit-assessment"
                  choose
                />
              </div>
              <div className="field">
                <label htmlFor="template">Consent template (optional)</label>
                <input
                  id="template"
                  type="text"
                  value={templateLabel}
                  onChange={(e) => setTemplateLabel(e.target.value)}
                  placeholder="e.g. loan-profile@1"
                />
                <div className="hint">A label shown with the request; the data asked for is below.</div>
              </div>
            </div>

            {[...list.map((b) => b.data_controller), ...unbound].map((c) => {
              const b = byController[c];
              const value = scopes[c] ?? [];
              return (
                <div className="grant" key={c}>
                  <div className="grant-head">
                    Registry <code className="mono">{c}</code>
                    {b && (
                      <span className="row" style={{ gap: 8, marginLeft: "auto" }}>
                        <button
                          type="button"
                          className="btn-secondary"
                          onClick={() => setScopes({ ...scopes, [c]: [...b.allowed_data_scopes] })}
                        >
                          All
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          onClick={() => setScopes({ ...scopes, [c]: [] })}
                        >
                          None
                        </button>
                      </span>
                    )}
                  </div>
                  {b ? (
                    <CheckboxGroup
                      label="Data to request"
                      options={b.allowed_data_scopes}
                      value={value}
                      onChange={(next) => setScopes({ ...scopes, [c]: next })}
                      hint={value.length === 0 ? "Nothing ticked: no data from this registry." : undefined}
                    />
                  ) : (
                    <div className="notice notice-error">
                      Your organisation has no policy for this registry.{" "}
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => setScopes({ ...scopes, [c]: [] })}
                      >
                        Remove
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <div className="card">
            <h3 className="card-title">Validity</h3>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="valid-from">Valid from (optional)</label>
                <input
                  id="valid-from"
                  type="date"
                  value={validFrom}
                  onChange={(e) => setValidFrom(e.target.value)}
                />
                <div className="hint">Empty: from approval.</div>
              </div>
              <div className="field">
                <label htmlFor="valid-until">Valid until (optional)</label>
                <input
                  id="valid-until"
                  type="date"
                  min={validFrom || today()}
                  value={validUntil}
                  onChange={(e) => setValidUntil(e.target.value)}
                />
                <div className="hint">Empty: the default validity under your policy.</div>
              </div>
            </div>
          </div>

          {touched && problems.length > 0 && (
            <div className="notice notice-error">
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          {create.error && (
            <div className="notice notice-error">
              {create.error instanceof ApiError ? create.error.message : "Could not create the request."}
            </div>
          )}

          <div className="actions">
            <Link to="/consents" className="btn-secondary">
              Cancel
            </Link>
            <button type="submit" className="btn-primary" disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create request"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
