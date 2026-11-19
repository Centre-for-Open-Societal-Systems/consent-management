import type { ConsentTemplate } from "../api/types";

// A consent template offered to the partner: its purpose and data per
// registry. With `chosen` and `onToggle`, the optional data is a choice (the
// subject may decline it); required data is always part of the consent.
export function TemplateFacts({
  t,
  chosen,
  onToggle,
}: {
  t: ConsentTemplate;
  chosen?: Record<string, string[]>;
  onToggle?: (controller: string, scope: string) => void;
}) {
  return (
    <dl className="facts">
      <dt>Purpose</dt>
      <dd>{t.purpose ? <code className="mono">{t.purpose}</code> : <span className="muted">—</span>}</dd>
      {t.grants.map((g) => (
        <div key={g.data_controller} style={{ display: "contents" }}>
          <dt>
            <code className="mono">{g.data_controller}</code>
          </dt>
          <dd>
            <div className="chips">
              {g.required_scopes.map((x) => (
                <span key={x} className="chip static selected" title="Required">
                  {x}
                </span>
              ))}
              {g.optional_scopes.map((x) =>
                onToggle ? (
                  <button
                    type="button"
                    key={x}
                    className={`chip optional${chosen?.[g.data_controller]?.includes(x) ? " selected" : ""}`}
                    aria-pressed={!!chosen?.[g.data_controller]?.includes(x)}
                    title="Optional: the subject may decline it"
                    onClick={() => onToggle(g.data_controller, x)}
                  >
                    {x} (optional)
                  </button>
                ) : (
                  <span key={x} className="chip static optional" title="Optional">
                    {x} (optional)
                  </span>
                )
              )}
            </div>
          </dd>
        </div>
      ))}
    </dl>
  );
}
