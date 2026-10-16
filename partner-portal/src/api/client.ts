// Thin typed fetch wrapper over the CM partner-portal API
// (/consent/v1/partner-portal/*), carrying the partner-realm Keycloak token.
// 401 → sign in again; other errors surface as ApiError (403 = the user is
// not set up as a partner user, handled by the layout).
import { getConfig, getToken, login, refreshToken } from "../auth";
import type {
  Binding,
  Consent,
  ConsentRequest,
  ConsentRequestCreate,
  Evidence,
  Me,
  Page,
  RequestSettings,
  SubjectAuth,
  SubjectAuthConfig,
  SubjectAuthStart,
  UseCase,
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

function errorMessage(body: unknown, fallback: string): { message: string; reason?: string } {
  const b = body as Record<string, unknown> | undefined;
  // Validation errors: 400 {"errors": [{code, message}]}; FastAPI: {"detail": …};
  // composite: {"error": {code, message}}.
  const listed = Array.isArray(b?.errors)
    ? (b!.errors as { message?: string }[]).map((e) => e.message).filter(Boolean).join("; ")
    : undefined;
  const err = b?.error as { message?: string; code?: string } | string | undefined;
  const detail =
    listed ||
    b?.detail ||
    (typeof err === "object" ? err?.message : err) ||
    b?.message ||
    fallback;
  const reason = (b?.reason_code ?? b?.reason ?? (typeof err === "object" ? err?.code : undefined)) as
    | string
    | undefined;
  return { message: typeof detail === "string" ? detail : JSON.stringify(detail), reason };
}

async function send(path: string, init: RequestInit = {}): Promise<Response> {
  await refreshToken();
  const base = getConfig().apiBaseUrl.replace(/\/$/, "");
  const headers: Record<string, string> = {
    Authorization: `Bearer ${getToken()}`,
    ...((init.headers as Record<string, string>) ?? {}),
  };
  // JSON bodies only; FormData sets its own multipart boundary.
  if (typeof init.body === "string") headers["Content-Type"] = "application/json";
  const res = await fetch(`${base}${path}`, { ...init, headers });
  if (res.status === 401) {
    login();
    throw new ApiError(401, "Your session has expired. Signing you in again…");
  }
  if (!res.ok) {
    const text = await res.text();
    let body: unknown;
    try {
      body = text ? JSON.parse(text) : undefined;
    } catch {
      body = undefined;
    }
    const { message, reason } = errorMessage(body, res.statusText || `HTTP ${res.status}`);
    throw new ApiError(res.status, message, reason);
  }
  return res;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await send(path, init);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

async function requestBlob(path: string): Promise<{ blob: Blob; filename?: string }> {
  const res = await send(path);
  const cd = res.headers.get("Content-Disposition") ?? "";
  const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
  const filename = m ? decodeURIComponent(m[1] ?? m[2]) : undefined;
  return { blob: await res.blob(), filename };
}

function qs(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}

const P = "/consent/v1/partner-portal";

export interface ListParams {
  status?: string;
  limit?: number;
  offset?: number;
}

export const api = {
  me: () => request<Me>(`${P}/me`),
  bindings: () => request<Binding[]>(`${P}/bindings`),

  // ── Consent requests ───────────────────────────────────────────────
  listRequests: (p: ListParams = {}) =>
    request<Page<ConsentRequest>>(`${P}/consent-requests${qs({ ...p })}`),
  getRequest: (id: string) => request<ConsentRequest>(`${P}/consent-requests/${encodeURIComponent(id)}`),
  createRequest: (data: ConsentRequestCreate) =>
    request<ConsentRequest>(`${P}/consent-requests`, { method: "POST", body: JSON.stringify(data) }),
  uploadEvidence: (id: string, file: File, kind = "signed_form") => {
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    return request<Evidence>(`${P}/consent-requests/${encodeURIComponent(id)}/evidence`, {
      method: "POST",
      body: form,
    });
  },
  downloadEvidence: (id: string, evidenceId: string) =>
    requestBlob(
      `${P}/consent-requests/${encodeURIComponent(id)}/evidence/${encodeURIComponent(evidenceId)}`
    ),
  deleteEvidence: (id: string, evidenceId: string) =>
    request<void>(
      `${P}/consent-requests/${encodeURIComponent(id)}/evidence/${encodeURIComponent(evidenceId)}`,
      { method: "DELETE" }
    ),
  submitRequest: (id: string) =>
    request<ConsentRequest>(`${P}/consent-requests/${encodeURIComponent(id)}/submit`, { method: "POST" }),
  cancelRequest: (id: string) =>
    request<ConsentRequest>(`${P}/consent-requests/${encodeURIComponent(id)}/cancel`, { method: "POST" }),

  // ── The subject's national-ID login (eSignet) ─────────────────────
  subjectAuthConfig: () => request<SubjectAuthConfig>(`${P}/subject-auth`),
  requestSettings: () => request<RequestSettings>(`${P}/request-settings`),
  startSubjectAuth: (id: string) =>
    request<SubjectAuthStart>(`${P}/consent-requests/${encodeURIComponent(id)}/subject-auth`, {
      method: "POST",
    }),
  subjectAuthStatus: (id: string) =>
    request<SubjectAuth>(`${P}/consent-requests/${encodeURIComponent(id)}/subject-auth`),

  // ── Consents ───────────────────────────────────────────────────────
  listConsents: (p: ListParams = {}) => request<Page<Consent>>(`${P}/consents${qs({ ...p })}`),
  getConsent: (id: string) => request<Consent>(`${P}/consents/${encodeURIComponent(id)}`),
  receipt: (id: string) => requestBlob(`${P}/consents/${encodeURIComponent(id)}/receipt`),
  // The partner's copy for its records: a ZIP with the receipt, the signed form
  // and the CM's signed statement tying them together.
  record: (id: string) => requestBlob(`${P}/consents/${encodeURIComponent(id)}/record`),
};

// ── Composite (public, no auth) ──────────────────────────────────────
export function compositeUrl(): string {
  return (getConfig().compositeUrl ?? "").replace(/\/$/, "");
}

export async function listUseCases(): Promise<UseCase[]> {
  const res = await fetch(`${compositeUrl()}/composite/v1/use-cases`);
  const text = await res.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    body = undefined;
  }
  if (!res.ok) {
    const { message, reason } = errorMessage(body, res.statusText || `HTTP ${res.status}`);
    throw new ApiError(res.status, message, reason);
  }
  return ((body as { use_cases?: UseCase[] })?.use_cases ?? []) as UseCase[];
}
