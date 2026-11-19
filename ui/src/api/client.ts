// Thin typed fetch wrapper over the Consent Manager REST API.
// Every path is rooted at /consent/v1 (the backend router prefix) on the STAFF
// api; every call carries the staff user's Keycloak bearer token.
import { getConfig, getToken, refreshToken } from "../auth";
import type {
  DecisionLog,
  OffsetPage,
  RevokeResponse,
  StaffConsent,
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
  // ── Decisions (admin status/audit view) ────────────────────────────
  // `partner_id` is the partner's audience (e.g. bank-a).
  listDecisions: (params: { partner_id?: string; decision?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.partner_id) q.set("partner_id", params.partner_id);
    if (params.decision) q.set("decision", params.decision);
    q.set("limit", String(params.limit ?? 50));
    return request<DecisionLog[]>(`${V1}/decisions?${q.toString()}`);
  },

  // ── Consents (staff view; revoke on the subject's request) ─────────
  getConsent: (id: string) => request<StaffConsent>(`${V1}/consents/${encodeURIComponent(id)}`),
  getConsentReceipt: (id: string) =>
    request<Record<string, unknown>>(`${V1}/consents/${encodeURIComponent(id)}/receipt`),
  revokeConsent: (id: string, reason: string) =>
    request<RevokeResponse>(`${V1}/consents/${encodeURIComponent(id)}/revocations`, {
      method: "POST",
      body: JSON.stringify({ reason }),
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
  approveVerification: (id: string, note?: string) =>
    request<VerificationRequest>(`${V1}/verifications/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body: JSON.stringify(note ? { note } : {}),
    }),
  rejectVerification: (id: string, note: string) =>
    request<VerificationRequest>(`${V1}/verifications/${encodeURIComponent(id)}/reject`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
};
