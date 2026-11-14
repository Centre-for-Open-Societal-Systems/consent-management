# ruff: noqa: E402
import asyncio
import logging

from .config import Settings

_config = Settings.get_config()

from openg2p_fastapi_common.app import Initializer as BaseInitializer

from .controllers import (
    ConsentVerificationController,
    DecisionsController,
    LifecycleController,
    PartnerPortalController,
    StaffConsentController,
    SubjectController,
    VerificationController,
    WellKnownController,
)
from .models import (
    AuditLog,
    AuthContext,
    ConsentArtefact,
    ConsentEvidence,
    ConsentReceipt,
    ConsentRequest,
    DecisionLog,
    IssuedReceipt,
    RevocationRecord,
    SubjectAuthSession,
)
from .services import (
    AssistedConsentService,
    ConsentRecordService,
    ConsentService,
    ConsentTemplateClient,
    CryptoService,
    EvidenceService,
    ExchangeService,
    LifecycleService,
    MasterDataClient,
    PartnerService,
    PmPolicyClient,
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
        PmPolicyClient()
        PartnerService()  # depends on PmPolicyClient
        PolicyService()
        ReceiptService()
        ConsentTemplateClient()
        MasterDataClient()
        ExchangeService()  # depends on CryptoService
        VerificationService()
        ConsentService()
        LifecycleService()  # depends on PartnerService, ConsentTemplateClient
        EvidenceService()
        SubjectAuthService()
        AssistedConsentService()
        ConsentRecordService()  # depends on AssistedConsentService, CryptoService

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
            # STAFF api — Keycloak staff realm. Decisions. (Partners and their
            # access policies are managed in Partner Management.)
            DecisionsController().post_init()
            # Consent scenario 1: staff verify assisted consents; partner users
            # (partner realm, own token check) use the partner portal API.
            ConsentVerificationController().post_init()
            # Staff view of a consent, and revocation on the subject's request.
            StaffConsentController().post_init()
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
                ConsentRequest,
                ConsentEvidence,
                AuthContext,
                SubjectAuthSession,
                ConsentArtefact,
                ConsentReceipt,
                RevocationRecord,
                DecisionLog,
                AuditLog,
                IssuedReceipt,
            ):
                await model.create_migrate()

            # create_migrate() only creates missing tables; it does not ALTER an
            # existing one. Apply idempotent column changes so a DB created under
            # an earlier schema picks up the current shape on next migrate.
            from openg2p_fastapi_common.context import dbengine
            from sqlalchemy import text

            async with dbengine.get().begin() as conn:
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
                # G2P-5719 "use_case → template": a consent (and its request)
                # keeps the consent template it was made from as a label. Rename
                # the column, keeping its data (and, for a request made from a
                # template, the marker on its grants). Idempotent.
                await conn.execute(
                    text(
                        """
                        DO $$
                        DECLARE t text;
                        BEGIN
                          FOREACH t IN ARRAY ARRAY['consent_artefacts', 'consent_requests'] LOOP
                            IF EXISTS (
                              SELECT 1 FROM information_schema.columns
                              WHERE table_schema = current_schema() AND table_name = t
                                AND column_name = 'use_case'
                            ) THEN
                              IF EXISTS (
                                SELECT 1 FROM information_schema.columns
                                WHERE table_schema = current_schema() AND table_name = t
                                  AND column_name = 'template'
                              ) THEN
                                EXECUTE format(
                                  'UPDATE %I SET template = use_case '
                                  'WHERE template IS NULL AND use_case IS NOT NULL', t);
                                EXECUTE format('ALTER TABLE %I DROP COLUMN use_case', t);
                              ELSE
                                EXECUTE format(
                                  'ALTER TABLE %I RENAME COLUMN use_case TO template', t);
                              END IF;
                            END IF;
                          END LOOP;
                        END $$;
                        """
                    )
                )
                await conn.execute(
                    text(
                        """
                        UPDATE consent_requests r SET grants = (
                          SELECT jsonb_agg(
                            CASE WHEN g ? 'use_case'
                              THEN (g - 'use_case') || jsonb_build_object('template', g -> 'use_case')
                              ELSE g END
                            ORDER BY i)
                          FROM jsonb_array_elements(r.grants) WITH ORDINALITY AS e(g, i))
                        WHERE jsonb_typeof(r.grants) = 'array'
                          AND EXISTS (
                            SELECT 1 FROM jsonb_array_elements(r.grants) g
                            WHERE g ? 'use_case')
                        """
                    )
                )
                for table in ("consent_artefacts", "consent_requests"):
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS template VARCHAR(255)")
                    )
                await conn.execute(
                    text(
                        "ALTER TABLE decision_logs ADD COLUMN IF NOT EXISTS "
                        "data_controller VARCHAR(255)"
                    )
                )
                # The full reference of the PM policy applied (id + kind:key).
                for table in ("decision_logs", "consent_artefacts"):
                    for column, ddl in (("policy_id", "VARCHAR(64)"),
                                        ("policy_ref", "VARCHAR(300)")):
                        await conn.execute(
                            text(
                                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                                f"{column} {ddl}"
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
                    ("created_by", "VARCHAR(255)"),
                    ("partner_audience", "VARCHAR(255)"),
                    ("submitted_at", "TIMESTAMPTZ"),
                    ("verified_by", "VARCHAR(255)"),
                    ("verified_at", "TIMESTAMPTZ"),
                    ("verification_note", "TEXT"),
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
                # Who revoked a consent on the subject's request (staff API).
                await conn.execute(
                    text(
                        "ALTER TABLE revocation_records ADD COLUMN IF NOT EXISTS "
                        "revoked_by VARCHAR(255)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_consent_artefacts_consent_request_id "
                        "ON consent_artefacts (consent_request_id)"
                    )
                )
                # Consent by lookup (/validate, caller-terms mode): active
                # consents by (partner, subject, purpose), latest valid_until
                # first. Replaces ix_consent_artefacts_lookup (by template).
                await conn.execute(text("DROP INDEX IF EXISTS ix_consent_artefacts_lookup"))
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_consent_artefacts_purpose_lookup "
                        "ON consent_artefacts (partner_id, subject_id_type, "
                        "subject_id_value, (purpose ->> 'code'), valid_until DESC) "
                        "WHERE status = 'active'"
                    )
                )
                # Consent by lookup by one of the subject's other identifiers
                # (subject_identifiers @> [{"type", "value"}]).
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_consent_artefacts_subject_identifiers "
                        "ON consent_artefacts USING gin (subject_identifiers jsonb_path_ops)"
                    )
                )
                # Consent by lookup at a registry: by (partner,
                # subject, data controller).
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_consent_artefacts_controller_lookup "
                        "ON consent_artefacts (partner_id, subject_id_type, "
                        "subject_id_value, controller_id, valid_until DESC) "
                        "WHERE status = 'active'"
                    )
                )
            _logger.info("Database migration complete")

        asyncio.run(migrate())
