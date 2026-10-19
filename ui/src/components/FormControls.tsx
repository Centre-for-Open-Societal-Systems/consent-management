// Small form controls shared by the staff forms: a checkbox group for a closed
// multi-choice list, a list textarea with suggestions for open lists, and an
// ISO-8601 duration picker. Allowed values come from GET /consent/v1/meta.
import { useId, useState } from "react";

// ── Checkbox group (closed multi-choice) ───────────────────────────────────
// Values already selected but no longer allowed (e.g. a policy saved before
// validation) stay visible, flagged, so they can be unticked.
export function CheckboxGroup({
  label,
  options,
  value,
  onChange,
  hint,
  error,
}: {
  label: string;
  options: string[];
  value: string[];
  onChange: (next: string[]) => void;
  hint?: React.ReactNode;
  error?: string | null;
}) {
  const hintId = useId();
  const unknown = value.filter((v) => !options.includes(v));
  const toggle = (opt: string) =>
    onChange(value.includes(opt) ? value.filter((v) => v !== opt) : [...value, opt]);

  return (
    <fieldset className="field checks-field" aria-describedby={hintId}>
      <legend>{label}</legend>
      <div className="checks">
        {[...options, ...unknown].map((opt) => {
          const bad = unknown.includes(opt);
          return (
            <label key={opt} className={bad ? "check invalid" : "check"}>
              <input type="checkbox" checked={value.includes(opt)} onChange={() => toggle(opt)} />
              <code className="mono">{opt}</code>
              {bad && <span className="flag">not supported — untick</span>}
            </label>
          );
        })}
      </div>
      <div id={hintId}>
        {error && <div className="field-error">{error}</div>}
        {hint && <div className="hint">{hint}</div>}
      </div>
    </fieldset>
  );
}

// ── List textarea with suggestions (open lists) ────────────────────────────
// One entry per line (or comma-separated). `suggestions` are values already in
// use elsewhere; clicking one adds it. They are hints, not a closed list.
export function ListTextarea({
  label,
  value,
  onChange,
  suggestions = [],
  placeholder,
  hint,
}: {
  label: string;
  value: string[];
  onChange: (next: string[]) => void;
  suggestions?: string[];
  placeholder?: string;
  hint?: React.ReactNode;
}) {
  const id = useId();
  // Keep the raw text so typing a trailing comma/newline is not swallowed.
  const [text, setText] = useState(value.join("\n"));
  const parse = (t: string) =>
    t
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
  const offered = suggestions.filter((s) => !value.includes(s)).slice(0, 30);

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        value={text}
        placeholder={placeholder}
        onChange={(e) => {
          setText(e.target.value);
          onChange(parse(e.target.value));
        }}
      />
      {offered.length > 0 && (
        <div className="suggestions">
          <span className="muted">In use elsewhere:</span>
          {offered.map((s) => (
            <button
              type="button"
              key={s}
              className="chip"
              onClick={() => {
                const next = [...value, s];
                setText(next.join("\n"));
                onChange(next);
              }}
            >
              + {s}
            </button>
          ))}
        </div>
      )}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

// ── ISO-8601 duration picker ───────────────────────────────────────────────
const UNITS: { key: string; label: string; iso: (n: string) => string }[] = [
  { key: "H", label: "hours", iso: (n) => `PT${n}H` },
  { key: "D", label: "days", iso: (n) => `P${n}D` },
  { key: "W", label: "weeks", iso: (n) => `P${n}W` },
  { key: "M", label: "months", iso: (n) => `P${n}M` },
  { key: "Y", label: "years", iso: (n) => `P${n}Y` },
];

const ISO_DURATION =
  /^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$/;

// Same rules as the API: ISO-8601 (P30D, P1Y, PT12H, P1DT6H), longer than zero.
export function durationError(iso?: string | null): string | null {
  if (!iso) return null;
  if (iso.length > 32) return "Too long.";
  const m = ISO_DURATION.exec(iso);
  if (!m) return "Enter a whole number greater than zero, or a valid ISO-8601 duration.";
  if (!m.slice(1).some((v) => v && Number(v) > 0)) return "Must be longer than zero.";
  return null;
}

function splitDuration(iso?: string | null): { unit: string; n: string; custom: string } {
  if (!iso) return { unit: "", n: "", custom: "" };
  const day = /^P(\d+)([DWMY])$/.exec(iso);
  if (day) return { unit: day[2], n: day[1], custom: "" };
  const hour = /^PT(\d+)H$/.exec(iso);
  if (hour) return { unit: "H", n: hour[1], custom: "" };
  return { unit: "custom", n: "", custom: iso };
}

// Number + unit, with "No limit" (null) and a raw ISO-8601 fallback for values
// a single unit cannot express (e.g. P1DT6H).
export function DurationInput({
  label,
  value,
  onChange,
  noneLabel = "No limit",
  hint,
}: {
  label: string;
  value?: string | null;
  onChange: (next: string | null) => void;
  noneLabel?: string;
  hint?: React.ReactNode;
}) {
  const id = useId();
  const [state, setState] = useState(() => splitDuration(value));
  const error = durationError(value);

  const emit = (next: { unit: string; n: string; custom: string }) => {
    setState(next);
    if (!next.unit) onChange(null);
    else if (next.unit === "custom") onChange(next.custom.trim().toUpperCase() || null);
    else onChange(UNITS.find((u) => u.key === next.unit)!.iso(next.n.trim()));
  };

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="row duration">
        {state.unit && state.unit !== "custom" && (
          <input
            id={id}
            type="number"
            min={1}
            step={1}
            value={state.n}
            aria-invalid={!!error}
            onChange={(e) => emit({ ...state, n: e.target.value })}
          />
        )}
        {state.unit === "custom" && (
          <input
            id={id}
            type="text"
            value={state.custom}
            placeholder="P1DT6H"
            aria-invalid={!!error}
            onChange={(e) => emit({ ...state, custom: e.target.value })}
          />
        )}
        <select
          id={state.unit ? undefined : id}
          aria-label={`${label} unit`}
          value={state.unit}
          onChange={(e) => {
            const unit = e.target.value;
            if (unit === "custom") emit({ unit, n: "", custom: value ?? "" });
            else emit({ unit, n: state.n || (unit ? "1" : ""), custom: "" });
          }}
        >
          <option value="">{noneLabel}</option>
          {UNITS.map((u) => (
            <option key={u.key} value={u.key}>
              {u.label}
            </option>
          ))}
          <option value="custom">Custom (ISO-8601)</option>
        </select>
      </div>
      {error ? (
        <div className="field-error">{error}</div>
      ) : (
        value && <div className="hint">{value} — {humaniseDuration(value)}</div>
      )}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

// Render an ISO-8601 duration (P1Y, P30D, PT12H, P1DT6H) in plain words.
export function humaniseDuration(iso?: string | null): string {
  if (!iso) return "—";
  const m = ISO_DURATION.exec(iso.trim());
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
