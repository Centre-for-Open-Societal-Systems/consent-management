// Keycloak auth — same pattern as the CM console (ui/src/auth.ts), against the
// `partner` realm and the public client `consent-partner-portal` (PKCE).
// Config comes from runtime /config.json (not build-time env) so the same image
// works across environments. When keycloak.url is empty (local dev) we mint an
// unsigned dev token carrying a partner role and partner_id; a backend with
// partner-realm verification configured will reject it.
import Keycloak from "keycloak-js";

export interface AppConfig {
  apiBaseUrl: string;
  // Optional: the use-case composite's base URL (e.g. https://composite.example.org).
  // Empty → the "Use cases" page is hidden.
  compositeUrl?: string;
  keycloak: { url: string; realm: string; clientId: string };
}

let config: AppConfig;
let keycloak: Keycloak | null = null;
let devMode = false;

export function getConfig(): AppConfig {
  return config;
}

export async function loadConfig(): Promise<AppConfig> {
  const res = await fetch("/config.json", { cache: "no-store" });
  config = await res.json();
  return config;
}

// Unsigned JWT (dev only) — never trusted by the backend when Keycloak is set.
function makeDevToken(): string {
  const header = { alg: "none", typ: "JWT" };
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    sub: "dev-partner-operator",
    preferred_username: "dev-partner-operator",
    name: "Dev Partner Operator",
    partner_id: "dev-partner",
    realm_access: { roles: ["PARTNER_OPERATOR"] },
    iat: now,
    exp: now + 60 * 60 * 8,
  };
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64(header)}.${b64(payload)}.`;
}

export async function initAuth(): Promise<void> {
  await loadConfig();
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
  if (devMode) return makeDevToken();
  return keycloak?.token ?? "";
}

export async function refreshToken(): Promise<void> {
  if (devMode || !keycloak) return;
  try {
    await keycloak.updateToken(30);
  } catch {
    keycloak.login();
  }
}

// The API said the session is no longer valid (401): sign in again.
export function login(): void {
  if (devMode || !keycloak) return;
  keycloak.login();
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
