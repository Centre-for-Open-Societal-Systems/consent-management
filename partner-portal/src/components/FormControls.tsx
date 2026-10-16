// Checkbox group for a closed multi-choice list (copied from the CM console,
// ui/src/components/FormControls.tsx). Values selected but not offered (e.g. a
// use case scope the partner may not ask for) stay visible, flagged.
import { useId } from "react";

export function CheckboxGroup({
  label,
  options,
  value,
  onChange,
  hint,
  error,
  flag = "not allowed — untick",
}: {
  label: string;
  options: string[];
  value: string[];
  onChange: (next: string[]) => void;
  hint?: React.ReactNode;
  error?: string | null;
  flag?: string;
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
              {bad && <span className="flag">{flag}</span>}
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
