"""Assisted consent (consent scenario 1): a partner user creates the request in
the partner portal, uploads the subject's signed form and submits it; CM staff
verify the evidence and approve (the consent is created) or reject it.

Every partner call is scoped to the partner (audience) on the user's token: a
request or consent of another partner is "not found".
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import func, or_, select

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactSource,
    ArtefactStatus,
    ConsentArtefact,
    ConsentEvidence,
    ConsentRequest,
    EvidenceKind,
    Partner,
    PartnerPolicy,
    PartnerStatus,
    PolicyStatus,
    RequestMethod,
    RequestStatus,
)
from ..utils.canonical import iso_duration_to_timedelta
from .evidence_service import EvidenceError, EvidenceService
from .lifecycle_service import LifecycleError, LifecycleService
from .partner_service import PartnerService
from .receipt_service import ReceiptService
from .subject_auth_service import SubjectAuthService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_DEFAULT_VALIDITY = timedelta(days=365)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def artefact_status(artefact: ConsentArtefact, now: Optional[datetime] = None) -> str:
    """The artefact's status, with lazy expiry (an active one past valid_until)."""
    now = now or datetime.now(timezone.utc)
    if artefact.status == ArtefactStatus.active.value and _aware(artefact.valid_until) < now:
        return ArtefactStatus.expired.value
    return artefact.status


class AssistedConsentService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.partners = PartnerService.get_component()
        self.lifecycle = LifecycleService.get_component()
        self.receipts = ReceiptService.get_component()
        self.evidence = EvidenceService.get_component()
        self.subject_auth = SubjectAuthService.get_component()

    # ── Partner: what it may ask for ─────────────────────────────────────────

    async def bindings(self, audience: str) -> list:
        """The partner's active bindings that have an active policy."""
        async with async_session()() as session:
            rows = await session.execute(
                select(Partner, PartnerPolicy)
                .join(PartnerPolicy, PartnerPolicy.partner_id == Partner.id)
                .where(
                    Partner.audience == audience,
                    Partner.status == PartnerStatus.active.value,
                    PartnerPolicy.status == PolicyStatus.active.value,
                )
                .order_by(Partner.controller_id)
            )
            return [
                {
                    "data_controller": binding.controller_id,
                    "allowed_data_scopes": list(policy.allowed_data_scopes or []),
                    "allowed_purposes": list(policy.allowed_purposes or []),
                    "allowed_subject_id_types": list(policy.allowed_subject_id_types or []),
                    "max_validity_duration": policy.max_validity_duration,
                    "consent_method": policy.consent_method or "partner_signed",
                }
                for binding, policy in rows.all()
            ]

    # ── Partner: requests ────────────────────────────────────────────────────

    async def create_request(self, audience: str, username: str, data) -> ConsentRequest:
        """An assisted request. Each grant must be within the partner's active
        policy for that controller (scopes, and its purpose, subject id type
        and maximum validity — an empty allowed list means any)."""
        try:
            resolved = await self.lifecycle.resolve_grants(audience, data.grants)
        except LifecycleError as exc:
            # "No active policy" is a policy check here, not a missing resource.
            raise LifecycleError(422, exc.detail) from exc

        given = {i.type.strip(): i.value.strip() for i in data.subject_identifiers}
        missing = [t for t in _config.subject_required_identifiers if t not in given]
        if missing:
            raise LifecycleError(422, f"the subject's {', '.join(missing)} is required")

        now = datetime.now(timezone.utc)
        validity = data.validity
        valid_from = _aware(validity.valid_from) if validity and validity.valid_from else None
        valid_until = _aware(validity.valid_until) if validity and validity.valid_until else None
        if valid_until is not None and valid_until <= now:
            raise LifecycleError(422, "validity.valid_until is in the past")
        purpose_code = data.purpose.code
        for grant, policy in resolved:
            controller = grant["data_controller"]
            if policy.allowed_purposes and purpose_code not in policy.allowed_purposes:
                raise LifecycleError(
                    422, f"purpose_not_allowed for data_controller '{controller}'"
                )
            if (
                policy.allowed_subject_id_types
                and data.subject_id.type not in policy.allowed_subject_id_types
            ):
                raise LifecycleError(
                    422, f"subject_not_allowed for data_controller '{controller}'"
                )
            max_validity = self._max_validity(policy)
            if valid_until is not None and max_validity is not None:
                if valid_until - (valid_from or now) > max_validity:
                    raise LifecycleError(
                        422, f"validity_exceeds_policy for data_controller '{controller}'"
                    )

        grants = [g for g, _ in resolved]
        async with async_session()() as session:
            req = ConsentRequest(
                subject_id_type=data.subject_id.type,
                subject_id_value=data.subject_id.value,
                subject_identifiers=[{"type": t, "value": v} for t, v in given.items()] or None,
                controller_id=grants[0]["data_controller"] if len(grants) == 1 else None,
                # The binding of the first grant (as for a staff grants request).
                partner_id=grants[0]["partner_binding_id"],
                purpose=data.purpose.model_dump(),
                requested_scopes=sorted({s for g in grants for s in g["data_scopes"]}),
                grants=grants,
                valid_from=valid_from,
                valid_until=valid_until,
                status=RequestStatus.pending.value,
                method=RequestMethod.assisted.value,
                use_case=data.use_case,
                created_by=username,
                partner_audience=audience,
            )
            session.add(req)
            await session.commit()
            await session.refresh(req)
            return req

    async def list_requests(
        self, audience: Optional[str] = None, status: Optional[str] = None,
        limit: int = 50, offset: int = 0, submitted_only: bool = False,
    ) -> tuple[int, list]:
        """Assisted requests, newest first. ``audience`` scopes to one partner
        (the portal); ``submitted_only`` hides drafts never submitted (staff)."""
        async with async_session()() as session:
            base = select(ConsentRequest).where(
                ConsentRequest.method.in_(self._PARTNER_METHODS)
            )
            if audience is not None:
                base = base.where(ConsentRequest.partner_audience == audience)
            if status:
                base = base.where(ConsentRequest.status == status)
            if submitted_only:
                base = base.where(ConsentRequest.submitted_at.is_not(None))
            total = int(
                (await session.execute(select(func.count()).select_from(base.subquery())))
                .scalar() or 0
            )
            rows = (
                await session.execute(
                    base.order_by(ConsentRequest.created_at.desc(), ConsentRequest.id)
                    .offset(offset).limit(limit)
                )
            ).scalars().all()
            return total, list(rows)

    async def get_request(
        self, request_id: str, audience: Optional[str] = None
    ) -> Optional[ConsentRequest]:
        """An assisted request; None if missing or (with ``audience``) another
        partner's."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id)
        if not self._visible(req, audience):
            return None
        return req

    # Partner-raised requests: assisted (signed form + staff) and otp (the
    # subject's one-time code, verified through OtpConsentService).
    _PARTNER_METHODS = (RequestMethod.assisted.value, RequestMethod.otp.value)

    @staticmethod
    def _visible(req: Optional[ConsentRequest], audience: Optional[str]) -> bool:
        if req is None or req.method not in AssistedConsentService._PARTNER_METHODS:
            return False
        return audience is None or req.partner_audience == audience

    @staticmethod
    def _assisted_only(req: ConsentRequest) -> None:
        """Evidence and staff verification belong to the assisted flow."""
        if req.method == RequestMethod.otp.value:
            raise LifecycleError(
                409, "an OTP request is confirmed with POST …/verify-otp, not submitted")

    async def consent_ids(self, request_ids: list) -> dict:
        """{request id: consent id} for requests that produced a consent."""
        if not request_ids:
            return {}
        async with async_session()() as session:
            rows = await session.execute(
                select(ConsentArtefact.consent_request_id, ConsentArtefact.id).where(
                    ConsentArtefact.consent_request_id.in_(request_ids)
                )
            )
            return {rid: cid for rid, cid in rows.all()}

    async def submit(self, request_id: str, audience: str) -> ConsentRequest:
        """pending → pending_verification; needs a signed form and, when national
        ID authentication is enabled, the subject's successful authentication."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, audience):
                raise LifecycleError(404, "not_found")
            self._assisted_only(req)
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            forms = (
                await session.execute(
                    select(func.count(ConsentEvidence.id)).where(
                        ConsentEvidence.consent_request_id == request_id,
                        ConsentEvidence.kind == EvidenceKind.signed_form.value,
                    )
                )
            ).scalar()
            if not forms:
                raise LifecycleError(409, "upload the signed consent form before submitting")
            if self.subject_auth.enabled and await self.subject_auth.authenticated(
                session, request_id
            ) is None:
                raise LifecycleError(
                    409, "the subject must authenticate with national ID before submitting"
                )
            req.status = RequestStatus.pending_verification.value
            req.submitted_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(req)
            return req

    async def cancel(self, request_id: str, audience: str) -> ConsentRequest:
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, audience):
                raise LifecycleError(404, "not_found")
            if req.status not in (
                RequestStatus.pending.value, RequestStatus.pending_verification.value
            ):
                raise LifecycleError(409, f"request is '{req.status}'")
            req.status = RequestStatus.cancelled.value
            await session.commit()
            await session.refresh(req)
            return req

    # ── Evidence ─────────────────────────────────────────────────────────────

    async def list_evidence(self, request_id: str) -> list:
        async with async_session()() as session:
            rows = await session.execute(
                select(ConsentEvidence)
                .where(ConsentEvidence.consent_request_id == request_id)
                .order_by(ConsentEvidence.uploaded_at, ConsentEvidence.id)
            )
            return list(rows.scalars().all())

    async def get_evidence(
        self, request_id: str, evidence_id: str, audience: Optional[str] = None
    ) -> Optional[ConsentEvidence]:
        if await self.get_request(request_id, audience) is None:
            return None
        async with async_session()() as session:
            evidence = await session.get(ConsentEvidence, evidence_id)
        if evidence is None or evidence.consent_request_id != request_id:
            return None
        return evidence

    async def add_evidence(
        self, request_id: str, audience: str, username: str, data: bytes,
        content_type: Optional[str], filename: Optional[str], kind: str,
    ) -> ConsentEvidence:
        """Upload a file to a ``pending`` request (checked before the upload and
        again, under the request lock, before the row is saved)."""
        if kind not in {k.value for k in EvidenceKind}:
            raise LifecycleError(422, f"kind must be one of {[k.value for k in EvidenceKind]}")
        req = await self.get_request(request_id, audience)
        if req is None:
            raise LifecycleError(404, "not_found")
        self._assisted_only(req)
        if req.status != RequestStatus.pending.value:
            raise LifecycleError(409, f"request is '{req.status}'")
        self.evidence.require_store()
        ctype = self.evidence.check_file(data, content_type)
        evidence = await self.evidence.put(request_id, data, ctype, filename, kind, username)
        try:
            async with async_session()() as session:
                req = await session.get(ConsentRequest, request_id, with_for_update=True)
                if req.status != RequestStatus.pending.value:
                    raise LifecycleError(409, f"request is '{req.status}'")
                count = (
                    await session.execute(
                        select(func.count(ConsentEvidence.id)).where(
                            ConsentEvidence.consent_request_id == request_id
                        )
                    )
                ).scalar() or 0
                if count >= _config.evidence_max_files_per_request:
                    raise LifecycleError(
                        409,
                        f"a request holds at most {_config.evidence_max_files_per_request} files",
                    )
                session.add(evidence)
                await session.commit()
                await session.refresh(evidence)
        except Exception:
            await self.evidence.delete_object(evidence.storage_key)
            raise
        return evidence

    async def delete_evidence(self, request_id: str, evidence_id: str, audience: str) -> None:
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, audience):
                raise LifecycleError(404, "not_found")
            evidence = await session.get(ConsentEvidence, evidence_id)
            if evidence is None or evidence.consent_request_id != request_id:
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            storage_key = evidence.storage_key
            await session.delete(evidence)
            await session.commit()
        await self.evidence.delete_object(storage_key)

    async def evidence_bytes(self, evidence: ConsentEvidence) -> bytes:
        return await self.evidence.get(evidence)

    # ── Staff verification ───────────────────────────────────────────────────

    async def approve(
        self, request_id: str, verified_by: str, note: Optional[str],
        identifiers_checked: bool = False,
    ) -> tuple[ConsentRequest, ConsentArtefact]:
        """Verify the evidence and create the consent: every requested grant,
        narrowed to the binding's policy as it is now (a grant left with no
        scope is dropped). A request with other subject identifiers (e.g. the
        Farmer ID) needs the approver to confirm them in the registry."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, None):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending_verification.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            if req.subject_identifiers and not identifiers_checked:
                types = ", ".join(i["type"] for i in req.subject_identifiers)
                raise LifecycleError(
                    422, f"confirm the subject's {types} in the registry before approving"
                )

            grants, fetch_type, max_validity = [], None, None
            for rg in req.grants or []:
                binding = await session.get(Partner, rg["partner_binding_id"])
                if binding is None or binding.status != PartnerStatus.active.value:
                    continue
                policy = await self.partners.get_policy(binding.id)
                if policy is None:
                    continue
                effective = sorted(set(rg["data_scopes"]) & set(policy.allowed_data_scopes or []))
                if not effective:
                    continue
                fetch_type = fetch_type or policy.fetch_type
                limit = self._max_validity(policy)
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
                raise LifecycleError(
                    422, "no requested scope is permitted by the partner's current policy"
                )

            kinds = (
                await session.execute(
                    select(ConsentEvidence.kind)
                    .where(ConsentEvidence.consent_request_id == request_id)
                    .distinct()
                )
            ).scalars().all()
            auth = await self.subject_auth.authenticated(session, request_id)
            if self.subject_auth.enabled and auth is None:
                raise LifecycleError(409, "the subject has not authenticated with national ID")
            now = datetime.now(timezone.utc)
            valid_from = _aware(req.valid_from) if req.valid_from else now
            valid_until = (
                _aware(req.valid_until) if req.valid_until
                else valid_from + (max_validity or _DEFAULT_VALIDITY)
            )
            if valid_until <= now:
                raise LifecycleError(409, "the requested validity has already ended")
            assurance = {
                "method": RequestMethod.assisted.value,
                "evidence": sorted(set(kinds)),
                "verified_by": verified_by,
                "verified_at": now.isoformat(),
                "subject_authenticated": auth is not None,
            }
            if req.subject_identifiers:
                # Checked by the approver in the registry (the source of truth).
                assurance["subject_identifiers_checked"] = {
                    "identifiers": [i["type"] for i in req.subject_identifiers],
                    "checked_by": verified_by,
                    "checked_at": now.isoformat(),
                }
            if auth is not None:
                assurance["subject_authentication"] = {
                    "provider": auth.auth_provider,
                    "method": auth.auth_method,
                    "acr": (auth.verified_claims or {}).get("acr"),
                    "authenticated_at": _aware(auth.auth_timestamp).isoformat()
                    if auth.auth_timestamp else None,
                    "auth_context_id": auth.id,
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
                assurance=assurance,
                consent_request_id=req.id,
                status=ArtefactStatus.active.value,
                created_at=now,
            )
            partner = await session.get(Partner, artefact.partner_id)
            receipt = self.receipts.build_receipt(artefact, partner)
            req.status = RequestStatus.approved.value
            req.verified_by = verified_by
            req.verified_at = now
            req.verification_note = note
            session.add(artefact)
            session.add(receipt)
            await session.commit()
            await session.refresh(req)
            await session.refresh(artefact)
            return req, artefact

    async def reject(self, request_id: str, verified_by: str, note: str) -> ConsentRequest:
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, None):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending_verification.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            req.status = RequestStatus.rejected.value
            req.verified_by = verified_by
            req.verified_at = datetime.now(timezone.utc)
            req.verification_note = note
            await session.commit()
            await session.refresh(req)
            return req

    # ── Partner: consents ────────────────────────────────────────────────────

    @staticmethod
    def _partner_consents(audience: str):
        bindings = select(Partner.id).where(Partner.audience == audience)
        return select(ConsentArtefact).where(
            ConsentArtefact.source == ArtefactSource.originated.value,
            ConsentArtefact.partner_id.in_(bindings),
        )

    async def list_consents(
        self, audience: str, status: Optional[str], limit: int, offset: int
    ) -> tuple[int, list, dict]:
        """The partner's originated consents, newest first, with
        {request id: use_case} for those made from a request."""
        now = datetime.now(timezone.utc)
        base = self._partner_consents(audience)
        if status == ArtefactStatus.active.value:
            base = base.where(
                ConsentArtefact.status == status, ConsentArtefact.valid_until >= now
            )
        elif status == ArtefactStatus.expired.value:
            base = base.where(
                or_(
                    ConsentArtefact.status == status,
                    (ConsentArtefact.status == ArtefactStatus.active.value)
                    & (ConsentArtefact.valid_until < now),
                )
            )
        elif status:
            base = base.where(ConsentArtefact.status == status)
        async with async_session()() as session:
            total = int(
                (await session.execute(select(func.count()).select_from(base.subquery())))
                .scalar() or 0
            )
            rows = list(
                (
                    await session.execute(
                        base.order_by(ConsentArtefact.created_at.desc(), ConsentArtefact.id)
                        .offset(offset).limit(limit)
                    )
                ).scalars().all()
            )
        return total, rows, await self.use_cases([a.consent_request_id for a in rows])

    async def get_consent(self, audience: str, consent_id: str):
        async with async_session()() as session:
            artefact = (
                await session.execute(
                    self._partner_consents(audience).where(ConsentArtefact.id == consent_id)
                )
            ).scalars().first()
        if artefact is None:
            return None, {}
        return artefact, await self.use_cases([artefact.consent_request_id])

    async def use_cases(self, request_ids: list) -> dict:
        ids = [r for r in request_ids if r]
        if not ids:
            return {}
        async with async_session()() as session:
            rows = await session.execute(
                select(ConsentRequest.id, ConsentRequest.use_case).where(
                    ConsentRequest.id.in_(ids)
                )
            )
            return {rid: use_case for rid, use_case in rows.all()}

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _max_validity(policy) -> Optional[timedelta]:
        if not policy.max_validity_duration:
            return None
        try:
            return iso_duration_to_timedelta(policy.max_validity_duration)
        except ValueError:
            _logger.warning(
                "Invalid max_validity_duration on policy %s: %s",
                policy.id, policy.max_validity_duration,
            )
            return None


__all__ = ["AssistedConsentService", "EvidenceError", "artefact_status"]
