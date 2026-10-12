// Types mirroring the Consent Manager backend schemas (/consent/v1/*).
// Kept intentionally close to the Pydantic models so the client stays honest.

export type PartnerStatus = "active" | "suspended";

// A "partner" in CM is a POLICY BINDING: it binds a Partner-Management partner
// (partner_mgmt_id) to a controller + data-share policy. Identity/keys live in
// PM; `name` here is just a display label. One partner (audience) may have
// several bindings — one per controller, each with its own policy.
export interface Partner {
  id: string;
  name?: string | null;
  audience: string;
  controller_id: string;
  partner_mgmt_id?: string | null;
  status: PartnerStatus;
  created_at?: string;
}

export interface PartnerCreate {
  partner_mgmt_id?: string | null;
  audience: string;
  controller_id: string;
  name?: string | null;
}

export interface PartnerUpdate {
  name?: string;
  status?: PartnerStatus;
  partner_mgmt_id?: string | null;
}

export type FetchType = "oneshot" | "periodic";

// A versioned data-share policy's lifecycle: `pending` awaits AWE approval (at
// most one per binding); `active` is in force; `superseded` / `rejected` are
// historical. `failed`: the submission to AWE failed. `stale`: approved, but the
// active policy changed after it was submitted, so it was not applied.
// failed / stale / rejected versions can be resubmitted.
export type PolicyStatus = "pending" | "active" | "superseded" | "rejected" | "failed" | "stale";

export const RESUBMITTABLE: PolicyStatus[] = ["failed", "stale", "rejected"];

// Durations are ISO-8601 duration strings (e.g. "P1Y", "P30D", "PT12H"),
// matching the backend. max_fetch_frequency is likewise a string (e.g. "P1D").
export interface PolicyUpsert {
  allowed_data_scopes: string[];
  allowed_purposes: string[];
  allowed_subject_id_types: string[];
  allowed_signing_algs: string[];
  max_validity_duration?: string | null;
  fetch_type: FetchType;
  max_fetch_frequency?: string | null;
  data_life?: string | null;
}

export interface PartnerPolicy extends PolicyUpsert {
  id: string;
  partner_id: string;
  version: number;
  status: PolicyStatus;
  awe_request_id?: string | null;
  // Version that was active when this one was created (0 = none).
  base_version?: number | null;
  // Why the version ended failed / stale / rejected.
  status_reason?: string | null;
  effective_from?: string | null;
  // Values the current rules reject (a version saved before them). Such a
  // version still loads; it must be fixed before it can be saved again.
  issues?: string[];
}

// GET /consent/v1/meta — the allowed values the API validates against, so the
// forms never hard-code them. `known_*` are open sets: values already in use,
// offered only as suggestions.
export interface PolicyMeta {
  signing_algorithms: string[];
  fetch_types: string[];
  partner_statuses: string[];
  known_controller_ids: string[];
  known_data_scopes: string[];
  known_purposes: string[];
  known_subject_id_types: string[];
}

// ── AWE approval tasks (approver inbox — proxied to AWE) ──────────────────
export interface AweTask {
  id: string;
  request_id: string;
  stage_order: number;
  assignee: string;
  status: string; // open | claimed | completed | ...
  artifact_type?: string | null;
  artifact_id?: string | null;
  policy_key?: string | null;
  context?: Record<string, unknown> | null;
  created_at: string;
  due_at?: string | null;
}

export interface PagedTasks {
  items: AweTask[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
  // false when the CM's AWE approvals are off (no inbox; policies take effect at once).
  awe_enabled?: boolean;
}

// ── Decision log (admin status/audit view) ───────────────────────────────
export interface DecisionLog {
  id: string;
  partner_id?: string | null;
  consent_id?: string | null;
  object_jti?: string | null;
  data_controller?: string | null;
  decision: "permit" | "deny";
  reason_code: string;
  detail?: string | null;
  policy_version?: number | null;
  created_at: string;
}

// ── Subject rights (transparency dashboard) ──────────────────────────────
// Backed by ArtefactResponse. `purpose` is a structured object; the registry
// decides its shape, so we render defensively.
export type ConsentStatus = "active" | "revoked" | "expired" | "pending";

export interface Purpose {
  code?: string;
  name?: string;
  description?: string;
  [k: string]: unknown;
}

export interface Artefact {
  id: string;
  consent_id?: string | null;
  subject_id_type: string;
  subject_id_value: string;
  partner_id: string;
  controller_id?: string | null;
  purpose: Purpose;
  effective_data_scopes: string[];
  grants?: ArtefactGrant[] | null;
  status: ConsentStatus;
  source: string;
  valid_from: string;
  valid_until: string;
  created_at: string;
  revoked_at?: string | null;
}

// One grant per data controller (registry) in a consent.
export interface Grant {
  data_controller: string;
  data_scopes: string[];
}

export interface ArtefactGrant extends Grant {
  effective_data_scopes: string[];
  granted_scopes?: string[];
  policy_version?: number | null;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

export interface RevokeResponse {
  consent_id: string;
  status: string;
  revoked_at: string;
}

// ── Consent-request (originate / redirect flow) ──────────────────────────
export interface ConsentRequest {
  id: string;
  subject_id_type: string;
  subject_id_value: string;
  partner_id: string;
  controller_id?: string | null;
  purpose: Purpose;
  requested_scopes: string[];
  // Present when the request spans several controllers (one grant each).
  grants?: Grant[] | null;
  status: string; // created | authenticated | approved | denied | expired
  valid_from?: string | null;
  valid_until?: string | null;
  created_at: string;
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
  use_case?: string | null;
  subject_id: { type: string; value: string };
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
  evidence?: VerificationEvidence[];
}

export interface OffsetPage<T> {
  total: number;
  items: T[];
}
