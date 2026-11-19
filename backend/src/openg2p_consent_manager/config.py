from openg2p_fastapi_common.config import Settings as BaseSettings
from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

from . import __version__


class TrustedReceiptIssuer(BaseModel):
    """An exchange Consent Manager whose consent tokens this CM accepts
    (department role). ``presenter``, when set, is the only presenter accepted
    on its tokens. ``status_url`` is a template with ``{jti}``; empty derives
    it from ``jwks_url`` (``<base>/consent/v1/receipts/{jti}/status``)."""

    issuer: str
    jwks_url: str
    presenter: str = ""
    status_url: str = ""


DEFAULT_WITHDRAWAL_METHOD = (
    "Ask the organisation operating this Consent Manager to revoke it "
    "(consent ID {consent_id})."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="consent_manager_", env_file=".env", extra="allow"
    )

    # Which API audience this instance serves — the platform's 4-API pattern
    # (staff / partner / beneficiary / agent). One image, one deployable per
    # audience; each mounts only its controllers and uses its own auth:
    #   staff       — Keycloak (staff realm): decisions, consent verifications
    #   partner     — PM keys (no Keycloak): the PDP /validate, status, receipts, JWKS
    #   beneficiary — Keycloak (beneficiary realm): /my/* + origination (deferred)
    #   all         — everything in one process (dev / back-compat default)
    api_audience: str = "all"

    openapi_title: str = "OpenG2P Consent Manager"
    openapi_description: str = """
        Consent Manager for OpenG2P.

        Acts as the Policy Decision Point (PDP) for outbound data sharing: it
        verifies partner-signed consent objects against each partner's access
        policy (in Partner Management) and returns the effective set of fields a
        data holder (PEP) may release. Also issues canonical consent artefacts and signed receipts.
        """
    openapi_version: str = __version__

    # ── Database ────────────────────────────────────────────────────────────
    db_driver: str = "postgresql+asyncpg"
    db_username: str = "postgres"
    db_password: str = "postgres"
    db_hostname: str = "localhost"
    db_port: int = 5432
    db_dbname: str = "consent_manager_db"

    # Pooling is handled by openg2p-fastapi-common's async engine. For horizontal
    # scaling the app is fully stateless: scale by adding pods/workers and ensure
    # Postgres max_connections ≳ (pods × workers × pool_size + headroom).

    # ── Signing / trust ─────────────────────────────────────────────────────
    # CM receipt signing key. Preferred source is a PKCS#12 (.p12) keystore
    # holding the private key (+ certificate). Falls back to a PEM string, then
    # to a process-local ephemeral key (dev only). The signing algorithm is
    # auto-detected from the loaded key type (Ed25519→EdDSA, EC→ES256, RSA→RS256).
    cm_signing_p12_path: str = ""
    cm_signing_p12_password: str = ""
    cm_signing_private_key_pem: str = ""
    cm_signing_kid: str = "cm-2025-01"
    cm_signing_algorithm: str = "EdDSA"  # fallback hint only; key type wins
    # Set true by the Helm chart in demo mode so the service warns loudly that it
    # is signing with the public bundled demo key (must be replaced for production).
    cm_signing_is_demo: bool = False

    # NOTE: there is no global data controller: one shared CM serves many
    # registries. /validate selects the consent's grant for the calling
    # controller and evaluates it against the partner's policy for that
    # controller in Partner Management.

    # Replay window for embedded consent objects (seconds). issued_at must be
    # within now ± this skew.
    replay_freshness_window_sec: int = 300

    # ── OIDC (origination flow ID-token validation) ─────────────────────────
    # If oidc_jwks_url is set, ID tokens are signature-verified against the IdP
    # JWKS. Otherwise claims are read unverified (dev only) and token_validated
    # is recorded as false.
    oidc_jwks_url: str = ""
    oidc_issuer: str = ""
    oidc_audience: str = ""

    # ── Partner public keys (Partner Management service) ────────────────────
    # Partner signing keys are no longer stored in CM. They are owned by the
    # Partner Management (PM) service and fetched from its unauthenticated
    # key-fetch API: GET {partner_mgmt_api_url}/keys/{reference_id}. CM caches
    # them per pod with the discipline PM's Cache-Control implies. A partner's
    # PM id follows from its audience: bank-a → PARTNER_BANK_A.
    #
    # Empty partner_mgmt_api_url disables PM fetching — verification then fails
    # closed (no keys / no policy → deny), the safe default until wired.
    partner_mgmt_api_url: str = ""  # e.g. http://commons-services-pm-partner-api
    # Crypto backend for verifying the partner's signed consent object (a compact
    # JWS). "partner-mgmt" verifies against keys fetched from PM via the shared
    # openg2p-fastapi-common CryptoHelper. "keymanager" (Mosip) and "local"
    # (seed keys, tests) remain selectable but are not the default.
    crypto_backend: str = "partner-mgmt"
    # Algorithms accepted on the partner consent JWS. The fastapi-common default
    # is "RS256" only; partners commonly use EdDSA/ES256, so widen it here.
    crypto_allowed_algorithms: str = "EdDSA,ES256,RS256"
    # Soft TTL: refresh window. Bounds how long a rotated/revoked key stays
    # trusted. Capped by the response's Cache-Control max-age when smaller.
    partner_key_cache_ttl_seconds: int = 300
    # Hard TTL: during a PM outage, serve last-known-good keys up to this age,
    # then fail closed.
    partner_key_hard_ttl_seconds: int = 21600
    # Negative cache: remember a 404 ("no keys") briefly to avoid hammering PM
    # for a disabled/unknown partner on every request.
    partner_key_negative_ttl_seconds: int = 30
    # Minimum interval between forced refetches for one partner (throttles the
    # unknown-kid refresh that catches key rotation immediately).
    partner_key_refresh_cooldown_seconds: int = 10
    # HTTP timeout (seconds) for a single key fetch from PM.
    partner_key_fetch_timeout_seconds: float = 3.0

    # ── Partner access policies (Partner Management service) ────────────────
    # What a partner may access is its policy in PM, read from PM's internal
    # policy API: GET {partner_mgmt_api_url}/policies/PARTNER_<AUD>
    # ?data_controller=<controller> (a scope policy, one per controller).
    # Cached per pod and per (partner, controller), as the keys are.
    # Soft TTL: refresh window; capped by PM's Cache-Control max-age when smaller.
    partner_policy_cache_ttl_seconds: int = 60
    # Hard TTL: while PM is unreachable, serve its last answer up to this age,
    # then fail closed (deny policy_unavailable).
    partner_policy_hard_ttl_seconds: int = 21600
    # Negative cache: remember a 404 (no such partner / policy) this long. Also
    # the wait before retrying PM after a failed fetch.
    partner_policy_negative_ttl_seconds: int = 30
    # HTTP timeout (seconds) for a single policy fetch from PM.
    partner_policy_fetch_timeout_seconds: float = 3.0

    # ── Caller authentication (Keycloak / OIDC bearer) ──────────────────────
    # Validates bearer tokens on protected endpoints against the Keycloak JWKS,
    # exactly like the AWE service. When auth_enabled is false (dev), tokens are
    # accepted without verification and role checks pass.
    auth_enabled: bool = True
    auth_issuer: str = ""  # e.g. https://keycloak.../realms/staff
    auth_jwks_url: str = ""  # usually issuer + /protocol/openid-connect/certs
    auth_audience: str = ""  # optional; empty disables the audience check
    auth_algorithms: list[str] = ["RS256", "ES256", "EdDSA"]
    # Role (realm- or client-scoped) required for the admin endpoints (decisions).
    auth_admin_role: str = "CONSENT_MANAGER_ADMIN"
    # Default subject id-type when a token omits the subject_id_type claim.
    subject_default_id_type: str = "national_id"

    # Role required to verify assisted consents (approve / reject their evidence).
    auth_approver_role: str = "CONSENT_MANAGER_APPROVER"

    # Consent receipt `withdrawal.method`: how the subject withdraws. Free text
    # (an operator may name a contact or URL); {consent_id} is replaced by the
    # consent's ID. Staff revoke on the subject's request (staff API).
    # Empty → this default.
    receipt_withdrawal_method: str = DEFAULT_WITHDRAWAL_METHOD
    # Base URL of the beneficiary api, when it is deployed: its subject
    # endpoint is then listed as an additional withdrawal method. Empty → none.
    receipt_withdrawal_subject_api_url: str = ""

    # ── Agri Stack exchange (G2P-5719) — all off by default ─────────────────
    # Consent tokens (receipt_* settings): short-lived JWSs, one per registry
    # and query — not the Kantara consent receipt.
    # Exchange role: issue signed consent tokens on /validate when the caller
    # sends issue_receipts=true. Needs an issuer ID and the callers (their
    # partner_id / sender_id) allowed to request and present tokens. Tokens
    # are signed with the CM signing key above (same kid/alg, same JWKS).
    receipt_issuer: str = ""  # e.g. agri-stack-exchange-cm
    receipt_presenters: list[str] = []  # e.g. ["agri-composite"]
    # Token lifetime (capped by the consent's expiry). Short: a department
    # checks tokens offline, so a revocation reaches issued tokens within it.
    receipt_ttl_seconds: int = 300
    # Department role: exchange CMs whose consent tokens /validate accepts.
    # Empty → tokens are denied (receipt_issuer_not_trusted); nothing else changes.
    trusted_receipt_issuers: list[TrustedReceiptIssuer] = []
    # Check each consent token's status at its issuer: never | always. Off by
    # default: tokens are short-lived and single-use; a revocation applies at
    # the exchange CM at once and to issued tokens within their lifetime.
    # "always" adds a runtime call to the exchange CM per token.
    receipt_status_check: str = "never"
    receipt_status_cache_ttl_seconds: int = 10
    receipt_jwks_cache_ttl_seconds: int = 300
    # Minimum interval between JWKS refetches for one issuer on an unknown kid.
    receipt_jwks_refresh_cooldown_seconds: int = 10
    receipt_fetch_timeout_seconds: float = 3.0
    # Exchange role (receipt_issuer and receipt_presenters set): consents made
    # here through the partner portal or the lifecycle API are made from a
    # consent template offered to the partner — a code, title, purpose, the
    # scopes per registry (required / optional) and limits (subject ID types,
    # longest validity). Templates are read from a template source (at the
    # Agri Stack Exchange, the Composite's internal endpoint), the URL of its
    # template collection:
    #   GET {consent_template_source_url}?partner=<audience>
    #   GET {consent_template_source_url}/{code}?partner=<audience>
    # Empty → such consents cannot be made (503, fail closed).
    consent_template_source_url: str = ""  # e.g. http://agri-composite-api/composite/v1/internal/consent-templates
    consent_template_fetch_timeout_seconds: float = 3.0
    # How long a template answer is reused (per pod); capped by the source's
    # Cache-Control max-age when smaller.
    consent_template_cache_ttl_seconds: int = 60

    # ── Partner portal: partner users (consent scenario 1) — off by default ──
    # Partner users log in to the Keycloak `partner` realm (not the staff realm)
    # and carry a `partner_id` claim (the partner's audience, e.g. bank-a).
    # Empty issuer → the partner-portal API answers 404. Tokens are always
    # signature-verified against the realm's JWKS (empty JWKS URL → derived
    # from the issuer), independent of auth_enabled (which is for staff).
    partner_auth_issuer: str = ""  # e.g. https://keycloak.../realms/partner
    partner_auth_jwks_url: str = ""
    partner_auth_audience: str = ""  # optional; empty disables the audience check
    partner_auth_partner_claim: str = "partner_id"
    # A partner user needs at least one of these realm/client roles.
    partner_auth_roles: list[str] = ["PARTNER_OPERATOR", "PARTNER_ADMIN"]

    # ── Consent evidence (signed forms) — S3-compatible object storage ──────
    # Garage in OpenG2P commons. Empty endpoint → uploads/downloads answer 503.
    evidence_s3_endpoint: str = ""  # e.g. http://commons-garage:3900
    evidence_s3_access_key: str = ""
    evidence_s3_secret_key: str = ""
    evidence_s3_bucket: str = "consent-evidence"  # created if missing
    evidence_s3_region: str = "garage"
    evidence_max_bytes: int = 10 * 1024 * 1024
    evidence_allowed_types: list[str] = ["application/pdf", "image/jpeg", "image/png"]
    evidence_max_files_per_request: int = 20
    # Other identifiers of the subject that an assisted request must carry besides
    # its subject ID (e.g. ["FARMER_ID"] in Agri Stack: the consent is about the
    # person, keyed by the national ID; the Farmer ID names the farmer). Staff
    # confirm them in the registry before approving. Empty: none required (a
    # request may still carry some).
    subject_required_identifiers: list[str] = []
    # Where a partner checks the CM's signature on its downloaded consent record
    # (the public JWKS of the CM partner API), named in the record's README.
    consent_record_jwks_url: str = ""

    # ── Subject authentication with national ID (consent scenario 1, phase 2) ──
    # The subject (e.g. a farmer at the bank) proves who they are through an
    # OIDC provider such as eSignet (OTP, biometrics) during an assisted request:
    # the partner portal opens the provider's login, the provider redirects to
    # the callback below, and the CM checks the result. When enabled it is
    # MANDATORY: an assisted request cannot be submitted until its subject has
    # authenticated (and the signed form is still required).
    subject_auth_enabled: bool = False
    subject_auth_provider_name: str = "National ID (eSignet)"
    # Discovery document (issuer, endpoints, JWKS). Each value can be overridden
    # below; an override wins.
    subject_auth_discovery_url: str = ""  # e.g. https://esignet.<domain>/.well-known/openid-configuration
    subject_auth_issuer: str = ""
    subject_auth_authorize_url: str = ""
    subject_auth_token_url: str = ""
    subject_auth_userinfo_url: str = ""
    subject_auth_jwks_url: str = ""
    # The CM's OIDC client at the provider; it authenticates with private_key_jwt.
    subject_auth_client_id: str = ""
    # Client private key (RSA PEM): a file (a mounted Secret, read when first
    # needed, so a Secret created after the pod started is picked up) or inline.
    subject_auth_private_key_path: str = ""
    subject_auth_private_key_pem: str = ""
    subject_auth_key_kid: str = ""  # empty → the client ID (as registered)
    # Where the provider sends the browser back: the partner portal's host routes
    # /consent/v1/partner-portal/ to this API, e.g.
    # https://consent-partner-portal.<domain>/consent/v1/partner-portal/subject-auth/callback
    subject_auth_redirect_uri: str = ""
    subject_auth_scope: str = "openid profile"
    subject_auth_acr_values: str = "mosip:idp:acr:generated-code"
    # Claims asked for; individual_id (the national ID) is what the subject is
    # matched on — eSignet's `sub` is pairwise, so it can never equal the ID.
    subject_auth_claims: dict = {
        "userinfo": {"individual_id": {"essential": True}, "name": {"essential": True}},
        "id_token": {},
    }
    subject_auth_subject_claim: str = "individual_id"
    subject_auth_ui_locales: str = ""
    # Subject ID types the provider authenticates (the national ID); a request
    # for another type cannot be authenticated, so it cannot be submitted.
    subject_auth_id_types: list[str] = ["FAYDA_FAN", "NATIONAL_ID", "national_id"]
    # Claims kept with the authentication record (besides acr/amr/auth_time);
    # the rest of the provider's answer is not stored.
    subject_auth_stored_claims: list[str] = ["name"]
    # How long a started login may take (the state's lifetime).
    subject_auth_session_ttl_seconds: int = 300
    subject_auth_http_timeout_seconds: float = 10.0

    # ── Assisted confirmation (consent scenario 0) ───────────────────────────
    # Instead of a signed form and a Consent Approver: after the subject's
    # national-ID login, the partner portal shows the consent's terms and one
    # "I agree" tick makes the consent active at once. The weakest proof: off
    # unless enabled here AND allowed by the partner's policy
    # (allow_assisted_confirmation: at the Exchange on the use-case policy, read
    # through the consent template; elsewhere on each scope policy). Needs
    # subject_auth_enabled. The consent covers every scope requested; its
    # validity is set here (capped by the policy or template), not by the
    # partner user.
    assisted_confirmation_enabled: bool = False
    assisted_confirmation_validity_days: int = 30
    # The national-ID login must be this recent when the tick is submitted.
    assisted_confirmation_login_max_age_seconds: int = 600

    # ── Master Data code lists (partner portal choices) ─────────────────────
    # Purposes and subject ID types offered in the partner portal's request
    # form, read from the Master Data Service's staff API
    # (POST {mds_url}/catalogue/get_list_values) with a client-credentials
    # token of the CM's own Keycloak client (never the user's token), as
    # Partner Management does. A loaded list is enforced on portal requests;
    # a list absent from the country's Master Data (G2P-CAT-404), an empty
    # mds_url or list code, or Master Data unreachable = free text (accepted).
    mds_url: str = ""  # e.g. http://commons-services-master-data-api
    mds_token_url: str = ""  # Keycloak token endpoint; empty = no token sent
    mds_client_id: str = ""
    mds_client_secret: str = ""
    mds_purpose_list_code: str = "CONSENT_PURPOSE"
    mds_subject_id_type_list_code: str = "SUBJECT_ID_TYPE"
    # How long a list read is reused; a failed read is retried after
    # min(this, 30) seconds.
    mds_cache_ttl: int = 300
    mds_timeout: float = 5.0
