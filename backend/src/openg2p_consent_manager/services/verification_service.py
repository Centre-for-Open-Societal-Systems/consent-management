import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from openg2p_fastapi_common.crypto import CryptoFactory
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactSource,
    ArtefactStatus,
    ConsentArtefact,
    ConsentReceipt,
    DecisionLog,
)
from ..schemas.common import ReasonCode
from ..schemas.verification import ConsentObject, Decision, ValidateRequest
from ..utils.canonical import b64url_decode, iso_duration_to_timedelta, sha256_hex
from .exchange_service import TOKEN_TYPS, ExchangeService, to_datetime
from .partner_service import PartnerService
from .pm_policy_client import PolicyUnavailable, policy_fields
from .policy_service import CallerTerms, PolicyResult, PolicyService
from .receipt_service import ReceiptService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class VerificationService(BaseService):
    """The PDP hot path: verify an embedded consent object → decision."""

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.partners = PartnerService.get_component()
        # Partner consent-JWS verification via the shared fastapi-common crypto
        # helper (partner-mgmt backend fetches keys from Partner Management).
        self.crypto_helper = CryptoFactory.build(backend=_config.crypto_backend)
        self.policy = PolicyService.get_component()
        self.receipts = ReceiptService.get_component()
        self.exchange = ExchangeService.get_component()

    @staticmethod
    def _decode_jws(jws: str) -> tuple[dict, dict]:
        """Return ``(claims, protected_header)`` from a compact JWS WITHOUT
        verifying — used to identify the partner and read alg/kid before the
        signature is checked. A permit is never issued on unverified claims."""
        parts = jws.split(".")
        if len(parts) != 3:
            raise ValueError("consent_jws is not a compact JWS (expected header.payload.signature)")
        protected_header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
        return claims, protected_header

    async def validate(self, parsed: ValidateRequest) -> Decision:
        refusal = self._terms_refusal(parsed)
        if refusal is not None:
            return await refusal
        if parsed.consent_id:
            return await self._validate_stored(parsed)
        if not parsed.consent_jws:
            return await self._validate_lookup(parsed)
        if parsed.issue_receipts:
            return await self._validate_and_issue(parsed)
        now = datetime.now(timezone.utc)
        jws = parsed.consent_jws
        # The JWS string is itself canonical/immutable, so hash it directly.
        ctx_hash = sha256_hex(jws.encode("utf-8"))

        # Recover the claims from the JWS payload (unverified) so we can identify
        # the partner and evaluate policy. The signature is verified below,
        # before any permit is issued.
        try:
            claims, jws_header = self._decode_jws(jws)
        except Exception as exc:
            _logger.info("Malformed consent JWS: %s", exc)
            return await self._deny(
                ReasonCode.malformed_object, "consent JWS could not be decoded",
                now, ctx_hash,
            )
        # An exchange CM's consent token (department role).
        if isinstance(jws_header, dict) and jws_header.get("typ") in TOKEN_TYPS:
            return await self._validate_receipt(parsed, claims, jws_header, now, ctx_hash)
        try:
            obj = ConsentObject(**claims)
        except Exception as exc:
            _logger.info("Malformed consent JWS: %s", exc)
            return await self._deny(
                ReasonCode.malformed_object, "consent JWS could not be decoded",
                now, ctx_hash,
            )

        # 1. Grant selection — which registry (data controller) is asking.
        #    A consent with `grants` needs the caller to name its controller and
        #    is evaluated only on that controller's grant. A legacy consent is a
        #    single grant; if the caller names a controller it must match.
        if obj.has_grants:
            if not parsed.data_controller:
                return await self._deny(
                    ReasonCode.malformed_object,
                    "data_controller is required when the consent carries grants",
                    now, ctx_hash, jti=obj.jti,
                )
            controller = parsed.data_controller
            grant = obj.grant_for(controller)
            if grant is None:
                return await self._deny(
                    ReasonCode.controller_not_granted,
                    f"consent has no grant for data_controller '{controller}'",
                    now, ctx_hash, jti=obj.jti, data_controller=controller,
                )
        else:
            controller = obj.data_controller
            if parsed.data_controller and parsed.data_controller != controller:
                return await self._deny(
                    ReasonCode.controller_not_granted,
                    f"consent is for data_controller '{controller}', "
                    f"not '{parsed.data_controller}'",
                    now, ctx_hash, jti=obj.jti, data_controller=parsed.data_controller,
                )
            grant = obj.grant_for(controller)

        # 1b. Subject — a caller-declared subject of the same id type as the
        #     consent's subject must be the same subject. (A different id type is
        #     for the registry to resolve; CM cannot compare across types.)
        ctx_subject = parsed.request_context.subject_id if parsed.request_context else None
        if self._subject_mismatch(ctx_subject, obj.subject_id.type, obj.subject_id.value):
            return await self._deny(
                ReasonCode.subject_mismatch,
                "request_context.subject_id does not match the consent subject",
                now, ctx_hash, jti=obj.jti, data_controller=controller,
            )

        # 2. Known party — the partner's active scope policy for this
        #    controller in Partner Management (cached) or, in caller-terms
        #    mode, the terms the presenter sent (no policy read).
        terms_mode = self._caller_terms_mode(parsed)
        try:
            material = (
                self.partners.terms_material(obj.aud, CallerTerms.from_context(
                    parsed.request_context))
                if terms_mode
                else await self.partners.get_verification_material(obj.aud, controller)
            )
        except PolicyUnavailable:
            return await self._deny(
                ReasonCode.policy_unavailable,
                "the partner's policy could not be read from Partner Management",
                now, ctx_hash, jti=obj.jti, data_controller=controller,
            )
        if material is None:
            return await self._deny(
                ReasonCode.unknown_partner,
                f"partner has no active policy for data_controller '{controller}' in "
                f"Partner Management (unknown, inactive or not granted)",
                now, ctx_hash, jti=obj.jti, data_controller=controller,
            )
        partner = material["partner"]
        policy = material["policy"]

        # 2b. Partner-signed consents are accepted only when the applicable
        #     policy (or the presenter's limits) allows them (the CM cannot
        #     verify the subject agreed; only the partner's signature).
        #     Consents the CM holds are unaffected.
        if not getattr(policy, "allow_partner_consents", False):
            return await self._deny(
                ReasonCode.partner_consent_not_accepted,
                "the partner's policy (or the request's limits) does not accept "
                "consents it signed itself (allow_partner_consents)",
                now, ctx_hash, partner_id=partner.audience, jti=obj.jti,
                data_controller=controller, policy=policy,
            )

        # 3. Signature — verify the consent JWS against the partner's key from
        # Partner Management. The shared helper reads the kid from the JWS header,
        # fetches the key for the partner's PM id (PARTNER_<AUD>), enforces
        # algorithm safety, and verifies.
        alg = jws_header.get("alg")
        if policy.allowed_signing_algs and alg not in policy.allowed_signing_algs:
            return await self._deny(
                ReasonCode.signature_invalid, "signing algorithm not permitted",
                now, ctx_hash, partner_id=partner.audience, jti=obj.jti,
                data_controller=controller, policy=policy,
            )
        try:
            verified = await self.crypto_helper.verify_jwt(jws, km_ref_id=partner.partner_mgmt_id)
        except Exception as exc:
            _logger.warning("Consent JWS verification error: %s", exc)
            verified = False
        if not verified:
            return await self._deny(
                ReasonCode.signature_invalid,
                "signature did not verify (or no verifying key from partner management)",
                now, ctx_hash, partner_id=partner.audience, jti=obj.jti,
                data_controller=controller, policy=policy,
            )

        # 3b. Caller-terms mode — the requested scopes must be sent and the
        #     consent given for the purpose sent. Checked before a stored
        #     decision is reused: the same consent may be presented for
        #     another purpose.
        if terms_mode:
            refusal = self.policy.check_caller_terms(
                parsed.request_context, (obj.purpose or {}).get("code")
            )
            if refusal is not None:
                return await self._deny(
                    refusal.reason, refusal.detail, now, ctx_hash,
                    partner_id=partner.audience, jti=obj.jti, data_controller=controller,
                    policy=policy,
                )

        # Idempotency: the same object (jti) presented again for the same
        # controller returns its existing decision. Keyed per (jti, controller),
        # so one consent validated by two registries gets two decisions and
        # receipts. Checked only AFTER the policy and the signature: a stored
        # decision is never handed to an unsigned or altered object reusing a
        # known jti. The stored consent must also be this one — same partner,
        # subject and scopes — or the jti is being reused.
        existing = await self._existing_artefact(obj.jti, controller)
        if existing is not None:
            if (
                existing.partner_id != partner.audience
                or existing.subject_id_type != obj.subject_id.type
                or existing.subject_id_value != obj.subject_id.value
                or sorted(existing.data_scopes or []) != sorted(grant.data_scopes or [])
            ):
                return await self._deny(
                    ReasonCode.replay, "jti already used for a different consent",
                    now, ctx_hash, partner_id=partner.audience, jti=obj.jti,
                    data_controller=controller, policy=policy,
                )
            if self._subject_mismatch(
                ctx_subject, existing.subject_id_type, existing.subject_id_value
            ):
                return await self._deny(
                    ReasonCode.subject_mismatch,
                    "request_context.subject_id does not match the consent subject",
                    now, ctx_hash, jti=obj.jti, data_controller=controller,
                )
            return self._decision_from_artefact(existing, now)

        # 10. Replay / freshness — issued_at within the configured window.
        issued_at = _aware(obj.issued_at)
        skew = timedelta(seconds=_config.replay_freshness_window_sec)
        if abs((now - issued_at).total_seconds()) > skew.total_seconds():
            return await self._deny(
                ReasonCode.replay, "issued_at outside freshness window",
                now, ctx_hash, partner_id=partner.audience, jti=obj.jti,
                data_controller=controller, policy=policy,
            )

        # 4–8. Policy evaluation (audience, subject, purpose, scope, validity).
        result = self.policy.evaluate(obj, material, parsed.request_context, grant=grant)
        if not result.permit:
            return await self._deny(
                result.reason, result.detail, now, ctx_hash,
                partner_id=partner.audience, jti=obj.jti,
                policy=policy, data_controller=controller,
            )

        # Permit — mint canonical artefact + signed receipt + decision log for
        # this (consent, controller).
        # In caller-terms mode the stored consent keeps the template named (a
        # label); a later request without a consent finds it by its purpose
        # (consent by lookup).
        return await self._permit(
            obj, grant, partner, result, now, ctx_hash,
            template=parsed.request_context.template if terms_mode else None,
        )

    # ── Agri Stack exchange: issuing consent tokens (exchange role) ─────────

    async def _validate_and_issue(self, parsed: ValidateRequest) -> Decision:
        """``issue_receipts``: validate the partner's consent for each granted
        controller (or just ``data_controller``) exactly as a registry's call
        would — same checks, artefacts and decision log — and, if every one is a
        permit, sign one consent token per controller for the caller to present."""
        now = datetime.now(timezone.utc)
        ctx_hash = sha256_hex(parsed.consent_jws.encode("utf-8"))
        presenter = parsed.partner_id
        refusal = self.exchange.issuing_refusal(presenter)
        if refusal:
            return await self._deny(
                ReasonCode.receipt_presenter_not_allowed, refusal, now, ctx_hash,
                data_controller=parsed.data_controller,
            )
        try:
            claims, jws_header = self._decode_jws(parsed.consent_jws)
            if isinstance(jws_header, dict) and jws_header.get("typ") in TOKEN_TYPS:
                raise ValueError("receipts are issued from a partner consent, not a receipt")
            obj = ConsentObject(**claims)
        except Exception as exc:
            _logger.info("Malformed consent JWS for issue_receipts: %s", exc)
            return await self._deny(
                ReasonCode.malformed_object, "consent JWS could not be decoded",
                now, ctx_hash,
            )
        if parsed.data_controller:
            controllers = [parsed.data_controller]
        elif obj.has_grants:
            controllers = [g.data_controller for g in obj.grants]
        else:
            controllers = [obj.data_controller]

        decisions: dict[str, Decision] = {}
        for controller in controllers:
            decision = await self.validate(
                parsed.model_copy(update={"data_controller": controller, "issue_receipts": False})
            )
            if decision.decision != "permit":
                return decision
            decisions[controller] = decision

        receipts = {
            controller: await self.exchange.issue(obj, decision, presenter, now)
            for controller, decision in decisions.items()
        }
        grants = {c: list(d.effective_data_scopes or []) for c, d in decisions.items()}
        if len(decisions) == 1:
            return next(iter(decisions.values())).model_copy(
                update={"receipts": receipts, "grants": grants}
            )
        first = next(iter(decisions.values()))
        return Decision(
            decision="permit", reason_code=ReasonCode.ok,
            subject_id=first.subject_id,
            valid_until=min(_aware(d.valid_until) for d in decisions.values()),
            evaluated_at=now, receipts=receipts, grants=grants,
        )

    # ── Consent by lookup (no consent_jws / consent_id) ─────────────────────

    async def _validate_lookup(self, parsed: ValidateRequest) -> Decision:
        """Neither ``consent_jws`` nor ``consent_id``: find a consent the CM
        holds and validate it as a stored consent.

        * Caller-terms mode (a token presenter): by (consent_partner_id,
          subject, purpose).
        * Otherwise (a registry, path A): by (partner_id, subject,
          data_controller).

        Active and valid now; several → latest ``valid_until``, then newest;
        none → ``consent_not_found``."""
        now = datetime.now(timezone.utc)
        ctx = parsed.request_context
        subject = ctx.subject_id if ctx else None
        if self._caller_terms_mode(parsed):
            partner_aud = parsed.consent_partner_id
            if subject is None or not partner_aud:
                return self._lookup_malformed(now)
            # Served by the partial index ix_consent_artefacts_purpose_lookup.
            key = ctx.purpose
            match = ConsentArtefact.purpose["code"].astext == key
            what = f"purpose '{key}'"
            update = {}
        else:
            partner_aud = parsed.partner_id
            key = parsed.data_controller
            if subject is None or not partner_aud or not key:
                return self._lookup_malformed(now)
            # Served by the partial index ix_consent_artefacts_controller_lookup
            # (a single-controller consent); an originated consent with several
            # grants has no controller_id and is matched on its grants.
            # A consent made from a consent template (exchange role) is
            # presented through the token presenter, not by a direct call to
            # the registry.
            match = and_(
                ConsentArtefact.template.is_(None),
                or_(
                    ConsentArtefact.controller_id == key,
                    and_(
                        ConsentArtefact.controller_id.is_(None),
                        ConsentArtefact.grants.contains([{"data_controller": key}]),
                    ),
                ),
            )
            what = f"data controller '{key}'"
            # The stored-consent path checks consent_partner_id against the
            # consent: here the caller is that partner.
            update = {"consent_partner_id": partner_aud}
        ctx_hash = sha256_hex(
            f"lookup:{partner_aud}:{subject.type}:{subject.value}:{key}".encode("utf-8")
        )
        async with async_session()() as session:
            result = await session.execute(
                select(ConsentArtefact)
                .where(
                    ConsentArtefact.partner_id == partner_aud,
                    # The consent's subject (the lookup indexes) or one of its
                    # other identifiers, e.g. the Farmer ID (GIN index
                    # ix_consent_artefacts_subject_identifiers). Types are
                    # matched as spelled: no aliasing.
                    or_(
                        and_(
                            ConsentArtefact.subject_id_type == subject.type,
                            ConsentArtefact.subject_id_value == subject.value,
                        ),
                        ConsentArtefact.subject_identifiers.contains(
                            [{"type": subject.type, "value": subject.value}]
                        ),
                    ),
                    match,
                    ConsentArtefact.source != ArtefactSource.receipt.value,
                    ConsentArtefact.status == ArtefactStatus.active.value,
                    ConsentArtefact.valid_from <= now,
                    ConsentArtefact.valid_until > now,
                )
                .order_by(
                    ConsentArtefact.valid_until.desc(), ConsentArtefact.created_at.desc()
                )
                .limit(1)
            )
            artefact = result.scalars().first()
        if artefact is None:
            return await self._deny(
                ReasonCode.consent_not_found,
                f"no active consent of partner '{partner_aud}' for this subject and {what}",
                now, ctx_hash, partner_id=partner_aud,
                data_controller=parsed.data_controller,
            )
        return await self._validate_stored(
            parsed.model_copy(update={"consent_id": artefact.id, **update}),
            artefact=artefact,
        )

    @staticmethod
    def _lookup_malformed(now) -> Decision:
        return Decision(
            decision="deny", reason_code=ReasonCode.malformed_object,
            detail="give 'consent_jws' or 'consent_id', or for consent by lookup "
            "request_context.subject_id with either (a configured presenter) "
            "consent_partner_id and request_context.purpose, or partner_id and "
            "data_controller",
            evaluated_at=now,
        )

    async def _stored_grants(self, artefact: ConsentArtefact, now: datetime) -> list:
        """The grants of a stored consent, each with the artefact (consent_id)
        it is recorded on. An originated consent holds its grants; a
        partner-signed one is stored per controller (same jti), so its grants
        are the active, valid artefacts of that jti for the same partner and
        template."""
        if artefact.source != ArtefactSource.embedded.value:
            return [
                {**g, "consent_id": artefact.id}
                for g in artefact.grants or [
                    {
                        "data_controller": artefact.controller_id,
                        "effective_data_scopes": artefact.effective_data_scopes or [],
                    }
                ]
            ]
        async with async_session()() as session:
            result = await session.execute(
                select(ConsentArtefact).where(
                    ConsentArtefact.object_jti == artefact.object_jti,
                    ConsentArtefact.partner_id == artefact.partner_id,
                    ConsentArtefact.template == artefact.template,
                    ConsentArtefact.source == ArtefactSource.embedded.value,
                    ConsentArtefact.status == ArtefactStatus.active.value,
                    ConsentArtefact.valid_until > now,
                )
            )
            siblings = list(result.scalars().all())
        return [
            {
                "data_controller": a.controller_id,
                "effective_data_scopes": a.effective_data_scopes or [],
                "consent_id": a.id,
            }
            for a in sorted(siblings, key=lambda a: a.controller_id or "")
        ]

    # ── A stored (originated) consent presented by its ID ───────────────────

    async def _validate_stored(
        self, parsed: ValidateRequest, artefact: Optional[ConsentArtefact] = None
    ) -> Decision:
        """``consent_id``: a consent the CM holds (originated — e.g. an assisted
        consent verified by staff). Checks the consent's state, the partner
        that obtained it and the subject; each grant is narrowed to the
        partner's CURRENT policy for its controller (and requested_scopes).
        With ``issue_receipts``, signs one consent token per granted controller in
        exactly the format used for a partner-signed consent. ``artefact``:
        the consent found by lookup (originated or partner-signed)."""
        from ..schemas.common import SubjectId

        now = datetime.now(timezone.utc)
        consent_id = parsed.consent_id
        partner_aud = parsed.consent_partner_id
        ctx_hash = sha256_hex(f"consent_id:{consent_id}:{partner_aud or ''}".encode("utf-8"))
        presenter = parsed.partner_id
        if parsed.issue_receipts:
            refusal = self.exchange.issuing_refusal(presenter)
            if refusal:
                return await self._deny(
                    ReasonCode.receipt_presenter_not_allowed, refusal, now, ctx_hash,
                    data_controller=parsed.data_controller,
                )

        if artefact is None:
            async with async_session()() as session:
                artefact = await session.get(ConsentArtefact, consent_id)
            if artefact is not None and artefact.source != ArtefactSource.originated.value:
                artefact = None
        if artefact is None:
            return await self._deny(
                ReasonCode.unknown_consent, "no stored consent with this consent_id",
                now, ctx_hash, data_controller=parsed.data_controller,
            )
        log = {"partner_id": artefact.partner_id, "consent_id": artefact.id}
        if artefact.status == ArtefactStatus.revoked.value:
            return await self._deny(
                ReasonCode.revoked, "consent revoked", now, ctx_hash, **log
            )
        if (
            artefact.status == ArtefactStatus.expired.value
            or _aware(artefact.valid_until) < now
        ):
            return await self._deny(
                ReasonCode.expired, "consent expired", now, ctx_hash, **log
            )
        if artefact.status != ArtefactStatus.active.value:
            return await self._deny(
                ReasonCode.unknown_consent, f"consent is '{artefact.status}'",
                now, ctx_hash, **log,
            )
        if _aware(artefact.valid_from) > now:
            return await self._deny(
                ReasonCode.not_yet_valid, "consent not yet valid", now, ctx_hash, **log
            )
        # Records store the partner's audience as partner_id.
        audience = artefact.partner_id
        if not partner_aud or partner_aud != audience:
            return await self._deny(
                ReasonCode.partner_mismatch,
                "consent_partner_id is not the partner the consent was given to",
                now, ctx_hash, **log,
            )
        ctx_subject = parsed.request_context.subject_id if parsed.request_context else None
        if self._subject_mismatch(
            ctx_subject, artefact.subject_id_type, artefact.subject_id_value,
            artefact.subject_identifiers,
        ):
            return await self._deny(
                ReasonCode.subject_mismatch,
                "request_context.subject_id does not match the consent subject",
                now, ctx_hash, **log,
            )

        stored = await self._stored_grants(artefact, now)
        if parsed.data_controller:
            stored = [g for g in stored if g.get("data_controller") == parsed.data_controller]
            if not stored:
                return await self._deny(
                    ReasonCode.controller_not_granted,
                    f"consent has no grant for data_controller '{parsed.data_controller}'",
                    now, ctx_hash, data_controller=parsed.data_controller, **log,
                )
        requested = (
            set(parsed.request_context.requested_scopes)
            if parsed.request_context and parsed.request_context.requested_scopes
            else None
        )
        # Each grant is narrowed to the partner's CURRENT scope policy for the
        # controller in Partner Management or, in caller-terms mode, to the
        # presenter's terms (whose ceiling is the requested scopes).
        terms_mode = self._caller_terms_mode(parsed)
        granted: dict[str, tuple[list, object, str]] = {}
        try:
            if terms_mode:
                policy = CallerTerms.from_context(parsed.request_context)
                refusal = self._stored_terms_refusal(policy, parsed.request_context, artefact)
                if refusal is not None:
                    return await self._deny(
                        refusal.reason, refusal.detail, now, ctx_hash,
                        data_controller=parsed.data_controller,
                        policy=policy, **log,
                    )
            for g in stored:
                controller = g.get("data_controller")
                if not controller:
                    continue
                if not terms_mode:
                    material = await self.partners.get_verification_material(
                        audience, controller
                    )
                    if material is None:
                        continue
                    policy = material["policy"]
                    allowed = set(policy.allowed_data_scopes or [])
                else:
                    allowed = set(g.get("effective_data_scopes") or [])
                effective = set(g.get("effective_data_scopes") or []) & allowed
                if requested is not None:
                    effective &= requested
                if effective:
                    granted[controller] = (sorted(effective), policy, g["consent_id"])
        except PolicyUnavailable:
            return await self._deny(
                ReasonCode.policy_unavailable,
                "the partner's policy could not be read from Partner Management",
                now, ctx_hash, data_controller=parsed.data_controller, **log,
            )
        if not granted:
            return await self._deny(
                ReasonCode.scope_exceeds_policy,
                "no consented scope is permitted by the partner's current policy",
                now, ctx_hash, data_controller=parsed.data_controller, **log,
            )

        subject = SubjectId(type=artefact.subject_id_type, value=artefact.subject_id_value)
        decisions = {
            controller: Decision(
                decision="permit", reason_code=ReasonCode.ok, consent_id=cid,
                subject_id=subject, data_controller=controller,
                effective_data_scopes=scopes, valid_until=artefact.valid_until,
                **policy_fields(policy), evaluated_at=now,
            )
            for controller, (scopes, policy, cid) in granted.items()
        }
        async with async_session()() as session:
            for controller, (_, policy, cid) in granted.items():
                session.add(
                    DecisionLog(
                        partner_id=audience, consent_id=cid,
                        data_controller=controller, decision="permit",
                        reason_code=ReasonCode.ok.value, **policy_fields(policy),
                        request_ctx_hash=ctx_hash,
                    )
                )
            await session.commit()

        receipts = None
        if parsed.issue_receipts:
            # The consent as issue() reads it: the partner that obtained it,
            # its purpose, and when it was given (the artefact's creation).
            consent = SimpleNamespace(
                aud=partner_aud, purpose=artefact.purpose, issued_at=artefact.created_at
            )
            receipts = {
                controller: await self.exchange.issue(consent, decision, presenter, now)
                for controller, decision in decisions.items()
            }
        grants = {c: d.effective_data_scopes for c, d in decisions.items()}
        stored_receipt = await self._stored_receipt_id(artefact.id)
        if len(decisions) == 1:
            return next(iter(decisions.values())).model_copy(
                update={"receipts": receipts, "grants": grants, "receipt_id": stored_receipt}
            )
        return Decision(
            decision="permit", reason_code=ReasonCode.ok, consent_id=artefact.id,
            receipt_id=stored_receipt, subject_id=subject, valid_until=artefact.valid_until,
            evaluated_at=now, receipts=receipts, grants=grants,
        )

    async def _stored_receipt_id(self, consent_id: str) -> Optional[str]:
        async with async_session()() as session:
            result = await session.execute(
                select(ConsentReceipt.id).where(ConsentReceipt.consent_id == consent_id)
            )
            return result.scalars().first()

    # ── Agri Stack exchange: accepting consent tokens (department role) ─────

    async def _validate_receipt(
        self, parsed: ValidateRequest, claims, header: dict, now, ctx_hash
    ) -> Decision:
        """A consent token from an exchange CM: verify it against a trusted
        issuer's JWKS, check audience/presenter/time (and, if configured, its
        status at the issuer), then apply THIS CM's standing policy for the presenter at the
        calling controller. Effective scopes = token scopes ∩ policy."""
        claims = claims if isinstance(claims, dict) else {}
        iss = claims.get("iss")
        jti = claims.get("jti") if isinstance(claims.get("jti"), str) else None
        controller = parsed.data_controller
        log = {"jti": jti, "data_controller": controller,
               "receipt_jti": jti, "receipt_issuer": iss if isinstance(iss, str) else None}

        if not _config.trusted_receipt_issuers:
            return await self._deny(
                ReasonCode.receipt_issuer_not_trusted,
                "consent receipts are not accepted: no trusted receipt issuers configured",
                now, ctx_hash, **log,
            )
        trusted = self.exchange.trusted_issuer(iss)
        if trusted is None:
            return await self._deny(
                ReasonCode.receipt_issuer_not_trusted,
                f"receipt issuer '{iss}' is not trusted", now, ctx_hash, **log,
            )

        # 1. Signature against the issuer's JWKS (refetched on an unknown kid).
        key = await self.exchange.issuer_key(trusted, header.get("kid"))
        if key is None:
            return await self._deny(
                ReasonCode.receipt_invalid,
                "no key for the receipt's kid in the issuer's JWKS", now, ctx_hash, **log,
            )
        pem, alg = key
        if header.get("alg") != alg:
            return await self._deny(
                ReasonCode.receipt_invalid, "receipt alg does not match the issuer key",
                now, ctx_hash, **log,
            )
        try:
            from jwt.api_jws import PyJWS

            PyJWS().decode(parsed.consent_jws, pem, algorithms=[alg])
        except Exception as exc:
            _logger.info("Receipt signature did not verify: %s", exc)
            return await self._deny(
                ReasonCode.receipt_invalid, "receipt signature did not verify",
                now, ctx_hash, **log,
            )

        # Claims (now verified).
        sub = claims.get("sub")
        scopes = claims.get("scopes")
        presenter = claims.get("presenter")
        exp = to_datetime(claims.get("exp"))
        nbf = to_datetime(claims.get("nbf")) or to_datetime(claims.get("iat"))
        consent_issued_at = to_datetime(claims.get("consent_issued_at")) or nbf
        consent_exp = to_datetime(claims.get("consent_exp")) or exp
        if not (
            jti and isinstance(sub, dict) and sub.get("type") and sub.get("value")
            and isinstance(scopes, list) and isinstance(presenter, str) and presenter
            and exp and nbf and consent_issued_at and consent_exp
        ):
            return await self._deny(
                ReasonCode.receipt_invalid, "receipt is missing required claims",
                now, ctx_hash, **log,
            )

        # 2. Audience = this controller; presenter = the caller; time window.
        if not controller:
            return await self._deny(
                ReasonCode.malformed_object,
                "data_controller is required for a consent receipt", now, ctx_hash, **log,
            )
        if claims.get("aud") != controller:
            return await self._deny(
                ReasonCode.audience_mismatch,
                f"receipt is for '{claims.get('aud')}', not '{controller}'",
                now, ctx_hash, **log,
            )
        if (trusted.presenter and presenter != trusted.presenter) or presenter != parsed.partner_id:
            return await self._deny(
                ReasonCode.presenter_mismatch,
                f"receipt presenter '{presenter}' is not the caller "
                f"'{parsed.partner_id}'", now, ctx_hash, **log,
            )
        leeway = timedelta(seconds=30)
        if now + leeway < nbf:
            return await self._deny(
                ReasonCode.expired, "receipt not yet valid", now, ctx_hash, **log,
            )
        if now >= exp:
            return await self._deny(
                ReasonCode.expired, "receipt expired", now, ctx_hash, **log,
            )
        ctx_subject = parsed.request_context.subject_id if parsed.request_context else None
        if self._subject_mismatch(ctx_subject, sub["type"], sub["value"]):
            return await self._deny(
                ReasonCode.subject_mismatch,
                "request_context.subject_id does not match the receipt subject",
                now, ctx_hash, **log,
            )

        # 3. Status at the issuer (revoked consent → revoked token); optional,
        #    off by default (tokens are short-lived).
        if (_config.receipt_status_check or "never").lower() == "always":
            status = await self.exchange.remote_status(trusted, jti)
            if status is None:
                return await self._deny(
                    ReasonCode.receipt_status_unavailable,
                    "receipt status could not be checked at the issuer", now, ctx_hash, **log,
                )
            if status != "active":
                reason = {"revoked": ReasonCode.revoked, "expired": ReasonCode.expired}.get(
                    status, ReasonCode.receipt_invalid
                )
                return await self._deny(
                    reason, f"receipt status at the issuer: {status}", now, ctx_hash, **log,
                )

        # 4. The standing ceiling: the presenter's scope policy for this
        #    controller in this install's Partner Management.
        try:
            material = await self.partners.get_verification_material(presenter, controller)
        except PolicyUnavailable:
            return await self._deny(
                ReasonCode.policy_unavailable,
                "the presenter's policy could not be read from Partner Management",
                now, ctx_hash, **log,
            )
        if material is None:
            return await self._deny(
                ReasonCode.unknown_partner,
                f"presenter '{presenter}' has no active policy for data_controller "
                f"'{controller}' in Partner Management", now, ctx_hash, **log,
            )
        partner = material["partner"]

        # The same token presented again for this controller → stored decision.
        existing = await self._existing_artefact(jti, controller)
        if existing is not None:
            if (
                existing.partner_id != partner.audience
                or existing.subject_id_value != sub["value"]
            ):
                return await self._deny(
                    ReasonCode.replay, "receipt jti already used for a different consent",
                    now, ctx_hash, partner_id=partner.audience, **log,
                )
            return self._decision_from_artefact(existing, now)

        try:
            obj = ConsentObject(
                jti=jti, subject_id=sub, aud=presenter,
                purpose={"code": claims.get("purpose")},
                data_controller=controller, data_scopes=[str(x) for x in scopes],
                fetch_type="oneshot",
                validity={"valid_from": consent_issued_at, "valid_until": consent_exp},
                issued_at=consent_issued_at,
            )
        except Exception as exc:
            _logger.info("Receipt claims do not form a consent: %s", exc)
            return await self._deny(
                ReasonCode.receipt_invalid, "receipt claims are malformed",
                now, ctx_hash, partner_id=partner.audience, **log,
            )
        grant = obj.grant_for(controller)
        result = self.policy.evaluate(obj, material, parsed.request_context, grant=grant)
        if not result.permit:
            return await self._deny(
                result.reason, result.detail, now, ctx_hash, partner_id=partner.audience,
                policy=material["policy"], **log,
            )
        # 5. The normal decision; the artefact lives no longer than the token.
        return await self._permit(
            obj, grant, partner, result, now, ctx_hash,
            source=ArtefactSource.receipt.value, valid_until=min(exp, consent_exp),
            receipt_jti=jti, receipt_issuer=trusted.issuer,
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_presenter(partner_id: Optional[str]) -> bool:
        return bool(partner_id and partner_id in (_config.receipt_presenters or []))

    @classmethod
    def _caller_terms_mode(cls, parsed: ValidateRequest) -> bool:
        """Caller-terms mode: the caller is a configured token presenter (e.g.
        the Composite) and sends the purpose of the request. The terms it
        sends (purpose, requested scopes, limits) then replace the consenting
        partner's per-registry scope policies."""
        ctx = parsed.request_context
        return bool(ctx is not None and ctx.purpose and cls._is_presenter(parsed.partner_id))

    def _terms_refusal(self, parsed: ValidateRequest):
        """A deny (awaitable) when ``request_context`` carries terms the caller
        may not send: limits or a template from a caller that is not a token
        presenter, or without the purpose they are for. None otherwise."""
        ctx = parsed.request_context
        if ctx is None or not ctx.has_terms:
            return None
        now = datetime.now(timezone.utc)
        ctx_hash = sha256_hex(ctx.model_dump_json().encode("utf-8"))
        if not self._is_presenter(parsed.partner_id):
            return self._deny(
                ReasonCode.caller_terms_not_allowed,
                "request_context.limits / .template are accepted only from a "
                "configured token presenter", now, ctx_hash,
                partner_id=parsed.partner_id, data_controller=parsed.data_controller,
            )
        if not ctx.purpose:
            return self._deny(
                ReasonCode.malformed_object,
                "request_context.purpose is required with limits or a template",
                now, ctx_hash, data_controller=parsed.data_controller,
            )
        return None

    def _stored_terms_refusal(self, terms, request_context, artefact):
        """Caller-terms mode for a stored consent: the checks the policy
        evaluation applies to a signed consent — requested scopes and purpose,
        then the limits' subject id types and maximum validity."""
        purpose_code = (artefact.purpose or {}).get("code")
        refusal = self.policy.check_caller_terms(request_context, purpose_code)
        if refusal is not None:
            return refusal
        if (
            terms.allowed_subject_id_types
            and artefact.subject_id_type not in terms.allowed_subject_id_types
        ):
            return PolicyResult(
                False, ReasonCode.subject_not_allowed,
                f"subject_id_type '{artefact.subject_id_type}' not permitted",
            )
        if terms.max_validity_duration:
            try:
                limit = iso_duration_to_timedelta(terms.max_validity_duration)
            except ValueError:
                limit = None
            if limit is not None and (
                _aware(artefact.valid_until) - _aware(artefact.valid_from)
            ) > limit:
                return PolicyResult(
                    False, ReasonCode.validity_exceeds_policy,
                    "validity longer than the request's limits allow",
                )
        return None

    async def list_decisions(
        self,
        partner_id: Optional[str] = None,
        decision: Optional[str] = None,
        limit: int = 50,
    ) -> list:
        """Recent validation decisions (append-only log), newest first — for the
        admin console's operational/audit status view."""
        async with async_session()() as session:
            query = select(DecisionLog)
            if partner_id:
                query = query.where(DecisionLog.partner_id == partner_id)
            if decision:
                query = query.where(DecisionLog.decision == decision)
            query = query.order_by(DecisionLog.created_at.desc()).limit(min(limit, 200))
            result = await session.execute(query)
            return list(result.scalars().all())

    @staticmethod
    def _subject_mismatch(
        ctx_subject, subject_type: str, subject_value: str, identifiers=None
    ) -> bool:
        """The caller's subject contradicts the consent: same type as the
        consent's subject (or as one of its other ``identifiers``, e.g. the
        Farmer ID), another value. A type the consent does not carry is left
        to the registry."""
        if ctx_subject is None:
            return False
        known = {subject_type: subject_value}
        for i in identifiers or []:
            if isinstance(i, dict) and i.get("type") and i["type"] not in known:
                known[i["type"]] = i.get("value")
        return ctx_subject.type in known and ctx_subject.value != known[ctx_subject.type]

    async def _existing_artefact(
        self, jti: str, controller_id: str
    ) -> Optional[ConsentArtefact]:
        async with async_session()() as session:
            result = await session.execute(
                select(ConsentArtefact).where(
                    ConsentArtefact.object_jti == jti,
                    ConsentArtefact.controller_id == controller_id,
                )
            )
            return result.scalars().first()

    def _decision_from_artefact(self, artefact: ConsentArtefact, now: datetime) -> Decision:
        from ..schemas.common import SubjectId

        status = artefact.status
        if status == ArtefactStatus.active.value and _aware(artefact.valid_until) < now:
            status = ArtefactStatus.expired.value
        if status == ArtefactStatus.revoked.value:
            return Decision(
                decision="deny", reason_code=ReasonCode.revoked,
                detail="consent revoked", data_controller=artefact.controller_id,
                evaluated_at=now,
            )
        if status == ArtefactStatus.expired.value:
            return Decision(
                decision="deny", reason_code=ReasonCode.expired,
                detail="consent expired", data_controller=artefact.controller_id,
                evaluated_at=now,
            )
        return Decision(
            decision="permit", reason_code=ReasonCode.ok,
            consent_id=artefact.id,
            subject_id=SubjectId(type=artefact.subject_id_type, value=artefact.subject_id_value),
            data_controller=artefact.controller_id,
            effective_data_scopes=artefact.effective_data_scopes,
            valid_until=artefact.valid_until, policy_version=artefact.policy_version,
            policy_id=artefact.policy_id, policy_ref=artefact.policy_ref,
            evaluated_at=now,
        )

    async def _permit(
        self, obj, grant, partner, result, now, ctx_hash,
        source: str = ArtefactSource.embedded.value,
        valid_until: Optional[datetime] = None,
        receipt_jti: Optional[str] = None, receipt_issuer: Optional[str] = None,
        template: Optional[str] = None,
    ) -> Decision:
        from ..schemas.common import SubjectId

        artefact = ConsentArtefact(
            template=template,
            subject_id_type=obj.subject_id.type,
            subject_id_value=obj.subject_id.value,
            controller_id=grant.data_controller,
            partner_id=partner.audience,
            purpose=obj.purpose,
            data_scopes=grant.data_scopes,
            effective_data_scopes=result.effective_scopes,
            fetch_type=obj.fetch_type,
            valid_from=_aware(obj.validity.valid_from),
            valid_until=valid_until or _aware(obj.validity.valid_until),
            source=source,
            policy_version=result.policy_version,
            policy_id=result.policy_id, policy_ref=result.policy_ref,
            object_jti=obj.jti,
            status=ArtefactStatus.active.value,
        )
        receipt = self.receipts.build_receipt(artefact)

        async with async_session()() as session:
            session.add(artefact)
            session.add(receipt)
            session.add(
                DecisionLog(
                    partner_id=partner.audience, consent_id=artefact.id, object_jti=obj.jti,
                    data_controller=grant.data_controller,
                    decision="permit", reason_code=ReasonCode.ok.value,
                    policy_version=result.policy_version, policy_id=result.policy_id,
                    policy_ref=result.policy_ref, request_ctx_hash=ctx_hash,
                    receipt_jti=receipt_jti, receipt_issuer=receipt_issuer,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                # A concurrent validation of the same (jti, controller) won the
                # race — return its decision rather than minting a duplicate.
                await session.rollback()
                existing = await self._existing_artefact(obj.jti, grant.data_controller)
                if existing is None:
                    raise
                return self._decision_from_artefact(existing, now)
            await session.refresh(artefact)
            await session.refresh(receipt)

        return Decision(
            decision="permit", reason_code=ReasonCode.ok,
            consent_id=artefact.id, receipt_id=receipt.id,
            subject_id=SubjectId(type=obj.subject_id.type, value=obj.subject_id.value),
            data_controller=grant.data_controller,
            effective_data_scopes=result.effective_scopes,
            valid_until=artefact.valid_until, policy_version=result.policy_version,
            policy_id=result.policy_id, policy_ref=result.policy_ref,
            evaluated_at=now,
        )

    async def _deny(
        self, reason: ReasonCode, detail: Optional[str], now, ctx_hash,
        partner_id: Optional[str] = None, jti: Optional[str] = None,
        policy_version: Optional[int] = None, data_controller: Optional[str] = None,
        receipt_jti: Optional[str] = None, receipt_issuer: Optional[str] = None,
        consent_id: Optional[str] = None, policy=None,
    ) -> Decision:
        """Log and return a deny. ``policy``: the PM policy evaluated, recorded
        with its full reference (version, PM id, kind:key)."""
        refs = policy_fields(policy)
        if policy_version is not None:
            refs["policy_version"] = policy_version
        async with async_session()() as session:
            session.add(
                DecisionLog(
                    partner_id=partner_id, consent_id=consent_id, object_jti=jti,
                    data_controller=data_controller,
                    decision="deny",
                    reason_code=reason.value, detail=detail,
                    **refs, request_ctx_hash=ctx_hash,
                    receipt_jti=receipt_jti, receipt_issuer=receipt_issuer,
                )
            )
            await session.commit()
        return Decision(
            decision="deny", reason_code=reason, detail=detail,
            data_controller=data_controller, evaluated_at=now,
        )
