import { Link } from "react-router-dom";
import { useUseCases } from "../api/hooks";
import { ErrorNotice } from "../components/Status";

// Published use cases of the composite (public list). Each shows the consent it
// needs per registry; "New request from this use case" prefills the form.
export default function UseCasesPage() {
  const { data, isLoading, error } = useUseCases();

  return (
    <div>
      <h1>Use cases</h1>
      <p className="muted page-intro">
        The queries you can run through the data exchange, and the consent each one needs from the
        subject: per registry, the <strong>required</strong> data and the <em>optional</em> data
        (used when consented).
      </p>

      {isLoading && <div className="loading">Loading…</div>}
      {error && <ErrorNotice error={error} what="use cases" />}
      {data && data.length === 0 && <div className="card">No use cases are published.</div>}

      {data?.map((u) => {
        const scopes = Object.entries(u.consent_scopes ?? {});
        return (
          <div className="card" key={u.use_case}>
            <div className="spread" style={{ alignItems: "flex-start" }}>
              <div>
                <h3 style={{ margin: 0 }}>{u.title || u.name}</h3>
                <div className="muted">
                  <code className="mono">{u.use_case}</code> · version {u.version}
                  {u.purpose && (
                    <>
                      {" "}
                      · purpose <code className="mono">{u.purpose}</code>
                    </>
                  )}
                </div>
              </div>
              {scopes.length > 0 && (
                <Link
                  to={`/requests/new?use_case=${encodeURIComponent(u.use_case)}`}
                  className="btn-primary"
                >
                  New request from this use case
                </Link>
              )}
            </div>
            {u.description && <p style={{ marginTop: 0 }}>{u.description}</p>}
            {u.consent?.required === false && (
              <div className="notice notice-info">This use case does not need consent.</div>
            )}
            {scopes.length === 0 ? (
              <div className="muted">No registry scopes declared.</div>
            ) : (
              <dl className="facts">
                {scopes.map(([controller, s]) => (
                  <div key={controller} style={{ display: "contents" }}>
                    <dt>
                      <code className="mono">{controller}</code>
                    </dt>
                    <dd>
                      <div className="chips">
                        {s.required.map((x) => (
                          <span key={x} className="chip static selected" title="Required">
                            {x}
                          </span>
                        ))}
                        {s.optional.map((x) => (
                          <span key={x} className="chip static optional" title="Optional">
                            {x} (optional)
                          </span>
                        ))}
                      </div>
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        );
      })}
    </div>
  );
}
