# ruff: noqa: E402
import asyncio
import logging

from .config import Settings

_config = Settings.get_config()

from openg2p_fastapi_common.app import Initializer as BaseInitializer

from .controllers import (
    AweController,
    ConsentVerificationController,
    DecisionsController,
    LifecycleController,
    MetaController,
    PartnerController,
    PartnerPortalController,
    SubjectController,
    VerificationController,
    WellKnownController,
)
from .models import (
    AuditLog,
    AuthContext,
    AweProcessedEvent,
    ConsentArtefact,
    ConsentEvidence,
    ConsentReceipt,
    ConsentRequest,
    DecisionLog,
    IssuedReceipt,
    Partner,
    PartnerPolicy,
    RevocationRecord,
    SubjectAuthSession,
)
from .services import (
    AssistedConsentService,
    AweClient,
    AweWebhookService,
    ConsentRecordService,
    ConsentService,
    CryptoService,
    EvidenceService,
    ExchangeService,
    LifecycleService,
    OtpConsentService,
    OtpPublisher,
    OtpService,
    PartnerService,
    PolicyService,
    ReceiptService,
    SubjectAuthService,
    VerificationService,
)

_logger = logging.getLogger(_config.logging_default_logger_name)


class Initializer(BaseInitializer):
    def initialize(self, **kwargs):
        super().initialize(**kwargs)

        # Services — order matters: a service that calls get_component() in its
        # __init__ must be constructed after its dependencies.
        CryptoService()
        PartnerService()
        PolicyService()
        ReceiptService()
        ExchangeService()  # depends on CryptoService
        VerificationService()
        ConsentService()
        LifecycleService()
        EvidenceService()
        SubjectAuthService()
        AssistedConsentService()
        ConsentRecordService()  # depends on AssistedConsentService, CryptoService  # depends on Lifecycle/Evidence/Receipt
        OtpService()
        if OtpPublisher().enabled:  # test harness: OTP copies in S3/MinIO
            OtpPublisher.get_component().ensure_bucket()
        OtpConsentService()  # depends on AssistedConsentService, PartnerService, ReceiptService
        AweClient()
        AweWebhookService()  # depends on PartnerService

        # Controllers — mounted per API audience (the platform's 4-API pattern).
        # One image, one deployable per audience; each mounts only its routes.
        audience = _config.api_audience
        staff = audience in ("staff", "all")
        partner = audience in ("partner", "all")
        beneficiary = audience in ("beneficiary", "all")
        _logger.info("Consent Manager API audience: %s", audience)

        if partner:
            # PARTNER api — PDP. Trust = partner-signed consent object (PM keys),
            # no Keycloak. Serves /validate, status, receipts, JWKS.
            VerificationController().post_init()
            WellKnownController().post_init()
        if staff:
            # STAFF api — Keycloak staff realm. Policy admin, approvals, decisions.
            PartnerController().post_init()
            MetaController().post_init()
            AweController().post_init()
            DecisionsController().post_init()
            # Consent scenario 1: staff verify assisted consents; partner users
            # (partner realm, own token check) use the partner portal API.
            ConsentVerificationController().post_init()
            PartnerPortalController().post_init()
        if beneficiary:
            # BENEFICIARY api — Keycloak beneficiary realm. /my/* + origination.
            SubjectController().post_init()
            LifecycleController().post_init()

    def migrate_database(self, args):
        super().migrate_database(args)

        async def migrate():
            _logger.info("Migrating consent manager database")
            for model in (
                Partner,
                PartnerPolicy,
                ConsentRequest,
                ConsentEvidence,
                AuthContext,
                SubjectAuthSession,
                ConsentArtefact,
                ConsentReceipt,
                RevocationRecord,
                DecisionLog,
                AuditLog,
                AweProcessedEvent,
                IssuedReceipt,
            ):
                await model.create_migrate()

            # create_migrate() only creates missing tables; it does not ALTER an
            # existing one. Apply idempotent column changes so a DB created under
            # an earlier schema picks up the current shape on next migrate.
            from openg2p_fastapi_common.context import dbengine
            from sqlalchemy import text

            async with dbengine.get().begin() as conn:
                # Partner is now a policy binding: keys + identity live in Partner
                # Management. Carry a PM reference; identity/onboarding fields go.
                await conn.execute(
                    text(
                        "ALTER TABLE partners ADD COLUMN IF NOT EXISTS "
                        "partner_mgmt_id VARCHAR(255)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_partners_partner_mgmt_id "
                        "ON partners (partner_mgmt_id)"
                    )
                )
                # name is now an optional display label; org_name + the old
                # partner-onboarding approval columns are gone (approval moved
                # onto the policy version).
                await conn.execute(text("ALTER TABLE partners ALTER COLUMN name DROP NOT NULL"))
                await conn.execute(text("ALTER TABLE partners DROP COLUMN IF EXISTS org_name"))
                await conn.execute(text("ALTER TABLE partners DROP COLUMN IF EXISTS approval_status"))
                await conn.execute(text("ALTER TABLE partners DROP COLUMN IF EXISTS awe_request_id"))
                # AWE approval now correlates to a policy version.
                await conn.execute(
                    text(
                        "ALTER TABLE partner_policies ADD COLUMN IF NOT EXISTS "
                        "awe_request_id VARCHAR(64)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_partner_policies_awe_request_id "
                        "ON partner_policies (awe_request_id)"
                    )
                )
                # Approval state machine: the version an approval is checked
                # against, why a version ended failed/stale/rejected, and at most
                # one pending version per binding (older duplicates → stale).
                await conn.execute(
                    text(
                        "ALTER TABLE partner_policies ADD COLUMN IF NOT EXISTS "
                        "base_version INTEGER"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE partner_policies ADD COLUMN IF NOT EXISTS "
                        "status_reason TEXT"
                    )
                )
                await conn.execute(
                    text(
                        "UPDATE partner_policies p SET status = 'stale', status_reason = "
                        "'Superseded by a newer pending version (one pending per binding)' "
                        "WHERE p.status = 'pending' AND EXISTS (SELECT 1 FROM "
                        "partner_policies q WHERE q.partner_id = p.partner_id AND "
                        "q.status = 'pending' AND q.version > p.version)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_policy_one_pending "
                        "ON partner_policies (partner_id) WHERE status = 'pending'"
                    )
                )

                # ── Single consent, one grant per registry (G2P-5719) ────────
                # A partner (audience) may now be bound to several controllers,
                # each binding with its own policy: uniqueness moves from
                # `audience` to (audience, controller_id). Existing rows are
                # unchanged — each partner's controller becomes its first binding.
                await conn.execute(
                    text(
                        """
                        DO $$
                        BEGIN
                          IF EXISTS (
                            SELECT 1 FROM pg_indexes
                            WHERE tablename = 'partners'
                              AND indexname = 'ix_partners_audience'
                              AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
                          ) THEN
                            DROP INDEX ix_partners_audience;
                            CREATE INDEX ix_partners_audience ON partners (audience);
                          END IF;
                          IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'uq_partner_audience_controller'
                          ) THEN
                            ALTER TABLE partners ADD CONSTRAINT uq_partner_audience_controller
                              UNIQUE (audience, controller_id);
                          END IF;
                        END $$;
                        """
                    )
                )
                # Replay/idempotency is per (jti, data_controller): the same
                # consent validated by two registries yields two artefacts.
                await conn.execute(
                    text(
                        """
                        DO $$
                        BEGIN
                          ALTER TABLE consent_artefacts
                            DROP CONSTRAINT IF EXISTS consent_artefacts_object_jti_key;
                          IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'uq_artefact_jti_controller'
                          ) THEN
                            ALTER TABLE consent_artefacts ADD CONSTRAINT uq_artefact_jti_controller
                              UNIQUE (object_jti, controller_id);
                          END IF;
                        END $$;
                        """
                    )
                )
                # Originated consents may carry several grants.
                for table in ("consent_artefacts", "consent_requests"):
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS grants JSONB")
                    )
                    await conn.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN controller_id DROP NOT NULL")
                    )
                await conn.execute(
                    text(
                        "ALTER TABLE decision_logs ADD COLUMN IF NOT EXISTS "
                        "data_controller VARCHAR(255)"
                    )
                )
                # Agri Stack exchange: the consent token a decision was made
                # on (department role). NULL for every other decision.
                for column in ("receipt_jti", "receipt_issuer"):
                    await conn.execute(
                        text(
                            "ALTER TABLE decision_logs ADD COLUMN IF NOT EXISTS "
                            f"{column} VARCHAR(255)"
                        )
                    )
                # ── Consent scenario 1: assisted requests + staff verification ──
                for column, ddl in (
                    ("method", "VARCHAR(20)"),
                    ("use_case", "VARCHAR(255)"),
                    ("created_by", "VARCHAR(255)"),
                    ("partner_audience", "VARCHAR(255)"),
                    ("submitted_at", "TIMESTAMPTZ"),
                    ("verified_by", "VARCHAR(255)"),
                    ("verified_at", "TIMESTAMPTZ"),
                    ("verification_note", "TEXT"),
                    # OTP consent (method "otp").
                    ("otp_hash", "VARCHAR(128)"),
                    ("otp_expires_at", "TIMESTAMPTZ"),
                    ("otp_attempts", "INTEGER"),
                    ("otp_verified_at", "TIMESTAMPTZ"),
                    ("otp_channel", "VARCHAR(50)"),
                    ("otp_destination", "VARCHAR(255)"),
                    ("otp_provider", "VARCHAR(50)"),
                    ("otp_reference", "VARCHAR(255)"),
                ):
                    await conn.execute(
                        text(
                            "ALTER TABLE consent_requests ADD COLUMN IF NOT EXISTS "
                            f"{column} {ddl}"
                        )
                    )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_consent_requests_partner_audience "
                        "ON consent_requests (partner_audience)"
                    )
                )
                # Other identifiers of the subject (e.g. the Farmer ID).
                for table in ("consent_requests", "consent_artefacts"):
                    await conn.execute(
                        text(
                            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                            "subject_identifiers JSONB"
                        )
                    )
                # How the subject confirmed, and the request a consent came from.
                await conn.execute(
                    text("ALTER TABLE consent_artefacts ADD COLUMN IF NOT EXISTS assurance JSONB")
                )
                await conn.execute(
                    text(
                        "ALTER TABLE consent_artefacts ADD COLUMN IF NOT EXISTS "
                        "consent_request_id VARCHAR"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_consent_artefacts_consent_request_id "
                        "ON consent_artefacts (consent_request_id)"
                    )
                )
            _logger.info("Database migration complete")

        asyncio.run(migrate())
