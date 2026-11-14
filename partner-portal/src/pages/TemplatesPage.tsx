import { useAccess } from "../api/hooks";
import type { ConsentTemplate, ScopePolicy } from "../api/types";
import { ErrorNotice } from "../components/Status";
import { TemplateFacts } from "../components/TemplateFacts";

function Chips({ values, empty }: { values: string[]; empty: string }) {
  if (!values.length) return <span className="muted">{empty}</span>;
  return (
    <div className="chips">
      {values.map((v) => (
        <span key={v} className="chip static">
          <code className="mono">{v}</code>
        </span>
      ))}
    </div>
  );
}

function ScopePolicyCard({ b }: { b: ScopePolicy }) {
  return (
    <div className="card">
      <h3 className="card-title">
        Registry <code className="mono">{b.data_controller}</code>
      </h3>
      <dl className="facts">
        <dt>Data scopes</dt>
        <dd>
          <Chips values={b.allowed_data_scopes} empty="None" />
        </dd>
        <dt>Purposes</dt>
        <dd>
          <Chips values={b.allowed_purposes} empty="None" />
        </dd>
        <dt>Subject ID types</dt>
        <dd>
          <Chips values={b.allowed_subject_id_types} empty="Any" />
        </dd>
        <dt>Policy version</dt>
        <dd>v{b.policy_version}</dd>
      </dl>
    </div>
  );
}

function TemplateCard({ t }: { t: ConsentTemplate }) {
  return (
    <div className="card">
      <div className="spread" style={{ alignItems: "flex-start" }}>
        <div>
          <h3 style={{ margin: 0 }}>{t.title || t.code}</h3>
          <div className="muted">
            <code className="mono">{t.code}</code>
          </div>
        </div>
      </div>
      {t.description && <p style={{ marginTop: 0 }}>{t.description}</p>}
      {t.available === false && <div className="notice notice-error">{t.detail || "Not available now."}</div>}
      {t.available !== false && <TemplateFacts t={t} />}
      {t.allowed_subject_id_types.length > 0 && (
        <div className="muted">Subject ID types accepted: {t.allowed_subject_id_types.join(", ")}</div>
      )}
      {t.max_validity_duration && (
        <div className="muted">Consent valid for at most {t.max_validity_duration}</div>
      )}
    </div>
  );
}

// Read-only: what the partner may ask consent for. At the Exchange, the consent
// templates offered to it (purpose, data per registry and limits, from the
// Consent Manager's template source); elsewhere its data access per registry
// (scope policies in Partner Management). Neither is set here; new consents
// start on the Consents page.
export default function TemplatesPage() {
  const { data, isLoading, error } = useAccess();
  const byTemplate = data?.consent_by === "template";

  return (
    <div>
      <h1>{byTemplate ? "Consent templates" : "Data access"}</h1>
      {byTemplate ? (
        <p className="muted page-intro">
          The consent templates offered to your organisation, for information. A consent is asked
          from one template: the subject agrees to share the data it names from each registry, for
          its purpose. To add or change one, ask the Partner Management administrator.
        </p>
      ) : (
        <p className="muted page-intro">
          For information: per registry, the data you may ask a subject to share, for which
          purposes, and the subject IDs accepted. To change it, ask the Consent Manager / Partner
          Management administrator.
        </p>
      )}

      {isLoading && <div className="loading">Loading…</div>}
      {error && <ErrorNotice error={error} what={byTemplate ? "your consent templates" : "your data access"} />}

      {data && byTemplate && data.templates.length === 0 && (
        <div className="card">
          No consent template is offered to your organisation yet, so it cannot request consent.
          Ask the Partner Management administrator.
        </div>
      )}
      {data && !byTemplate && data.scope_policies.length === 0 && (
        <div className="card">
          Your organisation has no active data-share policy yet, so it cannot request consent. Ask
          the Consent Manager / Partner Management administrator to set one in Partner Management.
        </div>
      )}

      {byTemplate
        ? data?.templates.map((t) => <TemplateCard key={t.code} t={t} />)
        : data?.scope_policies.map((b) => <ScopePolicyCard key={b.data_controller} b={b} />)}
    </div>
  );
}
