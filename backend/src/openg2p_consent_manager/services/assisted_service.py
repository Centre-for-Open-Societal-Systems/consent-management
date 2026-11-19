"""Assisted consent (consent scenario 1): a partner user creates the request in
the partner portal, uploads the subject's signed form and submits it; CM staff
verify the evidence and approve (the consent is created) or reject it.

Every partner call is scoped to the partner (audience) on the user's token: a
request or consent of another partner is "not found".
"""
import hashlib
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
    RequestMethod,
    RequestStatus,
)
from ..utils.canonical import iso_duration_to_timedelta
from .evidence_service import EvidenceError, EvidenceService
from .lifecycle_service import LifecycleError, LifecycleService
from .master_data_client import MasterDataClient
from .partner_service import PartnerService
from .pm_policy_client import PolicyUnavailable, common_policy_fields, policy_fields
from .policy_service import template_fields
from .receipt_service import ReceiptService
from .subject_auth_service import SubjectAuthService
from .template_client import (
    EXCHANGE_NEEDS_TEMPLATE,
    TemplateSourceUnavailable,
    consent_by_template,
)

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


ASSISTED_CONFIRMATION = "assisted_confirmation"


def _auth_summary(auth) -> dict:
    """The national-ID login as recorded on a consent's assurance."""
    return {
        "provider": auth.auth_provider,
        "method": auth.auth_method,
        "acr": (auth.verified_claims or {}).get("acr"),
        "authenticated_at": _aware(auth.auth_timestamp).isoformat()
        if auth.auth_timestamp else None,
        "auth_context_id": auth.id,
    }


def confirmation_statement(req, grants: list, valid_until: datetime) -> str:
    """The text the subject agrees to by assisted confirmation; its SHA-256 is
    kept on the consent. Day precision, so it reads the same when shown and
    when confirmed."""
    purpose = (req.purpose or {}).get("code") or "-"
    data = "; ".join(
        f"{g['data_controller']}: {', '.join(g['effective_data_scopes'])}" for g in grants
    )
    return (
        f"I agree to share my data with {req.partner_audience or req.partner_id} "
        f"for the purpose '{purpose}' until {valid_until.date().isoformat()}: {data}."
    )


def _artefact(req, grants, fetch_type, valid_from, valid_until, assurance, ceiling, now):
    """The consent made from an assisted request."""
    return ConsentArtefact(
        subject_id_type=req.subject_id_type,
        subject_id_value=req.subject_id_value,
        subject_identifiers=req.subject_identifiers,
        controller_id=grants[0]["data_controller"] if len(grants) == 1 else None,
        partner_id=req.partner_audience or req.partner_id,
        purpose=req.purpose,
        data_scopes=req.requested_scopes,
        effective_data_scopes=sorted({s for g in grants for s in g["effective_data_scopes"]}),
        grants=grants,
        fetch_type=fetch_type or "oneshot",
        valid_from=valid_from,
        valid_until=valid_until,
        source=ArtefactSource.originated.value,
        **common_policy_fields(grants),
        assurance=assurance,
        consent_request_id=req.id,
        template=req.template if ceiling is not None else None,
        status=ArtefactStatus.active.value,
        created_at=now,
    )


class AssistedConsentService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.partners = PartnerService.get_component()
        self.lifecycle = LifecycleService.get_component()
        self.receipts = ReceiptService.get_component()
        self.evidence = EvidenceService.get_component()
        self.subject_auth = SubjectAuthService.get_component()
        self.master_data = MasterDataClient.get_component()

    # ── Partner: what it may ask for ─────────────────────────────────────────

    async def access(self, audience: str) -> dict:
        """What the partner may ask for. Exchange role: the consent templates
        the template source offers the partner (purpose, per-registry scopes
        and limits). Otherwise: its active scope policies, one per data
        controller. Raises LifecycleError(503) when PM or (exchange role) the
        template source cannot be reached."""
        if consent_by_template():
            return {"consent_by": "template", "templates": await self._template_access(audience)}
        try:
            policies = await self.partners.scope_policies(audience)
        except PolicyUnavailable as exc:
            raise LifecycleError(
                503, "the partner's policies could not be read from Partner Management"
            ) from exc
        return {
            "consent_by": "scopes",
            "scope_policies": [
                {
                    "data_controller": policy.data_controller,
                    "allowed_data_scopes": list(policy.allowed_data_scopes or []),
                    "allowed_purposes": list(policy.allowed_purposes or []),
                    "allowed_subject_id_types": list(policy.allowed_subject_id_types or []),
                    "max_validity_duration": policy.max_validity_duration,
                    "policy_version": policy.version,
                    "allow_assisted_confirmation": bool(
                        getattr(policy, "allow_assisted_confirmation", False)
                    ),
                }
                for policy in policies
            ],
        }

    async def _template_access(self, audience: str) -> list:
        """One entry per consent template offered to the partner."""
        try:
            templates = await self.lifecycle.templates.list_templates(audience)
        except TemplateSourceUnavailable as exc:
            raise LifecycleError(
                503, "the consent template source could not be reached"
            ) from exc
        out = []
        for template in sorted(templates, key=lambda t: t.code):
            entry = {
                "code": template.code,
                "title": template.title,
                "description": template.description,
                "purpose": template.purpose,
                "grants": template.grants,
                "allowed_subject_id_types": list(template.limits.allowed_subject_id_types),
                "max_validity_duration": template.limits.max_validity_duration,
                "allow_partner_consents": template.limits.allow_partner_consents,
                "allow_assisted_confirmation": template.limits.allow_assisted_confirmation,
            }
            try:
                self.lifecycle.check_usable(template)
            except LifecycleError as exc:
                entry.update(available=False, detail=exc.detail)
            out.append(entry)
        return out

    # ── Partner: requests ────────────────────────────────────────────────────

    async def create_request(self, audience: str, username: str, data) -> ConsentRequest:
        """An assisted request. Each grant must be within the partner's active
        policy for that controller (scopes, and its purpose, subject id type
        and maximum validity — an empty allowed list means any).

        Exchange role: always made from a consent ``template`` offered to the
        partner; its purpose, scopes (``grants``, if given, chosen within them)
        and limits (subject id types, maximum validity) apply."""
        # Exchange role: always from a template. Elsewhere: grants checked
        # against scope policies; template a label.
        by_template = consent_by_template()
        if by_template and not data.template:
            raise LifecycleError(400, EXCHANGE_NEEDS_TEMPLATE)
        if not by_template and data.grants is None:
            raise LifecycleError(422, "'grants' is required")
        if not by_template and data.purpose is None:
            raise LifecycleError(422, "'purpose' is required with 'grants'")
        await self._check_code_lists(data, by_template)
        resolved = (
            None if by_template else await self.lifecycle.resolve_grants(audience, data.grants)
        )

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

        if by_template:
            template, grants = await self.lifecycle.resolve_template(
                audience, data.template, data.subject_id.type,
                data.purpose.code if data.purpose else None, valid_from, valid_until,
                chosen=data.grants,
            )
            purpose = {"code": template.purpose}
            template_code = template.code
        else:
            self._check_scope_policies(resolved, data, valid_from, valid_until, now)
            grants = [g for g, _ in resolved]
            purpose = data.purpose.model_dump()
            template_code = data.template

        async with async_session()() as session:
            req = ConsentRequest(
                subject_id_type=data.subject_id.type,
                subject_id_value=data.subject_id.value,
                subject_identifiers=[{"type": t, "value": v} for t, v in given.items()] or None,
                controller_id=grants[0]["data_controller"] if len(grants) == 1 else None,
                partner_id=audience,
                purpose=purpose,
                requested_scopes=sorted({s for g in grants for s in g["data_scopes"]}),
                grants=grants,
                valid_from=valid_from,
                valid_until=valid_until,
                status=RequestStatus.pending.value,
                method=RequestMethod.assisted.value,
                template=template_code,
                created_by=username,
                partner_audience=audience,
            )
            session.add(req)
            await session.commit()
            await session.refresh(req)
            return req

    async def _check_code_lists(self, data, by_template: bool) -> None:
        """The subject ID type and (not from a template: the template's purpose
        is the template source's) the purpose must be in their Master Data
        lists when those are loaded; otherwise anything is accepted."""
        checks = [("subject_id_types", "subject ID type", data.subject_id.type)]
        if not by_template and data.purpose is not None:
            checks.append(("purposes", "purpose", data.purpose.code))
        for kind, what, value in checks:
            codes = await self.master_data.code_list(kind)
            if codes is not None and value not in codes.codes:
                raise LifecycleError(
                    400,
                    f"unknown {what} '{value}': not in Master Data list {codes.list_code} "
                    f"(one of {', '.join(sorted(codes.codes))})",
                )

    def _check_scope_policies(self, resolved, data, valid_from, valid_until, now) -> None:
        """Each grant's scope policy: purpose, subject id type, maximum validity."""
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

    async def list_requests(
        self, audience: Optional[str] = None, status: Optional[str] = None,
        limit: int = 50, offset: int = 0, submitted_only: bool = False,
    ) -> tuple[int, list]:
        """Assisted requests, newest first. ``audience`` scopes to one partner
        (the portal); ``submitted_only`` hides drafts never submitted (staff)."""
        async with async_session()() as session:
            base = select(ConsentRequest).where(
                ConsentRequest.method == RequestMethod.assisted.value
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

    @staticmethod
    def _visible(req: Optional[ConsentRequest], audience: Optional[str]) -> bool:
        if req is None or req.method != RequestMethod.assisted.value:
            return False
        return audience is None or req.partner_audience == audience

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
    ) -> tuple[ConsentRequest, ConsentArtefact]:
        """Verify the evidence and create the consent: every requested grant,
        narrowed to the partner's policy as it is now (a grant left with no
        scope is dropped). Other subject identifiers (e.g. the Farmer ID) are
        carried as entered, not checked here: a registry returns only records
        of the consent's subject, so a wrong one finds no data."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, None):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending_verification.value:
                raise LifecycleError(409, f"request is '{req.status}'")

            grants, fetch_type, max_validity, ceiling, _ = await self._effective_grants(req)
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
            if auth is not None:
                assurance["subject_authentication"] = _auth_summary(auth)
            artefact = _artefact(
                req, grants, fetch_type, valid_from, valid_until, assurance, ceiling, now
            )
            receipt = self.receipts.build_receipt(artefact)
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

    async def _effective_grants(self, req):
        """The request's grants narrowed to what the partner may have now:
        ``(grants, fetch_type, max_validity, ceiling, policies)`` (``policies``:
        each grant's scope policy, None from a template). A request made from a
        consent template (exchange role) is narrowed to the template's scopes
        as they are now (``ceiling``: (template, {controller: scopes}), and the
        template must still be offered to the partner), else to the partner's
        scope policies (``ceiling`` None). A grant left with no scope is
        dropped; none left is LifecycleError(422)."""
        grants, fetch_type, max_validity = [], None, None
        ceiling = await self.lifecycle.template_ceiling(req)
        if ceiling is not None:
            max_validity = self.lifecycle.max_validity(ceiling[0])
        for rg in req.grants or []:
            if ceiling is not None:
                policy = None
                allowed = ceiling[1].get(rg["data_controller"], set())
            else:
                policy = await self.lifecycle.current_policy(
                    req.partner_audience or req.partner_id, rg["data_controller"]
                )
                if policy is None:
                    continue
                allowed = set(policy.allowed_data_scopes or [])
            effective = sorted(set(rg["data_scopes"]) & allowed)
            if not effective:
                continue
            if policy is not None:
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
                    "_policy": policy,
                    **(
                        template_fields(ceiling[0].code) if ceiling is not None
                        else policy_fields(policy)
                    ),
                }
            )
        if not grants:
            raise LifecycleError(
                422,
                f"no requested scope is in consent template '{req.template}' now"
                if ceiling is not None else
                "no requested scope is permitted by the partner's current policy",
            )
        policies = [g.pop("_policy") for g in grants]
        return grants, fetch_type, max_validity, ceiling, policies

    # ── Assisted confirmation (consent scenario 0) ───────────────────────────

    async def _confirmation_terms(self, req, now: datetime):
        """The consent an assisted confirmation of ``req`` makes, after checking
        it is allowed: on for the install (with national-ID login), and allowed
        by the consent template (exchange role) or by the scope policy of every
        registry. Every requested scope must be granted: no scope is dropped.
        The validity is the install's default, capped by the template's or the
        policies' longest and by a shorter one on the request.
        Returns (grants, fetch_type, valid_from, valid_until, ceiling, statement)."""
        if not _config.assisted_confirmation_enabled:
            raise LifecycleError(403, "assisted confirmation is not enabled")
        if not self.subject_auth.enabled:
            raise LifecycleError(
                409, "assisted confirmation needs national-ID authentication of the subject, "
                "which is not enabled"
            )
        grants, fetch_type, max_validity, ceiling, policies = await self._effective_grants(req)
        if ceiling is not None:
            allowed = ceiling[0].limits.allow_assisted_confirmation
        else:
            allowed = all(
                getattr(p, "allow_assisted_confirmation", False) for p in policies
            )
        if not allowed:
            raise LifecycleError(
                403,
                "the partner's policy does not allow assisted confirmation"
                + (f" for consent template '{req.template}'" if ceiling is not None else ""),
            )
        requested = {g["data_controller"]: set(g["data_scopes"]) for g in req.grants or []}
        if any(set(g["effective_data_scopes"]) != requested.get(g["data_controller"])
               for g in grants) or len(grants) != len(requested):
            raise LifecycleError(
                422, "not every requested scope is allowed now: raise a new request"
            )
        valid_from = now
        duration = timedelta(days=_config.assisted_confirmation_validity_days)
        if max_validity is not None:
            duration = min(duration, max_validity)
        valid_until = valid_from + duration
        if req.valid_until is not None:
            valid_until = min(valid_until, _aware(req.valid_until))
        if valid_until <= now:
            raise LifecycleError(409, "the requested validity has already ended")
        return (
            grants, fetch_type, valid_from, valid_until, ceiling,
            confirmation_statement(req, grants, valid_until),
        )

    async def confirmation(self, request_id: str, audience: str) -> dict:
        """The terms of an assisted confirmation of a ``pending`` request: the
        exact statement the subject agrees to, and the consent it makes."""
        req = await self.get_request(request_id, audience)
        if req is None:
            raise LifecycleError(404, "not_found")
        if req.status != RequestStatus.pending.value:
            raise LifecycleError(409, f"request is '{req.status}'")
        now = datetime.now(timezone.utc)
        grants, _, valid_from, valid_until, _, statement = await self._confirmation_terms(
            req, now
        )
        return {
            "statement": statement,
            "partner_id": req.partner_audience or req.partner_id,
            "purpose": req.purpose or {},
            "grants": [
                {"data_controller": g["data_controller"],
                 "data_scopes": g["effective_data_scopes"]}
                for g in grants
            ],
            "valid_from": valid_from,
            "valid_until": valid_until,
        }

    async def confirm(
        self, request_id: str, audience: str, username: str, statement: str,
    ) -> tuple[ConsentRequest, ConsentArtefact]:
        """Assisted confirmation (scenario 0): the subject, authenticated with
        national ID moments ago, agreed to ``statement`` (the "I agree" tick).
        The consent is active at once; no signed form, no Consent Approver.
        The assurance records the login, the partner user and the statement."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id, with_for_update=True)
            if not self._visible(req, audience):
                raise LifecycleError(404, "not_found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            now = datetime.now(timezone.utc)
            grants, fetch_type, valid_from, valid_until, ceiling, expected = (
                await self._confirmation_terms(req, now)
            )
            if statement.strip() != expected:
                raise LifecycleError(
                    409, "the terms have changed since they were shown: show them again"
                )
            auth = await self.subject_auth.authenticated(session, request_id)
            if auth is None:
                raise LifecycleError(
                    409, "the subject must authenticate with national ID first"
                )
            at = _aware(auth.auth_timestamp or auth.created_at)
            max_age = timedelta(seconds=_config.assisted_confirmation_login_max_age_seconds)
            if now - at > max_age:
                raise LifecycleError(
                    409, "the subject's national-ID login is too old: authenticate again"
                )
            assurance = {
                "method": ASSISTED_CONFIRMATION,
                "evidence": [],
                "confirmed_by": username,
                "confirmed_at": now.isoformat(),
                "statement": expected,
                "statement_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
                "subject_authenticated": True,
                "subject_authentication": _auth_summary(auth),
            }
            artefact = _artefact(
                req, grants, fetch_type, valid_from, valid_until, assurance, ceiling, now
            )
            receipt = self.receipts.build_receipt(artefact)
            req.status = RequestStatus.approved.value
            req.submitted_at = now
            req.verified_at = now
            req.verification_note = f"Assisted confirmation, by partner user {username}"
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
        return select(ConsentArtefact).where(
            ConsentArtefact.source == ArtefactSource.originated.value,
            ConsentArtefact.partner_id == audience,
        )

    async def list_consents(
        self, audience: str, status: Optional[str], limit: int, offset: int
    ) -> tuple[int, list, dict]:
        """The partner's originated consents, newest first, with
        {request id: template} for those made from a request."""
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
        return total, rows, await self.templates([a.consent_request_id for a in rows])

    async def get_consent(self, audience: str, consent_id: str):
        async with async_session()() as session:
            artefact = (
                await session.execute(
                    self._partner_consents(audience).where(ConsentArtefact.id == consent_id)
                )
            ).scalars().first()
        if artefact is None:
            return None, {}
        return artefact, await self.templates([artefact.consent_request_id])

    async def templates(self, request_ids: list) -> dict:
        """{request id: the consent template it names (a label)}."""
        ids = [r for r in request_ids if r]
        if not ids:
            return {}
        async with async_session()() as session:
            rows = await session.execute(
                select(ConsentRequest.id, ConsentRequest.template).where(
                    ConsentRequest.id.in_(ids)
                )
            )
            return {rid: template for rid, template in rows.all()}

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


__all__ = [
    "ASSISTED_CONFIRMATION", "AssistedConsentService", "EvidenceError", "artefact_status",
    "confirmation_statement",
]
