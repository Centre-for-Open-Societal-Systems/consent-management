import { useBindings } from "../api/hooks";
import { ErrorNotice } from "../components/Status";

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

// Read-only: what the partner's active data-share policies allow it to ask for,
// per registry (data controller). Policies are set by staff, not here.
export default function BindingsPage() {
  const { data, isLoading, error } = useBindings();

  return (
    <div>
      <h1>What we can ask for</h1>
      <p className="muted page-intro">
        Your organisation's active data-share policies: per registry, the data you may ask a subject
        to share, for which purposes, and the subject IDs accepted. Policies are agreed with and set
        by the Consent Manager staff.
      </p>

      {isLoading && <div className="loading">Loading…</div>}
      {error && <ErrorNotice error={error} what="your policies" />}
      {data && data.length === 0 && (
        <div className="card">
          Your organisation has no active data-share policy yet, so it cannot request consent. Contact
          the Consent Manager staff.
        </div>
      )}

      {data?.map((b) => (
        <div className="card" key={b.data_controller}>
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
          </dl>
        </div>
      ))}
    </div>
  );
}
