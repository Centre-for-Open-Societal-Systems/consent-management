import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import { useAccess, useRequestSettings } from "../api/hooks";
import type { ConsentRequestCreate, ConsentTemplate } from "../api/types";
import { dayEnd, dayStart, today } from "../format";
import { CodeSelect, codeLabels } from "./FormControls";
import { ErrorNotice } from "./Status";
import { TemplateFacts } from "./TemplateFacts";

const BASE_ID_TYPES = ["FAYDA_FAN", "FARMER_ID"];

// Every scope of a template, by registry (required + optional).
function allScopes(t: ConsentTemplate): Record<string, string[]> {
  return Object.fromEntries(t.grants.map((g) => [g.data_controller, [...g.data_scopes]]));
}

// New request at the Exchange: the partner picks one of the consent templates
// offered to it. The purpose and the required data per registry are the
// template's (shown read-only); optional data is ticked unless the subject
// declines it. The Consent Manager checks the template again and applies its
// limits.
export default function TemplateRequestForm() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const access = useAccess();
  const settings = useRequestSettings();

  const templates = useMemo(
    () => (access.data?.templates ?? []).filter((t) => t.available !== false),
    [access.data]
  );
  const [code, setCode] = useState(params.get("template") ?? "");
  const template = templates.find((t) => t.code === code);
  // The scopes chosen per registry: all of the template's, until the subject
  // declines optional ones.
  const [chosen, setChosen] = useState<Record<string, string[]>>({});
  useEffect(() => {
    setChosen(template ? allScopes(template) : {});
  }, [template]);
  const toggle = (controller: string, scope: string) => {
    const now = chosen[controller] ?? [];
    setChosen({
      ...chosen,
      [controller]: now.includes(scope) ? now.filter((s) => s !== scope) : [...now, scope],
    });
  };

  const [idType, setIdType] = useState("FAYDA_FAN");
  const [idValue, setIdValue] = useState("");
  const requiredIds = (settings.data?.required_subject_identifiers ?? []).filter((t) => t !== idType);
  const [extraIds, setExtraIds] = useState<Record<string, string>>({});
  const [validFrom, setValidFrom] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [touched, setTouched] = useState(false);

  // One template offered: choose it.
  useEffect(() => {
    if (!code && templates.length === 1) setCode(templates[0].code);
  }, [code, templates]);

  // ID types: those the template accepts, else the Master Data list (labels
  // from it in every case).
  const mdsIdTypes = settings.data?.subject_id_types ?? null;
  const idTypeLabels = useMemo(() => codeLabels(mdsIdTypes), [mdsIdTypes]);
  const idTypeOptions = useMemo(() => {
    const allowed = template?.allowed_subject_id_types ?? [];
    const mds = mdsIdTypes?.values.map((v) => v.code) ?? [];
    return [...new Set(allowed.length ? allowed : mds.length ? mds : BASE_ID_TYPES)];
  }, [template, mdsIdTypes]);

  useEffect(() => {
    const allowed = template?.allowed_subject_id_types ?? [];
    if (allowed.length && !allowed.includes(idType)) setIdType(allowed[0]);
  }, [template, idType]);

  const problems: string[] = [];
  if (!template) problems.push("Choose a consent template.");
  if (!idValue.trim()) problems.push("Enter the subject's ID.");
  for (const t of requiredIds) if (!(extraIds[t] ?? "").trim()) problems.push(`Enter the subject's ${t}.`);
  const allowedTypes = template?.allowed_subject_id_types ?? [];
  if (allowedTypes.length && !allowedTypes.includes(idType))
    problems.push(`Subject ID type ${idType} is not accepted for this consent template.`);
  if (mdsIdTypes && !mdsIdTypes.values.some((v) => v.code === idType))
    problems.push(`Subject ID type ${idType} is not in Master Data list ${mdsIdTypes.list_code}.`);
  if (validFrom && validUntil && validUntil < validFrom)
    problems.push("“Valid until” must be on or after “Valid from”.");
  if (validUntil && validUntil < today()) problems.push("“Valid until” is in the past.");

  const create = useMutation({
    mutationFn: () => {
      const body: ConsentRequestCreate = {
        subject_id: { type: idType, value: idValue.trim() },
        subject_identifiers: requiredIds.map((t) => ({ type: t, value: (extraIds[t] ?? "").trim() })),
        template: code,
        // Registries left with no scope (only optional ones, all declined) are dropped.
        grants: Object.entries(chosen)
          .filter(([, scopes]) => scopes.length > 0)
          .map(([data_controller, data_scopes]) => ({ data_controller, data_scopes })),
      };
      if (validFrom || validUntil) {
        body.validity = {};
        if (validFrom) body.validity.valid_from = dayStart(validFrom);
        if (validUntil) body.validity.valid_until = dayEnd(validUntil);
      }
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

  const unknownCode = !!code && !!access.data && !template;

  return (
    <div>
      <div className="spread">
        <h1 style={{ margin: 0 }}>New consent</h1>
        <Link to="/consents" className="btn-secondary">
          Back to consents
        </Link>
      </div>
      <p className="muted page-intro">
        Raise a request while the subject is with you, from one of the consent templates offered to
        your organisation: the subject consents to share the data it names, for its purpose. Next,
        upload the consent form the subject signed and submit the request for verification.
      </p>

      {access.isLoading && <div className="loading">Loading your consent templates…</div>}
      {access.error && <ErrorNotice error={access.error} what="your consent templates" />}
      {access.data && templates.length === 0 && (
        <div className="card">
          No consent template can be used by your organisation now, so it cannot request consent
          yet. See “Consent templates”.
        </div>
      )}

      {access.data && templates.length > 0 && (
        <form onSubmit={onSubmit}>
          <div className="card">
            <h3 className="card-title">Consent template</h3>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="template">Consent template</label>
                <select id="template" value={template ? code : ""} onChange={(e) => setCode(e.target.value)}>
                  <option value="">Choose…</option>
                  {templates.map((t) => (
                    <option key={t.code} value={t.code}>
                      {t.title ? `${t.title} (${t.code})` : t.code}
                    </option>
                  ))}
                </select>
                {unknownCode && (
                  <div className="hint">
                    <code className="mono">{code}</code> is not offered to your organisation.
                  </div>
                )}
              </div>
            </div>
            {template && (
              <>
                {template.description && <p style={{ marginTop: 0 }}>{template.description}</p>}
                <TemplateFacts t={template} chosen={chosen} onToggle={toggle} />
                <div className="hint">
                  The consent covers this purpose and data (required, and the optional data ticked:
                  untick what the subject does not agree to share) and nothing else.
                </div>
              </>
            )}
          </div>

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
            <h3 className="card-title">Validity</h3>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="valid-from">Valid from (optional)</label>
                <input id="valid-from" type="date" value={validFrom} onChange={(e) => setValidFrom(e.target.value)} />
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
                <div className="hint">
                  Empty: the default validity
                  {template?.max_validity_duration ? ` (at most ${template.max_validity_duration})` : ""}.
                </div>
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
