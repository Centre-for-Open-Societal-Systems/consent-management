// Types for the Consent Manager partner-portal API (/consent/v1/partner-portal/*).
// Shapes follow the scenario-1 contract.

export interface Me {
  username: string;
  name?: string | null;
  partner_id: string;
  roles: string[];
}

// One active scope policy of this partner (set in Partner Management): what it
// may ask for from one registry.
export interface ScopePolicy {
  data_controller: string;
  allowed_data_scopes: string[];
  allowed_purposes: string[];
  allowed_subject_id_types: string[];
  // ISO-8601 duration (e.g. "P1Y"); null = no limit beyond the default.
  max_validity_duration: string | null;
  policy_version: number;
  // Consents may be given by assisted confirmation (scenario 0).
  allow_assisted_confirmation?: boolean;
}

// What a consent from a template grants from one registry (data_scopes =
// required + optional).
export interface TemplateGrant {
  data_controller: string;
  data_scopes: string[];
  required_scopes: string[];
  optional_scopes: string[];
}

// A consent template offered to this partner (at the Exchange), from the
// Consent Manager's template source: its purpose, data per registry and
// limits. available=false: no consent can be made from it now (see detail).
export interface ConsentTemplate {
  code: string; // e.g. loan-profile@1
  title?: string | null;
  description?: string | null;
  purpose?: string | null;
  grants: TemplateGrant[];
  // Limits on a consent made from it; empty / null = none.
  allowed_subject_id_types: string[];
  max_validity_duration: string | null;
  allow_partner_consents: boolean;
  allow_assisted_confirmation?: boolean;
  available?: boolean;
  detail?: string | null;
}

// GET /access — what the partner may ask for. consent_by "template" (at the
// Exchange): a request names one of the templates. "scopes": a request names
// registries and data, within scope_policies.
export type ConsentBy = "template" | "scopes";

export interface PartnerAccess {
  consent_by: ConsentBy;
  scope_policies: ScopePolicy[];
  templates: ConsentTemplate[];
}

export interface SubjectId {
  type: string;
  value: string;
}

// The registry decides the purpose's shape beyond `code`, so render defensively.
export interface Purpose {
  code?: string;
  name?: string;
  description?: string;
  [k: string]: unknown;
}

export interface Grant {
  data_controller: string;
  data_scopes: string[];
}

export type RequestStatus =
  | "pending"
  | "pending_verification"
  | "approved"
  | "rejected"
  | "cancelled"
  | "denied"
  | "expired";

export const REQUEST_STATUSES: RequestStatus[] = [
  "pending",
  "pending_verification",
  "approved",
  "rejected",
  "cancelled",
  "denied",
  "expired",
];

export interface Evidence {
  id: string;
  kind: "signed_form" | "other" | string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  uploaded_by?: string | null;
  uploaded_at: string;
}

export interface ConsentRequest {
  id: string;
  status: RequestStatus | string;
  method?: string | null;
  // The consent template it was made from (a label).
  template?: string | null;
  subject_id: SubjectId;
  // Other identifiers of the subject, e.g. the Farmer ID.
  subject_identifiers?: SubjectId[];
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
  // The subject's latest national-ID login (null: none started).
  subject_authentication?: SubjectAuth | null;
  // Present on GET /consent-requests/{id}.
  evidence?: Evidence[];
}

export interface SubjectAuth {
  authentication_id: string;
  status: "pending" | "processing" | "success" | "failed" | "expired" | string;
  error?: string | null;
  started_by?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  provider?: string | null;
  method?: string | null;
  acr?: string | null;
  authenticated_at?: string | null;
  // As the identity provider returned them at this login (e.g. the name as per
  // the national ID), not the registry's record.
  demographics?: Record<string, unknown> | null;
  demographics_source?: string | null;
  // A later attempt failed or is running, but an earlier one succeeded.
  previous_success?: { authenticated_at?: string | null; method?: string | null } | null;
}

export interface SubjectAuthConfig {
  enabled: boolean;
  provider_name?: string | null;
  id_types: string[];
}

// A Master Data code list; a request must use one of its codes.
export interface CodeList {
  list_code: string;
  values: { code: string; label: string }[];
}

export interface RequestSettings {
  consent_by: ConsentBy;
  required_subject_identifiers: string[];
  subject_auth: SubjectAuthConfig;
  // Assisted confirmation (scenario 0) is on for this install; the partner's
  // policy or consent template must also allow it.
  assisted_confirmation?: boolean;
  // null: free text (no list in Master Data, or Master Data unreachable).
  purposes?: CodeList | null;
  subject_id_types?: CodeList | null;
}

export interface SubjectAuthStart {
  authentication_id: string;
  authorization_url: string;
  provider_name: string;
  expires_at: string;
}

// With grants: purpose and grants required, template a label. At the Exchange
// (consent_by "template"): template, and optionally grants chosen within its
// scopes (required ones included) — the purpose is the template's.
export interface ConsentRequestCreate {
  subject_id: SubjectId;
  subject_identifiers?: SubjectId[];
  purpose?: { code: string };
  grants?: Grant[];
  validity?: { valid_from?: string; valid_until?: string };
  template?: string;
}

export interface Page<T> {
  total: number;
  items: T[];
}

export type ConsentStatus = "active" | "revoked" | "expired";
export const CONSENT_STATUSES: ConsentStatus[] = ["active", "revoked", "expired"];

export interface ConsentGrant extends Grant {
  effective_data_scopes: string[];
  // The policy the grant was made under (kind:key, e.g.
  // "data_controller:farmer-registry", or "template:loan-profile@1" for a grant
  // made from a consent template), its version and PM's id of that version.
  policy_version?: number | null;
  policy_id?: string | null;
  policy_ref?: string | null;
}

// How the subject's consent was confirmed, e.g.
// {"method":"assisted","evidence":["signed_form"],"verified_by":"…","verified_at":"…","subject_authenticated":false}
// Assisted confirmation (scenario 0): the exact statement the person agrees to
// after the national-ID login, and the consent it makes.
export interface ConfirmationTerms {
  statement: string;
  partner_id: string;
  purpose: Record<string, unknown>;
  grants: { data_controller: string; data_scopes: string[] }[];
  valid_from: string;
  valid_until: string;
}

export interface Assurance {
  method?: string;
  evidence?: string[];
  verified_by?: string;
  verified_at?: string;
  subject_authenticated?: boolean;
  [k: string]: unknown;
}

export interface Consent {
  consent_id: string;
  status: ConsentStatus | string;
  subject_id: SubjectId;
  subject_identifiers?: SubjectId[];
  purpose: Purpose | string;
  grants: ConsentGrant[];
  valid_from?: string | null;
  valid_until?: string | null;
  assurance?: Assurance | null;
  // The consent template it was made from (a label).
  template?: string | null;
  consent_request_id?: string | null;
  created_at: string;
  revoked_at?: string | null;
}
