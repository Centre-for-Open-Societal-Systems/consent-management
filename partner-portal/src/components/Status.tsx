import { statusLabel } from "../format";

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${status}`}>{statusLabel(status)}</span>;
}

export function ErrorNotice({ error, what }: { error: unknown; what: string }) {
  const msg = error instanceof Error ? error.message : "";
  return (
    <div className="notice notice-error">
      Could not load {what}.{msg ? ` ${msg}` : ""}
    </div>
  );
}
