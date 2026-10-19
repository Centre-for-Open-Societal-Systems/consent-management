import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from openg2p_fastapi_common.utils.crypto import build_crypto_helper
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactSource,
    ArtefactStatus,
    ConsentArtefact,
    ConsentReceipt,
    DecisionLog,
    Partner,
)
from ..schemas.common import ReasonCode
from ..schemas.verification import ConsentObject, Decision, ValidateRequest
from ..utils.canonical import b64url_decode, sha256_hex
from .exchange_service import RECEIPT_TYP, ExchangeService, to_datetime
from .partner_service import PartnerService
from .policy_service import PolicyService
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
        self.crypto_helper = build_crypto_helper(backend=_config.crypto_backend)
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
        if parsed.consent_id:
            return await self._validate_stored(parsed)
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
        if isinstance(jws_header, dict) and jws_header.get("typ") == RECEIPT_TYP:
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

        # 2. Known party — the partner's binding to this controller + the
        #    binding's policy (cached). Policies are per (audience, controller).
        material = await self.partners.get_verification_material(obj.aud, controller)
        if material is None:
            return await self._deny(
                ReasonCode.unknown_partner,
                f"partner not onboarded for data_controller '{controller}', or suspended",
                now, ctx_hash, jti=obj.jti, data_controller=controller,
            )
        partner = material["partner"]
        policy = material["policy"]
        policy_version = policy.version if policy else None

        # 3. Signature — verify the consent JWS against the partner's key from
        # Partner Management. The shared helper reads the kid from the JWS header,
        # fetches the key for the partner's PM reference (partner_mgmt_id, falling
        # back to audience), enforces algorithm safety, and verifies.
        reference_id = partner.partner_mgmt_id or partner.audience
        alg = jws_header.get("alg")
        if policy and policy.allowed_signing_algs and alg not in policy.allowed_signing_algs:
            return await self._deny(
                ReasonCode.signature_invalid, "signing algorithm not permitted",
                now, ctx_hash, partner_id=partner.id, jti=obj.jti, data_controller=controller,
                policy_version=policy_version,
            )
        try:
            verified = await self.crypto_helper.verify_jwt(jws, km_ref_id=reference_id)
        except Exception as exc:
            _logger.warning("Consent JWS verification error: %s", exc)
            verified = False
        if not verified:
            return await self._deny(
                ReasonCode.signature_invalid,
                "signature did not verify (or no verifying key from partner management)",
                now, ctx_hash, partner_id=partner.id, jti=obj.jti, data_controller=controller,
                policy_version=policy_version,
            )

        # A partner-signed consent is not enough for an "otp" binding — also
        # when the same jti was permitted before the policy changed.
        refused = self.policy.consent_method_refusal(policy, otp_confirmed=False) if policy else None
        if refused is not None:
            return await self._deny(
                refused.reason, refused.detail, now, ctx_hash, partner_id=partner.id,
                jti=obj.jti, data_controller=controller, policy_version=policy_version,
            )

        # Idempotency: the same object (jti) presented again for the same
        # controller returns its existing decision. Keyed per (jti, controller),
        # so one consent validated by two registries gets two decisions and
        # receipts. Checked only AFTER the binding and the signature: a stored
        # decision is never handed to an unsigned or altered object reusing a
        # known jti. The stored consent must also be this one — same partner
        # binding, subject and scopes — or the jti is being reused.
        existing = await self._existing_artefact(obj.jti, controller)
        if existing is not None:
            if (
                existing.partner_id != partner.id
                or existing.subject_id_type != obj.subject_id.type
                or existing.subject_id_value != obj.subject_id.value
                or sorted(existing.data_scopes or []) != sorted(grant.data_scopes or [])
            ):
                return await self._deny(
                    ReasonCode.replay, "jti already used for a different consent",
                    now, ctx_hash, partner_id=partner.id, jti=obj.jti, data_controller=controller,
                    policy_version=policy_version,
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
                now, ctx_hash, partner_id=partner.id, jti=obj.jti, data_controller=controller,
                policy_version=policy_version,
            )

        # 4–8. Policy evaluation (audience, subject, purpose, scope, validity).
        result = self.policy.evaluate(obj, material, parsed.request_context, grant=grant)
        if not result.permit:
            return await self._deny(
                result.reason, result.detail, now, ctx_hash,
                partner_id=partner.id, jti=obj.jti, policy_version=result.policy_version,
                data_controller=controller,
            )

        # Permit — mint canonical artefact + signed receipt + decision log for
        # this (consent, controller).
        return await self._permit(
            obj, grant, partner, result, now, ctx_hash
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
            if isinstance(jws_header, dict) and jws_header.get("typ") == RECEIPT_TYP:
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

    # ── A stored (originated) consent presented by its ID ───────────────────

    async def _validate_stored(self, parsed: ValidateRequest) -> Decision:
        """``consent_id``: a consent the CM holds (originated — e.g. an assisted
        consent verified by staff). Checks the consent's state, the partner
        that obtained it and the subject; each grant is narrowed to the
        partner's CURRENT policy for its controller (and requested_scopes).
        With ``issue_receipts``, signs one consent token per granted controller in
        exactly the format used for a partner-signed consent."""
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

        async with async_session()() as session:
            artefact = await session.get(ConsentArtefact, consent_id)
            binding = (
                await session.get(Partner, artefact.partner_id) if artefact is not None else None
            )
        if artefact is None or artefact.source != ArtefactSource.originated.value:
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
        audience = binding.audience if binding is not None else None
        if not partner_aud or partner_aud != audience:
            return await self._deny(
                ReasonCode.partner_mismatch,
                "consent_partner_id is not the partner the consent was given to",
                now, ctx_hash, **log,
            )
        ctx_subject = parsed.request_context.subject_id if parsed.request_context else None
        if self._subject_mismatch(
            ctx_subject, artefact.subject_id_type, artefact.subject_id_value
        ):
            return await self._deny(
                ReasonCode.subject_mismatch,
                "request_context.subject_id does not match the consent subject",
                now, ctx_hash, **log,
            )

        stored = artefact.grants or [
            {
                "data_controller": artefact.controller_id,
                "effective_data_scopes": artefact.effective_data_scopes or [],
            }
        ]
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
        granted: dict[str, tuple[list, object, Optional[int]]] = {}
        refused = None
        for g in stored:
            controller = g.get("data_controller")
            if not controller:
                continue
            material = await self.partners.get_verification_material(audience, controller)
            policy = material["policy"] if material else None
            if policy is None:
                continue
            # The current policy's subject id types, purposes and validity
            # ceiling apply to a stored consent as they do to a signed one.
            refusal = self.policy.consent_method_refusal(
                policy, otp_confirmed=(artefact.assurance or {}).get("method") == "otp",
            ) or self.policy.terms_refusal(
                policy, subject_id_type=artefact.subject_id_type, purpose=artefact.purpose,
                valid_from=artefact.valid_from, valid_until=artefact.valid_until,
            )
            if refusal is not None:
                refused = refusal
                continue
            effective = set(g.get("effective_data_scopes") or []) & set(
                policy.allowed_data_scopes or []
            )
            if requested is not None:
                effective &= requested
            if effective:
                granted[controller] = (sorted(effective), material["partner"], policy.version)
        if not granted and refused is not None:
            return await self._deny(
                refused.reason, refused.detail,
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
                decision="permit", reason_code=ReasonCode.ok, consent_id=artefact.id,
                subject_id=subject, data_controller=controller,
                effective_data_scopes=scopes, valid_until=artefact.valid_until,
                policy_version=version, evaluated_at=now,
            )
            for controller, (scopes, _, version) in granted.items()
        }
        async with async_session()() as session:
            for controller, (_, partner, version) in granted.items():
                session.add(
                    DecisionLog(
                        partner_id=partner.id, consent_id=artefact.id,
                        data_controller=controller, decision="permit",
                        reason_code=ReasonCode.ok.value, policy_version=version,
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
        issuer's JWKS, check audience/presenter/time (and its status at the
        issuer), then apply THIS CM's standing policy for the presenter at the
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

        # 3. Status at the issuer (revoked consent → revoked token).
        if (_config.receipt_status_check or "always").lower() != "never":
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

        # 4. This CM's standing policy for the presenter at this controller.
        material = await self.partners.get_verification_material(presenter, controller)
        if material is None:
            return await self._deny(
                ReasonCode.unknown_partner,
                f"presenter '{presenter}' not onboarded for data_controller "
                f"'{controller}', or suspended", now, ctx_hash, **log,
            )
        partner = material["partner"]

        # The same token presented again for this controller → stored decision.
        existing = await self._existing_artefact(jti, controller)
        if existing is not None:
            if existing.partner_id != partner.id or existing.subject_id_value != sub["value"]:
                return await self._deny(
                    ReasonCode.replay, "receipt jti already used for a different consent",
                    now, ctx_hash, partner_id=partner.id, **log,
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
                now, ctx_hash, partner_id=partner.id, **log,
            )
        grant = obj.grant_for(controller)
        result = self.policy.evaluate(obj, material, parsed.request_context, grant=grant)
        if not result.permit:
            return await self._deny(
                result.reason, result.detail, now, ctx_hash, partner_id=partner.id,
                policy_version=result.policy_version, **log,
            )
        # 5. The normal decision; the artefact lives no longer than the token.
        return await self._permit(
            obj, grant, partner, result, now, ctx_hash,
            source=ArtefactSource.receipt.value, valid_until=min(exp, consent_exp),
            receipt_jti=jti, receipt_issuer=trusted.issuer,
        )

    # ── helpers ──────────────────────────────────────────────────────────────

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
    def _subject_mismatch(ctx_subject, subject_type: str, subject_value: str) -> bool:
        return (
            ctx_subject is not None
            and ctx_subject.type == subject_type
            and ctx_subject.value != subject_value
        )

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
            evaluated_at=now,
        )

    async def _permit(
        self, obj, grant, partner, result, now, ctx_hash,
        source: str = ArtefactSource.embedded.value,
        valid_until: Optional[datetime] = None,
        receipt_jti: Optional[str] = None, receipt_issuer: Optional[str] = None,
    ) -> Decision:
        from ..schemas.common import SubjectId

        artefact = ConsentArtefact(
            subject_id_type=obj.subject_id.type,
            subject_id_value=obj.subject_id.value,
            controller_id=grant.data_controller,
            partner_id=partner.id,
            purpose=obj.purpose,
            data_scopes=grant.data_scopes,
            effective_data_scopes=result.effective_scopes,
            fetch_type=obj.fetch_type,
            valid_from=_aware(obj.validity.valid_from),
            valid_until=valid_until or _aware(obj.validity.valid_until),
            source=source,
            policy_version=result.policy_version,
            object_jti=obj.jti,
            status=ArtefactStatus.active.value,
        )
        receipt = self.receipts.build_receipt(artefact, partner)

        async with async_session()() as session:
            session.add(artefact)
            session.add(receipt)
            session.add(
                DecisionLog(
                    partner_id=partner.id, consent_id=artefact.id, object_jti=obj.jti,
                    data_controller=grant.data_controller,
                    decision="permit", reason_code=ReasonCode.ok.value,
                    policy_version=result.policy_version, request_ctx_hash=ctx_hash,
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
            evaluated_at=now,
        )

    async def _deny(
        self, reason: ReasonCode, detail: Optional[str], now, ctx_hash,
        partner_id: Optional[str] = None, jti: Optional[str] = None,
        policy_version: Optional[int] = None, data_controller: Optional[str] = None,
        receipt_jti: Optional[str] = None, receipt_issuer: Optional[str] = None,
        consent_id: Optional[str] = None,
    ) -> Decision:
        async with async_session()() as session:
            session.add(
                DecisionLog(
                    partner_id=partner_id, consent_id=consent_id, object_jti=jti,
                    data_controller=data_controller,
                    decision="deny",
                    reason_code=reason.value, detail=detail,
                    policy_version=policy_version, request_ctx_hash=ctx_hash,
                    receipt_jti=receipt_jti, receipt_issuer=receipt_issuer,
                )
            )
            await session.commit()
        return Decision(
            decision="deny", reason_code=reason, detail=detail,
            data_controller=data_controller, evaluated_at=now,
        )
