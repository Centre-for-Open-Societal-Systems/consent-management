import os
from dataclasses import dataclass

from .testkey import DEFAULT_AUDIENCE, DEFAULT_KID, TEST_PRIVATE_KEY_PEM


def pm_partner_id_for(audience: str) -> str:
    """PM partner ID of a partner audience: bank-a -> PARTNER_BANK_A."""
    return "PARTNER_" + audience.upper().replace("-", "_")


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # CM is split by API audience (staff / partner). base_url is the STAFF api
    # (Keycloak); partner_base_url is the PARTNER api (/validate, JWKS,
    # receipts). Default partner→base_url so a single audience=all instance works.
    base_url: str          # STAFF api base, e.g. http://consent-manager-api (no path prefix)
    partner_base_url: str  # PARTNER api base, e.g. http://consent-manager-partner-api
    verify_tls: bool
    run_e2e: bool          # run the data-creating round-trip
    fail_on_error: bool    # propagate pytest exit code (CD gating) — handled in entrypoint
    auth_enabled: bool     # whether the CM enforces Keycloak auth
    token_url: str         # Keycloak token endpoint (e2e + auth)
    client_id: str         # client-credentials client (has CONSENT_MANAGER_ADMIN)
    client_secret: str
    controller_id: str     # data controller the sanity partner's PM policy is for
    # ── Partner Management (signed e2e round-trip) ──────────────────────────
    # Partner keys and policies live in PM, so CM can't self-inject either. The
    # e2e signs with a private key (bundled TEST key by default) and ensures a
    # persistent test partner in PM holds the matching public key and a scope
    # policy for controller_id. The public half is derived from the private key
    # at seed time — only the private key is a "secret".
    partner_audience: str       # the sanity partner's audience (consent `aud`)
    pm_kid: str                 # kid registered in PM for the sanity key
    pm_private_key_pem: str     # PEM private key the e2e signs with
    pm_partner_api_url: str     # PM partner API base (keys, policies; to check servability), e.g. http://partner-management-partner-api
    pm_admin_url: str           # PM staff-portal-api base (to seed), e.g. http://partner-management-staff-portal-api
    # partner_manager client-credentials to call the PM admin API. Empty when PM
    # auth is disabled (COMMON_AUTH_ENABLED=false).
    pm_admin_token_url: str
    pm_admin_client_id: str
    pm_admin_client_secret: str

    @classmethod
    def from_env(cls) -> "Config":
        base_url = (os.environ.get("SANITY_BASE_URL") or "http://localhost:8000").rstrip("/")
        return cls(
            base_url=base_url,
            # Default the partner base to the staff base so a single audience=all
            # instance (dev) works; the split deployment sets it to the partner api.
            partner_base_url=(os.environ.get("SANITY_PARTNER_BASE_URL") or base_url).rstrip("/"),
            verify_tls=_bool(os.environ.get("SANITY_VERIFY_TLS"), True),
            run_e2e=_bool(os.environ.get("SANITY_RUN_E2E"), False),
            fail_on_error=_bool(os.environ.get("SANITY_FAIL_ON_ERROR"), False),
            auth_enabled=_bool(os.environ.get("SANITY_AUTH_ENABLED"), True),
            token_url=os.environ.get("SANITY_TOKEN_URL", ""),
            client_id=os.environ.get("SANITY_CLIENT_ID", "consent-manager"),
            client_secret=os.environ.get("SANITY_CLIENT_SECRET", ""),
            controller_id=os.environ.get("SANITY_CONTROLLER_ID", "sanity-controller"),
            partner_audience=os.environ.get("SANITY_PARTNER_AUDIENCE") or DEFAULT_AUDIENCE,
            pm_kid=os.environ.get("SANITY_PM_KID") or DEFAULT_KID,
            pm_private_key_pem=os.environ.get("SANITY_PM_PRIVATE_KEY_PEM") or TEST_PRIVATE_KEY_PEM,
            pm_partner_api_url=(os.environ.get("SANITY_PM_PARTNER_API_URL") or "").rstrip("/"),
            pm_admin_url=(os.environ.get("SANITY_PM_ADMIN_URL") or "").rstrip("/"),
            pm_admin_token_url=os.environ.get("SANITY_PM_ADMIN_TOKEN_URL", ""),
            pm_admin_client_id=os.environ.get("SANITY_PM_ADMIN_CLIENT_ID", "partner-management"),
            pm_admin_client_secret=os.environ.get("SANITY_PM_ADMIN_CLIENT_SECRET", ""),
        )

    @property
    def pm_partner_id(self) -> str:
        """The sanity partner's PM ID — CM looks up keys and policy by it."""
        return pm_partner_id_for(self.partner_audience)

    @property
    def can_reach_pm(self) -> bool:
        """The signed e2e needs to at least check key servability in PM."""
        return bool(self.pm_partner_api_url)
