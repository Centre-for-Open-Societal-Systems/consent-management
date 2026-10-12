// Thin typed fetch wrapper over the Consent Manager REST API.
// Every path is rooted at /consent/v1 (the backend router prefix). Admin calls
// carry the Keycloak bearer token; the subject/consent-request flow reuses the
// authenticated subject's token.
import { getConfig, getToken, refreshToken } from "../auth";
import type {
  Artefact,
  AweTask,
  ConsentRequest,
  DecisionLog,
  Grant,
  PagedTasks,
  Paginated,
  Partner,
  PartnerCreate,
  PartnerPolicy,
  PartnerUpdate,
  PolicyMeta,
  PolicyUpsert,
  OffsetPage,
  RevokeResponse,
  VerificationRequest,
} from "./types";

export class ApiError extends Error {
  status: number;
  reason?: string;
  constructor(status: number, message: string, reason?: string) {
    super(message);
    this.status = status;
    this.reason = reason;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  await refreshToken();
  const base = getConfig().apiBaseUrl.replace(/\/$/, "");
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...(init.headers ?? {}),
    },
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const body = text ? JSON.parse(text) : undefined;
  if (!res.ok) {
    // The platform's validation errors arrive as 400 {"errors": [{code, message}]}.
    const listed = Array.isArray(body?.errors)
      ? body.errors.map((e: { message?: string }) => e.message).filter(Boolean).join("; ")
      : undefined;
    const detail = listed || (body?.detail ?? body?.error ?? body?.message ?? res.statusText);
    const reason = body?.reason_code ?? body?.reason;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail), reason);
  }
  return body as T;
}

// File download (evidence): the raw body as a Blob, with the bearer token.
async function requestBlob(path: string): Promise<Blob> {
  await refreshToken();
  const base = getConfig().apiBaseUrl.replace(/\/$/, "");
  const res = await fetch(`${base}${path}`, { headers: { Authorization: `Bearer ${getToken()}` } });
  if (!res.ok) {
    const text = await res.text();
    let detail: string = res.statusText;
    try {
      const body = JSON.parse(text);
      detail = body?.detail ?? body?.error ?? body?.message ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.blob();
}

const V1 = "/consent/v1";

export const api = {
  // Allowed values for the binding/policy forms (staff).
  getMeta: () => request<PolicyMeta>(`${V1}/meta`),

  // ── Partners (admin) ───────────────────────────────────────────────
  // `audience` narrows to one partner's bindings (one per controller).
  listPartners: (params: { audience?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.audience) q.set("audience", params.audience);
    const qs = q.toString();
    return request<Partner[]>(`${V1}/partners${qs ? `?${qs}` : ""}`);
  },
  getPartner: (id: string) => request<Partner>(`${V1}/partners/${id}`),
  createPartner: (data: PartnerCreate) =>
    request<Partner>(`${V1}/partners`, { method: "POST", body: JSON.stringify(data) }),
  updatePartner: (id: string, data: PartnerUpdate) =>
    request<Partner>(`${V1}/partners/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

  // Policy (versioned). putPolicy returns the new version — `pending` if it
  // widened access and AWE approval is enabled, else `active`.
  getPolicy: (id: string) => request<PartnerPolicy>(`${V1}/partners/${id}/policy`),
  listPolicies: (id: string) => request<PartnerPolicy[]>(`${V1}/partners/${id}/policies`),
  putPolicy: (id: string, data: PolicyUpsert) =>
    request<PartnerPolicy>(`${V1}/partners/${id}/policy`, { method: "PUT", body: JSON.stringify(data) }),
  // Copy a failed / stale / rejected version into a new version and save it
  // again (re-evaluated against the current active policy).
  resubmitPolicy: (id: string, version: number) =>
    request<PartnerPolicy>(`${V1}/partners/${id}/policies/${version}/resubmit`, { method: "POST" }),

  // ── Decisions (admin status/audit view) ────────────────────────────
  listDecisions: (params: { partner_id?: string; decision?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.partner_id) q.set("partner_id", params.partner_id);
    if (params.decision) q.set("decision", params.decision);
    q.set("limit", String(params.limit ?? 50));
    return request<DecisionLog[]>(`${V1}/decisions?${q.toString()}`);
  },

  // ── AWE approvals (approver inbox — proxied to AWE with the approver JWT) ──
  // status "actionable" (default) = open or claimed tasks.
  listMyTasks: (params: { status?: string; page?: number; page_size?: number } = {}) => {
    const q = new URLSearchParams();
    q.set("status", params.status ?? "actionable");
    q.set("page", String(params.page ?? 1));
    q.set("page_size", String(params.page_size ?? 25));
    return request<PagedTasks>(`${V1}/awe/tasks?${q.toString()}`);
  },
  submitTaskDecision: (taskId: string, action: "approve" | "reject" | "abstain", comment?: string) =>
    request<unknown>(`${V1}/awe/tasks/${taskId}/decision`, {
      method: "POST",
      body: JSON.stringify({ action, comment: comment ?? null }),
    }),
  claimTask: (taskId: string) =>
    request<AweTask>(`${V1}/awe/tasks/${taskId}/claim`, { method: "POST" }),
  getAweRequest: (requestId: string) => request<unknown>(`${V1}/awe/requests/${requestId}`),

  // ── Subject rights (transparency dashboard) ────────────────────────
  myConsents: (status?: string) =>
    request<Paginated<Artefact>>(
      `${V1}/my/consents${status ? `?status=${encodeURIComponent(status)}` : ""}`
    ).then((p) => p.items),
  myConsent: (id: string) => request<Artefact>(`${V1}/my/consents/${id}`),
  revokeMyConsent: (id: string) =>
    request<RevokeResponse>(`${V1}/my/consents/${id}/revoke`, {
      method: "POST",
      body: JSON.stringify({ originated_by: "subject" }),
    }),

  // ── Consent request (originate / redirect flow) ────────────────────
  getConsentRequest: (id: string) => request<ConsentRequest>(`${V1}/consent-requests/${id}`),
  // The subject authenticates by presenting their IdP id_token, then approves
  // with the scopes they agree to share.
  authenticateConsentRequest: (id: string, idToken: string) =>
    request<{ token_validated: boolean }>(`${V1}/consent-requests/${id}/authenticate`, {
      method: "POST",
      body: JSON.stringify({ id_token: idToken }),
    }),
  approveConsentRequest: (id: string, grantedScopes: string[]) =>
    request<Artefact>(`${V1}/consent-requests/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ granted_scopes: grantedScopes }),
    }),
  // For a request with grants: approve each controller's scopes; a controller
  // left out is declined.
  approveConsentRequestGrants: (id: string, grants: Grant[]) =>
    request<Artefact>(`${V1}/consent-requests/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ grants }),
    }),
  denyConsentRequest: (id: string, reason?: string) =>
    request<ConsentRequest>(`${V1}/consent-requests/${id}/deny`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),

  // ── Consent verifications (assisted consent; approver acts, admin reads) ──
  listVerifications: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return request<OffsetPage<VerificationRequest>>(`${V1}/verifications?${q.toString()}`);
  },
  getVerification: (id: string) =>
    request<VerificationRequest>(`${V1}/verifications/${encodeURIComponent(id)}`),
  getVerificationEvidence: (id: string, evidenceId: string) =>
    requestBlob(`${V1}/verifications/${encodeURIComponent(id)}/evidence/${encodeURIComponent(evidenceId)}`),
  approveVerification: (id: string, note?: string, identifiersChecked = false) =>
    request<VerificationRequest>(`${V1}/verifications/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body: JSON.stringify({ ...(note ? { note } : {}), identifiers_checked: identifiersChecked }),
    }),
  rejectVerification: (id: string, note: string) =>
    request<VerificationRequest>(`${V1}/verifications/${encodeURIComponent(id)}/reject`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
};
