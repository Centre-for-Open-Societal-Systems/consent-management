import { getConfig } from "../auth";

// A partner's PM id from its audience, as the CM derives it: bank-a → PARTNER_BANK_A.
export function pmPartnerId(audience: string): string {
  return "PARTNER_" + audience.trim().toUpperCase().replace(/-/g, "_");
}

// "data_controller:farmer-registry" → "Registry farmer-registry";
// "template:loan-profile@1" → "Consent template loan-profile@1" (terms of a
// consent template, sent by a token presenter: no Partner Management policy).
export function policyLabel(ref?: string | null): string | null {
  if (!ref) return null;
  const i = ref.indexOf(":");
  const kind = i < 0 ? "" : ref.slice(0, i);
  const key = i < 0 ? ref : ref.slice(i + 1);
  if (kind === "template") return `Consent template ${key}`;
  if (kind === "data_controller") return `Registry ${key}`;
  return key;
}

// The PM policy a decision or consent used: "Registry farmer-registry · v1",
// linking to the partner's page in Partner Management when its URL is set (not
// for a consent template, which is not a PM policy).
export function PolicyRef({
  partnerId,
  policyRef,
  policyVersion,
  policyId,
}: {
  partnerId?: string | null;
  policyRef?: string | null;
  policyVersion?: number | null;
  policyId?: string | null;
}) {
  const label = policyLabel(policyRef);
  const version = policyVersion != null ? `v${policyVersion}` : null;
  const text = [label, version].filter(Boolean).join(" · ");
  if (!text) return <>—</>;
  const pmUrl = getConfig().partnerManagementUiUrl?.trim().replace(/\/+$/, "");
  const title = policyId ? `Partner Management policy ${policyId}` : undefined;
  if (pmUrl && partnerId && !policyRef?.startsWith("template:")) {
    return (
      <a
        href={`${pmUrl}/partners/${encodeURIComponent(pmPartnerId(partnerId))}`}
        target="_blank"
        rel="noreferrer"
        title={title}
      >
        {text}
      </a>
    );
  }
  return <span title={title}>{text}</span>;
}
