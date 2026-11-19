import hashlib
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import jwt
from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactSource,
    ArtefactStatus,
    AuthContext,
    ConsentArtefact,
    ConsentRequest,
    RequestStatus,
)
from ..utils.canonical import iso_duration_to_timedelta
from .partner_service import PartnerService
from .pm_policy_client import PolicyUnavailable, common_policy_fields, policy_fields
from .policy_service import template_fields
from .receipt_service import ReceiptService
from .template_client import (
    EXCHANGE_NEEDS_TEMPLATE,
    TEMPLATE_CODE,
    ConsentTemplateClient,
    TemplateSourceUnavailable,
    consent_by_template,
)

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class LifecycleError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class LifecycleService(BaseService):
    """Origination flow: request → authenticate → approve / deny."""

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.partners = PartnerService.get_component()
        self.receipts = ReceiptService.get_component()
        self.templates = ConsentTemplateClient.get_component()

    async def create_request(self, data) -> ConsentRequest:
        """``partner_id`` is the partner's audience. Each requested grant (or the
        single ``data_controller`` + ``requested_scopes``) must be within the
        partner's active policy for that controller in Partner Management.

        Exchange role: the request must name a consent ``template`` offered to
        the partner and is made from it — its purpose, scopes (``grants``, if
        given, chosen within them) and limits (see ``resolve_template``)."""
        if consent_by_template() and data.template is None:
            raise LifecycleError(400, EXCHANGE_NEEDS_TEMPLATE)
        if data.template is not None:
            return await self._create_template_request(data)
        if data.grants is not None:
            requested = data.grants
        else:
            requested = [
                SimpleNamespace(
                    data_controller=data.data_controller, data_scopes=data.requested_scopes
                )
            ]
        resolved = await self.resolve_grants(data.partner_id, requested)
        if data.grants is not None:
            # One request, several controllers.
            grants = [g for g, _ in resolved]
            requested_scopes = sorted({s for g in grants for s in g["data_scopes"]})
            controller_id = grants[0]["data_controller"] if len(grants) == 1 else None
        else:
            grants = None
            requested_scopes = data.requested_scopes
            controller_id = data.data_controller

        validity = data.validity or {}
        async with async_session()() as session:
            req = ConsentRequest(
                subject_id_type=data.subject_id.type,
                subject_id_value=data.subject_id.value,
                controller_id=controller_id,
                partner_id=data.partner_id,
                purpose=data.purpose,
                requested_scopes=requested_scopes,
                grants=grants,
                valid_from=validity.get("valid_from"),
                valid_until=validity.get("valid_until"),
                status=RequestStatus.pending.value,
            )
            session.add(req)
            await session.commit()
            await session.refresh(req)
            return req

    async def _create_template_request(self, data) -> ConsentRequest:
        if not consent_by_template():
            raise LifecycleError(
                422, "template is for the exchange role: give the grants instead"
            )
        validity = data.validity or {}
        purpose_code = (data.purpose or {}).get("code") if data.purpose else None
        template, grants = await self.resolve_template(
            data.partner_id, data.template, data.subject_id.type, purpose_code,
            validity.get("valid_from"), validity.get("valid_until"), chosen=data.grants,
        )
        async with async_session()() as session:
            req = ConsentRequest(
                subject_id_type=data.subject_id.type,
                subject_id_value=data.subject_id.value,
                controller_id=grants[0]["data_controller"] if len(grants) == 1 else None,
                partner_id=data.partner_id,
                purpose={"code": template.purpose},
                requested_scopes=sorted({s for g in grants for s in g["data_scopes"]}),
                grants=grants,
                valid_from=validity.get("valid_from"),
                valid_until=validity.get("valid_until"),
                status=RequestStatus.pending.value,
                template=template.code,
            )
            session.add(req)
            await session.commit()
            await session.refresh(req)
            return req

    # ── Exchange role: consent from a consent template ──────────────────────

    async def read_template(self, audience: str, code: str):
        """Consent template ``code`` as offered to the partner by the template
        source. Raises LifecycleError: 503 when the source cannot be reached
        (fail closed), 422 when it is not offered to the partner or a consent
        cannot be made from it."""
        if not code or not TEMPLATE_CODE.match(code):
            raise LifecycleError(422, f"'{code}' is not a consent template code")
        try:
            template = await self.templates.get_template(audience, code)
        except TemplateSourceUnavailable as exc:
            raise LifecycleError(
                503,
                f"the consent template source could not be reached to read template "
                f"'{code}'; a consent from a template cannot be made now",
            ) from exc
        if template is None:
            raise LifecycleError(
                422,
                f"template_not_offered: consent template '{code}' is not offered to "
                "the partner",
            )
        self.check_usable(template)
        return template

    @staticmethod
    def check_usable(template) -> None:
        """A consent can be made from ``template``: it names a purpose and
        scopes for every registry. Raises LifecycleError(422)."""
        code = template.code
        if template.unscoped_controllers or not template.grants:
            raise LifecycleError(
                422,
                f"consent template '{code}' names no scopes for "
                f"{', '.join(template.unscoped_controllers) or 'any registry'}: "
                "a consent cannot be made from it",
            )
        if not template.purpose:
            raise LifecycleError(
                422, f"consent template '{code}' names no purpose: a consent cannot be "
                "made from it"
            )

    @staticmethod
    def max_validity(template) -> Optional[timedelta]:
        """The template's longest validity, or None (no limit, or unreadable)."""
        duration = template.limits.max_validity_duration
        if not duration:
            return None
        try:
            return iso_duration_to_timedelta(duration)
        except ValueError:
            _logger.warning(
                "Invalid max_validity_duration on consent template %s: %s",
                template.code, duration,
            )
            return None

    @staticmethod
    def chosen_grants(template, chosen) -> list:
        """The template's grants with the scopes chosen (``chosen``: a list of
        ``data_controller`` + ``data_scopes``; None = every scope). Each chosen
        grant must be one of the template's registries, hold its required
        scopes and stay within required ∪ optional; a registry with required
        scopes cannot be left out. Raises LifecycleError(422)."""
        by = {g["data_controller"]: g for g in template.grants}
        if chosen is None:
            return [
                {"data_controller": c, "data_scopes": list(g["data_scopes"])}
                for c, g in by.items()
            ]
        picked = {}
        for g in chosen:
            tg = by.get(g.data_controller)
            if tg is None:
                raise LifecycleError(
                    422,
                    f"data_controller '{g.data_controller}' is not in consent template "
                    f"'{template.code}'",
                )
            scopes = set(g.data_scopes)
            if not scopes.issubset(set(tg["data_scopes"])):
                raise LifecycleError(
                    422,
                    f"scope_exceeds_template for data_controller '{g.data_controller}'",
                )
            missing = set(tg["required_scopes"]) - scopes
            if missing:
                raise LifecycleError(
                    422,
                    f"data_controller '{g.data_controller}' needs the template's "
                    f"required scopes {sorted(missing)}",
                )
            picked[g.data_controller] = sorted(scopes)
        left_out = [c for c, g in by.items() if g["required_scopes"] and c not in picked]
        if left_out:
            raise LifecycleError(
                422, f"consent template '{template.code}' needs {', '.join(left_out)}"
            )
        return [{"data_controller": c, "data_scopes": s} for c, s in picked.items() if s]

    async def resolve_template(
        self, audience: str, code: str, subject_id_type: str,
        purpose_code: Optional[str] = None, valid_from=None, valid_until=None,
        chosen=None,
    ):
        """A consent from template ``code`` (offered to the partner by the
        template source): its purpose; its grants (controller → scopes, chosen
        within required / optional); its limits on subject ID types and
        validity. Returns (template, grants)."""
        template = await self.read_template(audience, code)
        if purpose_code and purpose_code != template.purpose:
            raise LifecycleError(
                422,
                f"purpose '{purpose_code}' is not the purpose of consent template "
                f"'{code}' ('{template.purpose}')",
            )
        limits = template.limits
        if (
            limits.allowed_subject_id_types
            and subject_id_type not in limits.allowed_subject_id_types
        ):
            raise LifecycleError(
                422, f"subject_not_allowed: '{subject_id_type}' by consent template '{code}'"
            )
        limit = self.max_validity(template)
        if valid_until is not None and limit is not None:
            start = _aware(valid_from) if valid_from else datetime.now(timezone.utc)
            if _aware(valid_until) - start > limit:
                raise LifecycleError(
                    422, f"validity_exceeds_policy: by consent template '{code}'"
                )
        grants = [
            {
                **g,
                **template_fields(template.code),
                # Marks a grant made from the template (approval narrows to it).
                "template": template.code,
            }
            for g in self.chosen_grants(template, chosen)
        ]
        return template, grants

    async def template_ceiling(self, req):
        """For approving a request made from a consent template (exchange
        role): (template, {controller: the template's scopes now}); the
        template must still be offered to the partner. None for any other
        request (whose template, if any, is a label)."""
        if not req.template or not any(
            g.get("template") == req.template for g in req.grants or []
        ):
            return None
        template = await self.read_template(req.partner_audience or req.partner_id, req.template)
        return template, {g["data_controller"]: set(g["data_scopes"]) for g in template.grants}

    async def current_policy(self, audience: str, controller: Optional[str]):
        """The partner's active policy for ``controller`` in Partner Management,
        or None. Raises LifecycleError(503) when PM cannot be reached."""
        try:
            material = await self.partners.get_verification_material(audience, controller)
        except PolicyUnavailable as exc:
            raise LifecycleError(
                503, "the partner's policy could not be read from Partner Management"
            ) from exc
        return material["policy"] if material else None

    async def resolve_grants(self, audience: str, requested) -> list:
        """Check each requested grant (``data_controller`` + ``data_scopes``)
        against the partner's active policy for that controller in Partner
        Management. Returns ``[(grant dict, policy)]``; raises LifecycleError."""
        resolved = []
        for g in requested:
            policy = await self.current_policy(audience, g.data_controller)
            if policy is None:
                raise LifecycleError(
                    422,
                    f"no active policy in Partner Management for data_controller "
                    f"'{g.data_controller}'",
                )
            if not set(g.data_scopes).issubset(set(policy.allowed_data_scopes or [])):
                raise LifecycleError(
                    422, f"scope_exceeds_policy for data_controller '{g.data_controller}'"
                )
            resolved.append(
                (
                    {
                        "data_controller": g.data_controller,
                        "data_scopes": list(g.data_scopes),
                        **policy_fields(policy),
                    },
                    policy,
                )
            )
        return resolved

    async def get_request(self, request_id: str) -> Optional[ConsentRequest]:
        async with async_session()() as session:
            return await session.get(ConsentRequest, request_id)

    async def authenticate(self, request_id: str, id_token: str) -> AuthContext:
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id)
            if req is None:
                raise LifecycleError(404, "consent request not found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")

            claims, validated = self._decode_id_token(id_token)
            # Subject identity in the token must match the request's subject.
            sub = claims.get("subject_id_value") or claims.get("sub")
            if sub and sub != req.subject_id_value:
                raise LifecycleError(403, "token subject does not match request")

            ctx = AuthContext(
                consent_request_id=request_id,
                auth_provider=claims.get("iss"),
                auth_method=(claims.get("amr") or [None])[0]
                if isinstance(claims.get("amr"), list)
                else claims.get("amr"),
                auth_timestamp=datetime.now(timezone.utc),
                issuer=claims.get("iss"),
                id_token_hash=hashlib.sha256(id_token.encode("utf-8")).hexdigest(),
                token_validated=validated,
                verified_claims=claims,
            )
            session.add(ctx)
            await session.commit()
            await session.refresh(ctx)
            return ctx

    async def approve(
        self, request_id: str, granted_scopes: Optional[list] = None,
        granted_grants: Optional[list] = None,
    ) -> ConsentArtefact:
        """Approve a request. ``granted_scopes`` for a single-controller request;
        ``granted_grants`` (list of ConsentGrant) for a grants request — the
        subject approves each controller's scopes, and an omitted controller is
        declined. The resulting artefact holds the approved grants.

        An originated consent is presentable at /validate by its ID
        (``consent_id``)."""
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id)
            if req is None:
                raise LifecycleError(404, "consent request not found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")

            ctx = await self._latest_auth_context(session, request_id)
            if ctx is None:
                raise LifecycleError(412, "authentication required before approval")

            if req.grants is not None:
                artefact = await self._approve_grants(
                    session, req, ctx, granted_scopes, granted_grants
                )
                await session.commit()
                await session.refresh(artefact)
                return artefact

            if granted_scopes is None:
                raise LifecycleError(400, "granted_scopes is required for this request")
            if not set(granted_scopes).issubset(set(req.requested_scopes or [])):
                raise LifecycleError(400, "granted scopes exceed requested scopes")

            policy = await self.current_policy(req.partner_id, req.controller_id)
            allowed = set(policy.allowed_data_scopes or []) if policy else set()
            effective = sorted(set(granted_scopes) & allowed)
            if not effective:
                raise LifecycleError(400, "no granted scope permitted by policy")

            now = datetime.now(timezone.utc)
            valid_from = req.valid_from or now
            valid_until = req.valid_until or (now + timedelta(days=365))

            artefact = ConsentArtefact(
                subject_id_type=req.subject_id_type,
                subject_id_value=req.subject_id_value,
                controller_id=req.controller_id,
                partner_id=req.partner_id,
                purpose=req.purpose,
                data_scopes=req.requested_scopes,
                effective_data_scopes=effective,
                fetch_type=policy.fetch_type if policy else "oneshot",
                valid_from=valid_from,
                valid_until=valid_until,
                source=ArtefactSource.originated.value,
                **policy_fields(policy),
                auth_context_id=ctx.id,
                consent_request_id=req.id,
                status=ArtefactStatus.active.value,
            )
            receipt = self.receipts.build_receipt(artefact)
            req.status = RequestStatus.approved.value
            session.add(artefact)
            session.add(receipt)
            await session.commit()
            await session.refresh(artefact)
            return artefact

    async def deny(self, request_id: str, reason: Optional[str]) -> ConsentRequest:
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id)
            if req is None:
                raise LifecycleError(404, "consent request not found")
            if req.status != RequestStatus.pending.value:
                raise LifecycleError(409, f"request is '{req.status}'")
            req.status = RequestStatus.denied.value
            await session.commit()
            await session.refresh(req)
            return req

    async def _approve_grants(self, session, req, ctx, granted_scopes, granted_grants):
        """Build (and add to the session) the artefact + receipt for a grants
        request. Each approved grant is narrowed to the partner's active policy
        for its controller."""
        requested = {g["data_controller"]: g for g in req.grants}
        if granted_grants is not None:
            approved = {}
            for g in granted_grants:
                if g.data_controller not in requested:
                    raise LifecycleError(
                        400, f"data_controller '{g.data_controller}' was not requested"
                    )
                if not set(g.data_scopes).issubset(
                    set(requested[g.data_controller]["data_scopes"])
                ):
                    raise LifecycleError(
                        400,
                        f"granted scopes exceed requested scopes for "
                        f"'{g.data_controller}'",
                    )
                approved[g.data_controller] = list(g.data_scopes)
        else:
            # One scope list for every controller: each grant keeps the scopes it
            # requested that the subject approved.
            if not set(granted_scopes).issubset(set(req.requested_scopes or [])):
                raise LifecycleError(400, "granted scopes exceed requested scopes")
            approved = {
                c: [s for s in g["data_scopes"] if s in set(granted_scopes)]
                for c, g in requested.items()
            }

        grants = []
        fetch_type = None
        # A request made from a consent template (exchange role) is narrowed to
        # the template's scopes as they are now, not to scope policies.
        ceiling = await self.template_ceiling(req)
        for controller, rg in requested.items():
            scopes = approved.get(controller) or []
            if not scopes:
                continue  # declined by the subject
            if ceiling is not None:
                policy, allowed = None, ceiling[1].get(controller, set())
            else:
                policy = await self.current_policy(req.partner_id, controller)
                allowed = set(policy.allowed_data_scopes or []) if policy else set()
            effective = sorted(set(scopes) & allowed)
            if not effective:
                continue
            fetch_type = fetch_type or (policy.fetch_type if policy else None)
            grants.append(
                {
                    "data_controller": controller,
                    "data_scopes": rg["data_scopes"],
                    "granted_scopes": sorted(scopes),
                    "effective_data_scopes": effective,
                    **(
                        template_fields(ceiling[0].code) if ceiling is not None
                        else policy_fields(policy)
                    ),
                }
            )
        if not grants:
            raise LifecycleError(400, "no granted scope permitted by policy")

        now = datetime.now(timezone.utc)
        default_validity = timedelta(days=365)
        limit = self.max_validity(ceiling[0]) if ceiling is not None else None
        if limit is not None:
            default_validity = min(default_validity, limit)
        artefact = ConsentArtefact(
            subject_id_type=req.subject_id_type,
            subject_id_value=req.subject_id_value,
            controller_id=grants[0]["data_controller"] if len(grants) == 1 else None,
            partner_id=req.partner_id,
            purpose=req.purpose,
            data_scopes=req.requested_scopes,
            effective_data_scopes=sorted(
                {s for g in grants for s in g["effective_data_scopes"]}
            ),
            grants=grants,
            fetch_type=fetch_type or "oneshot",
            valid_from=req.valid_from or now,
            valid_until=req.valid_until or (now + default_validity),
            source=ArtefactSource.originated.value,
            **common_policy_fields(grants),
            auth_context_id=ctx.id,
            consent_request_id=req.id,
            template=req.template if ceiling is not None else None,
            status=ArtefactStatus.active.value,
        )
        receipt = self.receipts.build_receipt(artefact)
        req.status = RequestStatus.approved.value
        session.add(artefact)
        session.add(receipt)
        return artefact

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _latest_auth_context(self, session, request_id: str) -> Optional[AuthContext]:
        from sqlalchemy import select

        result = await session.execute(
            select(AuthContext)
            .where(AuthContext.consent_request_id == request_id)
            .order_by(AuthContext.created_at.desc())
        )
        return result.scalars().first()

    def _decode_id_token(self, id_token: str) -> tuple[dict, bool]:
        """Return (claims, validated). Verifies against the IdP JWKS if configured."""
        if _config.oidc_jwks_url:
            try:
                jwk_client = jwt.PyJWKClient(_config.oidc_jwks_url)
                signing_key = jwk_client.get_signing_key_from_jwt(id_token)
                claims = jwt.decode(
                    id_token,
                    signing_key.key,
                    algorithms=["RS256", "ES256", "EdDSA"],
                    audience=_config.oidc_audience or None,
                    issuer=_config.oidc_issuer or None,
                    options={"verify_aud": bool(_config.oidc_audience)},
                )
                return claims, True
            except Exception as exc:
                raise LifecycleError(401, f"ID token validation failed: {exc}")
        _logger.warning(
            "oidc_jwks_url not configured — decoding ID token WITHOUT signature "
            "verification (dev only)."
        )
        claims = jwt.decode(id_token, options={"verify_signature": False})
        return claims, False
