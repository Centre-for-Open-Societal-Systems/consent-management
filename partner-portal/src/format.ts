// Display helpers shared by the pages.
import type { Assurance, Purpose } from "./api/types";

export function purposeLabel(purpose: Purpose | string | null | undefined): string {
  if (!purpose) return "—";
  if (typeof purpose === "string") return purpose;
  return (
    (purpose.name as string) ||
    (purpose.description as string) ||
    (purpose.code as string) ||
    "—"
  );
}

export function purposeCode(purpose: Purpose | string | null | undefined): string {
  if (!purpose) return "";
  return typeof purpose === "string" ? purpose : ((purpose.code as string) ?? "");
}

export function formatDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function formatDateTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatBytes(n?: number | null): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

export function statusLabel(status: string): string {
  return status.replace(/_/g, " ");
}

const EVIDENCE_WORDS: Record<string, string> = {
  signed_form: "signed form",
  other: "supporting document",
};

// The consent's assurance in plain words, e.g.
// "Assisted, signed form verified by jane.doe on 3 Oct 2026".
export function assuranceText(a?: Assurance | null): string {
  if (!a || !a.method) return "Not recorded";
  const method = a.method.charAt(0).toUpperCase() + a.method.slice(1).replace(/_/g, " ");
  const parts = [method];
  const evidence = (a.evidence ?? []).map((e) => EVIDENCE_WORDS[e] ?? e.replace(/_/g, " "));
  if (evidence.length) {
    let ev = evidence.join(" and ");
    if (a.verified_by || a.verified_at) {
      ev += " verified";
      if (a.verified_by) ev += ` by ${a.verified_by}`;
      if (a.verified_at) ev += ` on ${formatDate(a.verified_at)}`;
    }
    parts.push(ev);
  }
  if (a.subject_authenticated) parts.push("subject authenticated with national ID");
  return parts.join(", ");
}

// Save a blob as a file (anchor download).
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// Open a blob (image/PDF) in a new tab. The tab is opened synchronously (before
// the download) so popup blockers allow it; pass it in from the click handler.
export function openBlobIn(win: Window | null, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  if (win) win.location.href = url;
  else window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
