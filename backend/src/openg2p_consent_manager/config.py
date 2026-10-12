from openg2p_fastapi_common.config import Settings as BaseSettings
from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

from . import __version__


class TrustedReceiptIssuer(BaseModel):
    """An exchange Consent Manager whose consent receipts this CM accepts
    (department role). ``presenter``, when set, is the only presenter accepted
    on its receipts. ``status_url`` is a template with ``{jti}``; empty derives
    it from ``jwks_url`` (``<base>/consent/v1/receipts/{jti}/status``)."""

    issuer: str
    jwks_url: str
    presenter: str = ""
    status_url: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="consent_manager_", env_file=".env", extra="allow"
    )

    # Which API audience this instance serves — the platform's 4-API pattern
    # (staff / partner / beneficiary / agent). One image, one deployable per
    # audience; each mounts only its controllers and uses its own auth:
    #   staff       — Keycloak (staff realm): policy bindings, approvals, decisions
    #   partner     — PM keys (no Keycloak): the PDP /validate, status, receipts, JWKS
    #   beneficiary — Keycloak (beneficiary realm): /my/* + origination (deferred)
    #   all         — everything in one process (dev / back-compat default)
    api_audience: str = "all"

    openapi_title: str = "OpenG2P Consent Manager"
    openapi_description: str = """
        Consent Manager for OpenG2P.

        Acts as the Policy Decision Point (PDP) for outbound data sharing: it
        verifies partner-signed consent objects against each partner's onboarded
        policy and returns the effective set of fields a data holder (PEP) may
        release. Also issues canonical consent artefacts and signed receipts.
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

    # NOTE: the data controller / module is a per-binding attribute
    # (Partner.controller_id) — one shared CM serves many modules, and one
    # partner may be bound to several controllers (one binding + policy each).
    # /validate selects the consent's grant for the calling controller and
    # evaluates it against that (audience, controller) binding's policy.

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

    # ── Hot-path caching (pod-local, TTL) ───────────────────────────────────
    # The partner row + its active policy change rarely but are read on every
    # validate. Cached in-process per pod; staleness bounded by the TTL. (Keys
    # are NOT part of this — they live in Partner Management, see below.)
    partner_cache_ttl_sec: int = 60
    partner_cache_enabled: bool = True

    # ── Partner public keys (Partner Management service) ────────────────────
    # Partner signing keys are no longer stored in CM. They are owned by the
    # Partner Management (PM) service and fetched from its unauthenticated
    # key-fetch API: GET {partner_mgmt_api_url}/keys/{reference_id}. CM caches
    # them per pod with the discipline PM's Cache-Control implies. A partner's
    # PM reference is Partner.partner_mgmt_id (falling back to Partner.audience).
    #
    # Empty partner_mgmt_api_url disables PM fetching — verification then fails
    # closed (no keys → deny), which is the correct safe default until wired.
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

    # ── Caller authentication (Keycloak / OIDC bearer) ──────────────────────
    # Validates bearer tokens on protected endpoints against the Keycloak JWKS,
    # exactly like the AWE service. When auth_enabled is false (dev), tokens are
    # accepted without verification and role checks pass.
    auth_enabled: bool = True
    auth_issuer: str = ""  # e.g. https://keycloak.../realms/staff
    auth_jwks_url: str = ""  # usually issuer + /protocol/openid-connect/certs
    auth_audience: str = ""  # optional; empty disables the audience check
    auth_algorithms: list[str] = ["RS256", "ES256", "EdDSA"]
    # Role (realm- or client-scoped) required for partner/policy admin endpoints.
    auth_admin_role: str = "CONSENT_MANAGER_ADMIN"
    # Default subject id-type when a token omits the subject_id_type claim.
    subject_default_id_type: str = "national_id"

    # Role required to act on AWE approval tasks via CM's proxy/inbox. Approvers
    # log into CM (not AWE) and CM proxies task-list/decision calls to AWE with
    # the approver's own JWT.
    auth_approver_role: str = "CONSENT_MANAGER_APPROVER"

    # ── Approval Workflow Engine (AWE) integration ──────────────────────────
    # Widening a partner's data-share POLICY is gated behind human approval in the
    # shared, per-environment AWE. CM is a *caller service*: on a widening it
    # submits an approval request and keeps the new policy version `pending`,
    # flipping it `active` only when AWE delivers a terminal `request_approved`
    # webhook. Approvers act in CM's OWN UI — CM proxies AWE's task-list/decision
    # endpoints with the approver's JWT (AWE has no approver UI, only /admin).
    #
    # When awe_enabled is false (default), a widening policy activates immediately
    # (no approval gate) — legacy behaviour.
    awe_enabled: bool = False
    # Whether the FIRST policy of a new binding (a grant from nothing) needs
    # approval. On by default: with AWE on, a new binding denies everything until
    # its first policy is approved. Set false to let the first policy go active
    # immediately and gate only later widenings.
    awe_gate_first_policy: bool = True
    # Base URL of the environment's AWE, reachable from CM pods (e.g.
    # https://awe.<baseDomain>). No trailing slash needed.
    awe_base_url: str = ""
    awe_http_timeout_seconds: float = 30.0
    # AWE approval policy (workflow: stages/approvers/SLA) that governs data-share
    # policy changes. Registered in AWE out-of-band. NB: distinct from CM's own
    # data-share policy — this is the *approval* policy key.
    awe_policy_change_policy_key: str = "consent-manager.policy_change.v1"
    # CM→AWE auth for POST /v1/awe/requests. By default CM forwards the acting
    # admin's own bearer (as the registry does): it carries the issuer users log
    # in with, which is the one AWE trusts, and makes the admin the requester.
    # Without a caller bearer (auth disabled, automation without a token) CM
    # falls back to a service token: awe_static_token if set, else a Keycloak
    # client-credentials grant from awe_token_url. That token's `iss` must be an
    # issuer AWE accepts — use the EXTERNAL Keycloak URL when AWE validates the
    # external issuer (the commons default).
    awe_forward_caller_token: bool = True
    awe_token_url: str = ""  # Keycloak token endpoint
    awe_client_id: str = ""
    awe_client_secret: str = ""
    awe_static_token: str = ""
    # Per-caller callback secret CM registered into the shared AWE DB. AWE looks
    # the raw secret up by this id; CM holds the same raw secret to verify the
    # HMAC on inbound webhooks. `callback_secret_id` is passed on every request.
    awe_callback_secret_id: str = ""
    awe_callback_hmac_secret: str = ""
    # URL AWE should POST terminal webhooks back to, reachable FROM the AWE pod
    # (in-cluster: http://<staff-api-service>/consent/v1/awe/webhooks/decision).
    # Must resolve to the STAFF api, which serves the webhook route.
    awe_callback_url: str = ""
    # Reject webhooks whose signed timestamp is more than this far from now.
    awe_webhook_max_skew_sec: int = 300
    # The approver inbox lists open AND claimed tasks. AWE filters on one status
    # per call, so CM fetches up to this many of each and merges them.
    awe_inbox_fetch_limit: int = 100

    # ── Agri Stack exchange (G2P-5719) — all off by default ─────────────────
    # Exchange role: issue signed consent receipts on /validate when the caller
    # sends issue_receipts=true. Needs an issuer ID and the callers (their
    # partner_id / sender_id) allowed to request and present receipts. Receipts
    # are signed with the CM signing key above (same kid/alg, same JWKS).
    receipt_issuer: str = ""  # e.g. agri-stack-exchange-cm
    receipt_presenters: list[str] = []  # e.g. ["agri-composite"]
    receipt_ttl_seconds: int = 900
    # Department role: exchange CMs whose receipts /validate accepts. Empty →
    # receipts are denied (receipt_issuer_not_trusted); nothing else changes.
    trusted_receipt_issuers: list[TrustedReceiptIssuer] = []
    # Check each receipt's status at its issuer: always | never.
    receipt_status_check: str = "always"
    receipt_status_cache_ttl_seconds: int = 10
    receipt_jwks_cache_ttl_seconds: int = 300
    # Minimum interval between JWKS refetches for one issuer on an unknown kid.
    receipt_jwks_refresh_cooldown_seconds: int = 10
    receipt_fetch_timeout_seconds: float = 3.0

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
