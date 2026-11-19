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
  // Partner Management UI, linked from the nav for admins (partners and their
  // access policies are managed there). Empty = no link.
  partnerManagementUiUrl?: string;
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
  if (devMode) return makeDevToken(devRoles());
  return keycloak?.token ?? "";
}

// In dev mode we grant both admin + approver so every surface is reachable.
function devRoles(): string[] {
  return [config.adminRole, config.approverRole].filter(Boolean);
}

export async function refreshToken(): Promise<void> {
  if (devMode || !keycloak) return;
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
