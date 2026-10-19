"""OTP consent: the partner raises a consent request, the CM sends the subject
a one-time code at once, the subject tells the partner the code and the
partner submits it. A correct code grants the consent immediately — no
evidence upload and no staff verification (that is the assisted flow).

Built on the partner portal's assisted requests: the same request model, the
same policy checks when the request is raised (``AssistedConsentService``),
the same partner scoping (another partner's request is "not found"), and the
consent is built the way staff approval builds it, with ``assurance`` saying
it was confirmed by OTP. The consent is then used like any stored consent:
``/validate`` with its ``consent_id``.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import func, select

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactSource,
    ArtefactStatus,
    AuthContext,
    ConsentArtefact,
    ConsentRequest,
    Partner,
    PartnerStatus,
    RequestMethod,
    RequestStatus,
)
from .assisted_service import AssistedConsentService
from .lifecycle_service import LifecycleError
from .otp_service import OtpError, OtpService
from .partner_service import PartnerService
from .policy_service import PolicyService
from .receipt_service import ReceiptService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_DEFAULT_VALIDITY = timedelta(days=365)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class OtpConsentService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.assisted = AssistedConsentService.get_component()
        self.partners = PartnerService.get_component()
        self.receipts = ReceiptService.get_component()
        self.policy = PolicyService.get_component()

    @property
    def otp(self) -> OtpService:
        return OtpService.get_component()

    @staticmethod
    def _visible(req: Optional[ConsentRequest], audience: Optional[str]) -> bool:
        if req is None or req.method != RequestMethod.otp.value:
            return False
        return audience is None or req.partner_audience == audience

    async def create_request(self, audience: str, username: str, data) -> ConsentRequest:
        """Raise the request (checked against the partner's policies exactly as
        an assisted request is) and send the subject the code."""
        if self.otp.readiness_error():  # refused before anything is stored
            raise LifecycleError(503, "otp_unavailable")
        await self._check_subject_budget(data.subject_id.value)
        req = await self.assisted.create_request(audience, username, data)
        async with async_session()() as session:
            req = await session.get(ConsentRequest, req.id, with_for_update=True)
            req.method = RequestMethod.otp.value
            try:
                await self.otp.issue(req, req.subject_id_value)
            except OtpError as exc:
                req.status = RequestStatus.cancelled.value
                await session.commit()
                raise LifecycleError(exc.status, exc.reason) from exc
            await session.commit()
            await session.refresh(req)
        _logger.info("OTP consent request %s raised by %s (%s); code sent",
                     req.id, audience, username)
        return req

    @staticmethod
    async def _check_subject_budget(subject_id_value: str) -> None:
        """Codes sent to one subject, by any partner, within the window: the
        sends of every OTP request that had a code sent in it (an over-count,
        never an under-count)."""
        since = datetime.now(timezone.utc) - timedelta(seconds=_config.otp_subject_window_sec)
        async with async_session()() as session:
            sent = (
                await session.execute(
                    select(func.coalesce(func.sum(ConsentRequest.otp_send_count), 0)).where(
                        ConsentRequest.method == RequestMethod.otp.value,
                        ConsentRequest.subject_id_value == subject_id_value,
                        ConsentRequest.otp_last_sent_at >= since,
                    )
                )
            ).scalar_one()
        if sent >= _config.otp_max_sends_per_subject:
            raise LifecycleError(429, "subject_otp_limit_reached")

    async def get_request(self, request_id: str, audience: str) -> Optional[ConsentRequest]:
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id)
        return req if self._visible(req, audience) else None

    async def resend_otp(self, request_id: str, audience: str) -> ConsentRequest:
        """A new code for a still-pending request (the old one stops working)."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, audience):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            # A new code resets the attempts for that code only; these budgets
            # are what stop resends from undoing a lockout.
            now = datetime.now(timezone.utc)
            last = _aware(req.otp_last_sent_at) if req.otp_last_sent_at else None
            if last is not None and now < last + timedelta(seconds=_config.otp_resend_cooldown_sec):
                raise LifecycleError(429, "resend_too_soon")
            if (req.otp_send_count or 0) >= _config.otp_max_sends_per_request:
                raise LifecycleError(429, "resend_limit_reached")
            await self._check_subject_budget(req.subject_id_value)
            try:
                await self.otp.issue(req, req.subject_id_value)
            except OtpError as exc:
                raise LifecycleError(exc.status, exc.reason) from exc
            await session.commit()
            await session.refresh(req)
            return req

    async def verify_otp(
        self, request_id: str, audience: str, username: str, code: str
    ) -> tuple[ConsentRequest, ConsentArtefact]:
        """Check the code and, if it is right, create the consent: every
        requested grant, narrowed to the binding's policy as it is now (a grant
        left with no scope is dropped)."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, audience):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            try:
                await self.otp.verify(req, code)
            except OtpError as exc:
                status, reason = exc.status, exc.reason
                if (req.otp_failures_total or 0) >= _config.otp_max_failures_per_request:
                    # Out of guesses over all its codes: the request is closed
                    # and the partner must raise a new one.
                    req.status = RequestStatus.cancelled.value
                    req.verification_note = "OTP failure limit reached"
                    _logger.warning("OTP consent request %s cancelled: failure limit", req.id)
                    status, reason = 429, "otp_failure_limit_reached"
                await session.commit()  # keep the attempt counts
                raise LifecycleError(status, reason) from exc

            now = datetime.now(timezone.utc)
            valid_from = _aware(req.valid_from) if req.valid_from else now
            # The whole current policy applies, not only its scopes: a grant
            # whose purpose, subject id type or requested validity the policy
            # no longer allows is dropped. Without an explicit end the validity
            # is derived from the remaining policies below, so it always fits.
            until = _aware(req.valid_until) if req.valid_until else valid_from
            grants, fetch_type, max_validity, refusal = [], None, None, None
            for rg in req.grants or []:
                binding = await session.get(Partner, rg["partner_binding_id"])
                if binding is None or binding.status != PartnerStatus.active.value:
                    continue
                policy = await self.partners.get_policy(binding.id)
                if policy is None:
                    continue
                refused = self.policy.terms_refusal(
                    policy, subject_id_type=req.subject_id_type, purpose=req.purpose,
                    valid_from=valid_from, valid_until=until,
                )
                if refused is not None:
                    refusal = f"{refused.reason.value} for data_controller '{rg['data_controller']}'"
                    continue
                effective = sorted(set(rg["data_scopes"]) & set(policy.allowed_data_scopes or []))
                if not effective:
                    continue
                fetch_type = fetch_type or policy.fetch_type
                limit = AssistedConsentService._max_validity(policy)
                if limit is not None:
                    max_validity = limit if max_validity is None else min(max_validity, limit)
                grants.append(
                    {
                        "data_controller": rg["data_controller"],
                        "data_scopes": rg["data_scopes"],
                        "granted_scopes": sorted(rg["data_scopes"]),
                        "effective_data_scopes": effective,
                        "partner_binding_id": binding.id,
                        "policy_version": policy.version,
                    }
                )
            if not grants:
                await session.commit()  # the code is spent either way
                raise LifecycleError(
                    422, refusal or "no requested scope is permitted by the partner's current policy"
                )

            valid_until = (
                _aware(req.valid_until) if req.valid_until
                else valid_from + (max_validity or _DEFAULT_VALIDITY)
            )
            if valid_until <= now:
                await session.commit()
                raise LifecycleError(409, "the requested validity has already ended")

            ctx = AuthContext(
                consent_request_id=req.id,
                auth_provider=req.otp_provider or "otp",
                auth_method="otp",
                auth_timestamp=req.otp_verified_at or now,
                issuer=req.otp_provider or "otp",
                # No ID token in this path; the column is not nullable, so it
                # holds a hash naming the act. token_validated stays False.
                id_token_hash=hashlib.sha256(f"otp:{req.id}".encode("utf-8")).hexdigest(),
                token_validated=False,
                verified_claims={
                    "auth_method": "otp",
                    "otp_provider": req.otp_provider,
                    "otp_channel": req.otp_channel,
                    "otp_reference": req.otp_reference,
                    "subject_id_value": req.subject_id_value,
                    "submitted_by": username,
                },
            )
            session.add(ctx)
            await session.flush()

            assurance = {
                "method": RequestMethod.otp.value,
                "evidence": [],
                "verified_by": None,
                "verified_at": now.isoformat(),
                "subject_authenticated": True,
                "subject_authentication": {
                    "provider": req.otp_provider,
                    "method": "otp",
                    "channel": req.otp_channel,
                    "authenticated_at": _aware(req.otp_verified_at or now).isoformat(),
                    "auth_context_id": ctx.id,
                    "submitted_by": username,
                },
            }
            artefact = ConsentArtefact(
                subject_id_type=req.subject_id_type,
                subject_id_value=req.subject_id_value,
                subject_identifiers=req.subject_identifiers,
                controller_id=grants[0]["data_controller"] if len(grants) == 1 else None,
                partner_id=grants[0]["partner_binding_id"],
                purpose=req.purpose,
                data_scopes=req.requested_scopes,
                effective_data_scopes=sorted(
                    {s for g in grants for s in g["effective_data_scopes"]}
                ),
                grants=grants,
                fetch_type=fetch_type or "oneshot",
                valid_from=valid_from,
                valid_until=valid_until,
                source=ArtefactSource.originated.value,
                policy_version=grants[0]["policy_version"] if len(grants) == 1 else None,
                auth_context_id=ctx.id,
                assurance=assurance,
                consent_request_id=req.id,
                status=ArtefactStatus.active.value,
                created_at=now,
            )
            partner = await session.get(Partner, artefact.partner_id)
            receipt = self.receipts.build_receipt(artefact, partner)
            req.status = RequestStatus.approved.value
            req.verified_at = now
            session.add(artefact)
            session.add(receipt)
            await session.commit()
            await session.refresh(req)
            await session.refresh(artefact)
        _logger.info("OTP consent request %s: code verified, consent %s granted",
                     request_id, artefact.id)
        return req, artefact


__all__ = ["OtpConsentService"]
