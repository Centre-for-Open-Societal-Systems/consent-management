import hashlib
import logging
from datetime import datetime, timedelta, timezone
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
    Partner,
    PartnerStatus,
    RequestStatus,
)
from .partner_service import PartnerService
from .receipt_service import ReceiptService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


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

    async def create_request(self, data) -> ConsentRequest:
        partner = await self.partners.get_partner(data.partner_id)
        if partner is None:
            raise LifecycleError(404, "partner not found")

        grants = None
        if data.grants is not None:
            # One request, several controllers: each grant must be within the
            # partner's binding + active policy for that controller.
            grants = [g for g, _ in await self.resolve_grants(partner.audience, data.grants)]
            requested_scopes = sorted({s for g in grants for s in g["data_scopes"]})
            controller_id = grants[0]["data_controller"] if len(grants) == 1 else None
        else:
            policy = await self.partners.get_policy(data.partner_id)
            if policy is None:
                raise LifecycleError(404, "partner or policy not found")
            # Reject up front anything the policy can never satisfy.
            if not set(data.requested_scopes).issubset(set(policy.allowed_data_scopes or [])):
                raise LifecycleError(422, "scope_exceeds_policy")
            requested_scopes = data.requested_scopes
            controller_id = partner.controller_id

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

    async def resolve_grants(self, audience: str, requested) -> list:
        """Check each requested grant (``data_controller`` + ``data_scopes``)
        against the partner's binding to that controller and its active policy.
        Returns ``[(grant dict, policy)]``; raises LifecycleError."""
        resolved = []
        for g in requested:
            binding = await self.partners.get_binding(audience, g.data_controller)
            if binding is None or binding.status != PartnerStatus.active.value:
                raise LifecycleError(
                    422, f"partner not bound to data_controller '{g.data_controller}'"
                )
            policy = await self.partners.get_policy(binding.id)
            if policy is None:
                raise LifecycleError(
                    404, f"no active policy for data_controller '{g.data_controller}'"
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
                        "partner_binding_id": binding.id,
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

            policy = await self.partners.get_policy(req.partner_id)
            allowed = set(policy.allowed_data_scopes or []) if policy else set()
            effective = sorted(set(granted_scopes) & allowed)
            if not effective:
                raise LifecycleError(400, "no granted scope permitted by policy")

            partner = await session.get(Partner, req.partner_id)
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
                policy_version=policy.version if policy else None,
                auth_context_id=ctx.id,
                consent_request_id=req.id,
                status=ArtefactStatus.active.value,
            )
            receipt = self.receipts.build_receipt(artefact, partner)
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
        request. Each approved grant is narrowed to its binding's active policy."""
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
        for controller, rg in requested.items():
            scopes = approved.get(controller) or []
            if not scopes:
                continue  # declined by the subject
            policy = await self.partners.get_policy(rg["partner_binding_id"])
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
                    "partner_binding_id": rg["partner_binding_id"],
                    "policy_version": policy.version if policy else None,
                }
            )
        if not grants:
            raise LifecycleError(400, "no granted scope permitted by policy")

        partner = await session.get(Partner, req.partner_id)
        now = datetime.now(timezone.utc)
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
            valid_until=req.valid_until or (now + timedelta(days=365)),
            source=ArtefactSource.originated.value,
            policy_version=grants[0]["policy_version"] if len(grants) == 1 else None,
            auth_context_id=ctx.id,
            consent_request_id=req.id,
            status=ArtefactStatus.active.value,
        )
        receipt = self.receipts.build_receipt(artefact, partner)
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
