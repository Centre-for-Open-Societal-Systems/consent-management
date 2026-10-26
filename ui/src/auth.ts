// Keycloak auth — mirrors the AWE UI pattern.
// Config comes from runtime /config.json (not build-time env) so the same
// image works across environments. When keycloak.url is empty (local dev),
// we mint an unsigned dev token carrying the admin role so the console is
// usable without a running Keycloak.
import Keycloak from "keycloak-js";

export interface AppConfig {
  apiBaseUrl: string;
  keycloak: { url: string; realm: string; clientId: string };
  adminRole: string;
  approverRole: string;
  // Farmer pages (/my, and /consent/<id> when consentLogin is off) log in with
  // national ID (eSignet) through the beneficiary API auth instead of Keycloak.
  beneficiaryAuth?: { enabled: boolean };
  // A consent request opened from its link (/consent/<id>) logs in with the
  // consent login: national ID for that one request only (separate token).
  consentLogin?: { enabled: boolean };
}

let config: AppConfig;
let keycloak: Keycloak | null = null;
let devMode = false;
let farmerMode = false;
// Which national-ID login this page uses: the beneficiary API auth (/my) or
// the consent login for one request (/consent/<id> opened from its link).
let farmerLoginKind: "bene" | "consent" = "bene";
let consentRequestId = "";
let farmerToken = "";
let farmerError = "";
const FARMER_TOKEN_KEY = "cm.farmer.token";
const CONSENT_TOKEN_KEY = "cm.consent-login.";

export function getConfig(): AppConfig {
  return config;
}

export async function loadConfig(): Promise<AppConfig> {
  const res = await fetch("/config.json", { cache: "no-store" });
  config = await res.json();
  return config;
}

// Unsigned JWT (dev only) — never trusted by the backend when Keycloak is set.
function makeDevToken(roles: string[]): string {
  const header = { alg: "none", typ: "JWT" };
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    sub: "dev-admin",
    preferred_username: "dev-admin",
    name: "Dev Admin",
    realm_access: { roles },
    iat: now,
    exp: now + 60 * 60 * 8,
  };
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64(header)}.${b64(payload)}.`;
}

export async function initAuth(): Promise<void> {
  await loadConfig();
  const kind = farmerLoginFor(window.location.pathname);
  if (kind) {
    farmerMode = true;
    farmerLoginKind = kind;
    await initFarmer();
    return;
  }
  if (!config.keycloak.url) {
    devMode = true;
    return;
  }
  keycloak = new Keycloak({
    url: config.keycloak.url,
    realm: config.keycloak.realm,
    clientId: config.keycloak.clientId,
  });
  await keycloak.init({
    onLoad: "login-required",
    checkLoginIframe: false,
    pkceMethod: "S256",
  });
}

export function getToken(): string {
  if (farmerMode) return farmerToken;
  if (devMode) return makeDevToken(devRoles());
  return keycloak?.token ?? "";
}

// In dev mode we grant both admin + approver so every surface is reachable.
function devRoles(): string[] {
  return [config.adminRole, config.approverRole].filter(Boolean);
}

export async function refreshToken(): Promise<void> {
  if (farmerMode || devMode || !keycloak) return;
  try {
    await keycloak.updateToken(30);
  } catch {
    keycloak.login();
  }
}

// Roles pulled from both realm_access and every resource_access.* client,
// matching how the backend resolves them.
export function getRoles(): string[] {
  if (devMode) return devRoles();
  if (!keycloak?.tokenParsed) return [];
  const parsed = keycloak.tokenParsed as {
    realm_access?: { roles?: string[] };
    resource_access?: Record<string, { roles?: string[] }>;
  };
  const roles = new Set<string>(parsed.realm_access?.roles ?? []);
  for (const client of Object.values(parsed.resource_access ?? {})) {
    for (const r of client.roles ?? []) roles.add(r);
  }
  return [...roles];
}

export function hasRole(role: string): boolean {
  return getRoles().includes(role);
}

export function isAdmin(): boolean {
  return hasRole(config.adminRole);
}

export function isApprover(): boolean {
  return hasRole(config.approverRole);
}

export function currentUser(): string {
  if (devMode) return "dev-admin";
  const parsed = keycloak?.tokenParsed as { preferred_username?: string; name?: string } | undefined;
  return parsed?.name ?? parsed?.preferred_username ?? "user";
}

export function logout(): void {
  if (devMode || !keycloak) {
    window.location.reload();
    return;
  }
  keycloak.logout({ redirectUri: window.location.origin });
}

export function isDevMode(): boolean {
  return devMode;
}

// ── Farmer logins (national ID, eSignet) ────────────────────────────────────
// Two separate logins, picked by the page the browser opened:
//   * beneficiary API auth (/my/*, and /consent/<id> when consentLogin is off):
//     /beneficiary/auth/login → eSignet → back with ?login=<one-time code> →
//     /beneficiary/auth/token. The token covers the farmer's requests and consents.
//   * consent login (/consent/<id> opened from its link, consentLogin on):
//     /consent-login/start?request_id=<id> → eSignet → back with ?login= →
//     /consent-login/token. The token is valid for that one request only.
// Tokens are kept in sessionStorage (gone when the tab closes).

export function isFarmerPage(pathname: string): boolean {
  return pathname.startsWith("/consent/") || pathname === "/my" || pathname.startsWith("/my/");
}

// The farmer login a page uses, or null for the Keycloak console.
function farmerLoginFor(pathname: string): "bene" | "consent" | null {
  if (!isFarmerPage(pathname)) return null;
  if (pathname.startsWith("/consent/") && config.consentLogin?.enabled) {
    consentRequestId = decodeURIComponent(pathname.slice("/consent/".length).split("/")[0]);
    return "consent";
  }
  return config.beneficiaryAuth?.enabled ? "bene" : null;
}

const BENE_AUTH = "/consent/v1/beneficiary/auth";
const CONSENT_LOGIN = "/consent/v1/consent-login";

function apiBase(): string {
  return config.apiBaseUrl.replace(/\/$/, "");
}

function tokenKey(): string {
  return farmerLoginKind === "consent" ? CONSENT_TOKEN_KEY + consentRequestId : FARMER_TOKEN_KEY;
}

function tokenClaims(token: string): { exp?: number; name?: string; subject_id_value?: string } {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    // UTF-8: names may be in Amharic.
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(part), (ch) => ch.charCodeAt(0))));
  } catch {
    return {};
  }
}

function storeFarmerToken(token: string): void {
  farmerToken = token;
  try {
    if (token) sessionStorage.setItem(tokenKey(), token);
    else sessionStorage.removeItem(tokenKey());
  } catch {
    /* storage blocked: the token lives for this page only */
  }
}

async function initFarmer(): Promise<void> {
  const url = new URL(window.location.href);
  const code = url.searchParams.get("login");
  farmerError = url.searchParams.get("login_error") ?? "";
  if (code !== null || url.searchParams.has("login_error")) {
    url.searchParams.delete("login");
    url.searchParams.delete("login_error");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  if (code) {
    const auth = farmerLoginKind === "consent" ? CONSENT_LOGIN : BENE_AUTH;
    const res = await fetch(`${apiBase()}${auth}/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok && body.access_token) storeFarmerToken(body.access_token);
    else farmerError = body.error ?? "Login failed. Please try again.";
    return;
  }
  let saved = "";
  try {
    saved = sessionStorage.getItem(tokenKey()) ?? "";
  } catch {
    /* storage blocked */
  }
  const exp = tokenClaims(saved).exp ?? 0;
  storeFarmerToken(exp * 1000 > Date.now() + 30_000 ? saved : "");
}

export function isFarmerMode(): boolean {
  return farmerMode;
}

// True on /consent/<id> opened from its link: the consent login for that one
// request (no /my pages with this login).
export function isConsentLogin(): boolean {
  return farmerMode && farmerLoginKind === "consent";
}

// Whether /my pages have their own (beneficiary API auth) login.
export function hasBeneficiaryAuth(): boolean {
  return !!config.beneficiaryAuth?.enabled;
}

export function farmerLoggedIn(): boolean {
  return !!farmerToken;
}

export function farmerLoginError(): string {
  return farmerError;
}

export function farmerIdentity(): { name?: string; id?: string } {
  const c = tokenClaims(farmerToken);
  return { name: c.name, id: c.subject_id_value };
}

export function farmerLogin(): void {
  const back = encodeURIComponent(window.location.href);
  if (farmerLoginKind === "consent") {
    const id = encodeURIComponent(consentRequestId);
    window.location.assign(`${apiBase()}${CONSENT_LOGIN}/start?request_id=${id}&return_url=${back}`);
    return;
  }
  window.location.assign(`${apiBase()}${BENE_AUTH}/login?return_url=${back}`);
}

export function farmerLogout(): void {
  storeFarmerToken("");
  window.location.assign(farmerLoginKind === "consent" ? window.location.pathname : "/my/consents");
}

// The token expired or was refused: log in again.
export function farmerSessionEnded(): void {
  storeFarmerToken("");
  window.location.reload();
}
