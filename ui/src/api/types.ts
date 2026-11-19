// Types mirroring the Consent Manager backend schemas (/consent/v1/*).
// Kept intentionally close to the Pydantic models so the client stays honest.

// ── Decision log (admin status/audit view) ───────────────────────────────
export interface DecisionLog {
  id: string;
  // The partner's audience (e.g. bank-a), as in Partner Management.
  partner_id?: string | null;
  consent_id?: string | null;
  object_jti?: string | null;
  data_controller?: string | null;
  decision: "permit" | "deny";
  reason_code: string;
  detail?: string | null;
  policy_version?: number | null;
  // The PM policy applied: PM's id of that version, and its kind:key
  // ("data_controller:farmer-registry"; "template:loan-profile@1" when a token
  // presenter sent a consent template's terms — no PM policy).
  policy_id?: string | null;
  policy_ref?: string | null;
  created_at: string;
}

// ── Consents (staff view) ────────────────────────────────────────────────
// `purpose` is a structured object; the registry decides its shape, so we
// render defensively.
export type ConsentStatus = "active" | "revoked" | "expired";

export interface Purpose {
  code?: string;
  name?: string;
  description?: string;
  [k: string]: unknown;
}

// One grant per data controller (registry) in a consent.
export interface Grant {
  data_controller: string;
  data_scopes: string[];
}

export interface RevokeResponse {
  consent_id: string;
  status: string;
  revoked_at: string;
}

// GET /consent/v1/consents/{id} (staff api).
export interface StaffConsent {
  consent_id: string;
  status: ConsentStatus;
  partner_id: string;
  source: string;
  subject_id: { type: string; value: string };
  subject_identifiers?: { type: string; value: string }[];
  purpose: Purpose;
  grants: {
    data_controller?: string | null;
    data_scopes: string[];
    effective_data_scopes: string[];
    policy_version?: number | null;
    policy_id?: string | null;
    policy_ref?: string | null;
  }[];
  valid_from: string;
  valid_until: string;
  assurance?: Record<string, unknown> | null;
  // The consent template it was made from (a label).
  template?: string | null;
  // The assisted request it came from: its evidence downloads from there.
  consent_request_id?: string | null;
  created_at: string;
  revoked_at?: string | null;
  receipt_id?: string | null;
  evidence: VerificationEvidence[];
  revocations: {
    originated_by: string;
    reason?: string | null;
    revoked_by?: string | null;
    created_at: string;
  }[];
}

// ── Consent verifications (assisted consent, staff check of the signed form) ──
// /consent/v1/verifications — a partner user raised the request in the partner
// portal and uploaded the evidence; staff approve (creates the consent) or reject.
export type VerificationStatus = "pending_verification" | "approved" | "rejected";

export interface VerificationEvidence {
  id: string;
  kind: "signed_form" | "other" | string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  uploaded_by?: string | null;
  uploaded_at: string;
}

export interface VerificationRequest {
  id: string;
  status: string;
  method?: string | null;
  // The consent template the request was made from (a label).
  template?: string | null;
  subject_id: { type: string; value: string };
  // Other identifiers of the subject (e.g. the Farmer ID); the approver checks
  // them in the registry.
  subject_identifiers?: { type: string; value: string }[];
  purpose: Purpose | string;
  grants: Grant[];
  valid_from?: string | null;
  valid_until?: string | null;
  created_by?: string | null;
  created_at: string;
  submitted_at?: string | null;
  verified_by?: string | null;
  verified_at?: string | null;
  verification_note?: string | null;
  consent_id?: string | null;
  partner_audience?: string | null;
  // The subject's latest national-ID login (eSignet), if the CM has it on.
  subject_authentication?: {
    status: string;
    error?: string | null;
    provider?: string | null;
    method?: string | null;
    acr?: string | null;
    authenticated_at?: string | null;
    started_by?: string | null;
    // As the identity provider returned them at this login, not the registry's.
    demographics?: Record<string, unknown> | null;
    demographics_source?: string | null;
    previous_success?: { authenticated_at?: string | null; method?: string | null } | null;
  } | null;
  evidence?: VerificationEvidence[];
}

export interface OffsetPage<T> {
  total: number;
  items: T[];
}
