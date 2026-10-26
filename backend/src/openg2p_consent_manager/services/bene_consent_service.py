"""The beneficiary API's consent side: what a subject logged in with national
ID may see and answer — with a beneficiary API auth token (BeneAuthService) or
a consent login token for one request (ConsentLoginService).

* a subject sees only their own requests and consents, matched on the national
  ID across every type in ``subject_auth_id_types``;
* a subject answers only self-service requests (assisted / OTP requests are
  confirmed through the partner and staff);
* an approval records the subject's login (bene_api or consent_login) as the
  request's authentication (AuthContext) and on the consent's assurance.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import func, or_, select

from ..config import Settings
from ..db import async_session
from ..models import AuthContext, ConsentArtefact, ConsentRequest, RequestMethod

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BeneConsentService(BaseService):
    @staticmethod
    def owns(req: Optional[ConsentRequest], subject_id_value: str, id_types: list) -> bool:
        return (
            req is not None
            and req.subject_id_type in id_types
            and "".join(str(req.subject_id_value or "").split()) == subject_id_value
        )

    @staticmethod
    def self_service(req: ConsentRequest) -> bool:
        """Requests the subject answers themselves (originated by a service).
        Assisted / OTP requests are confirmed through the partner and staff."""
        return req.method in (None, RequestMethod.self_service.value)

    async def list_requests(self, subject_id_value: str, id_types: list, status: Optional[str] = None,
                            page: int = 1, size: int = 20) -> dict:
        async with async_session()() as session:
            base = select(ConsentRequest).where(
                ConsentRequest.subject_id_type.in_(id_types),
                ConsentRequest.subject_id_value == subject_id_value,
                or_(ConsentRequest.method.is_(None),
                    ConsentRequest.method == RequestMethod.self_service.value),
            )
            if status:
                base = base.where(ConsentRequest.status == status)
            total = int((await session.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
            rows = (
                await session.execute(
                    base.order_by(ConsentRequest.created_at.desc()).offset((page - 1) * size).limit(size)
                )
            ).scalars().all()
        return {"items": rows, "total": total, "page": page, "size": size,
                "pages": max(1, (total + size - 1) // size)}

    async def record_login(self, request_id: str, claims: dict) -> AuthContext:
        """The subject's national-ID login (the claims of their token: a
        beneficiary API login, or a consent login for this request) as the
        request's authentication, so the approval records how they confirmed."""
        auth_time = claims.get("auth_time")
        amr = claims.get("amr")
        method = (amr[0] if isinstance(amr, list) and amr else amr) or claims.get("acr")
        ctx = AuthContext(
            consent_request_id=request_id,
            auth_provider=claims.get("idp") or _config.subject_auth_provider_name,
            auth_method=str(method)[:50] if method else None,
            auth_timestamp=datetime.fromtimestamp(auth_time, timezone.utc)
            if isinstance(auth_time, (int, float)) else _now(),
            issuer=claims.get("idp_iss"),
            id_token_hash=claims.get("idh") or "",
            token_validated=True,
            verified_claims={
                **{k: claims[k] for k in ("acr", "amr", "auth_time", "name", "sid") if claims.get(k) is not None},
                "login": "consent_login" if claims.get("crid") else "bene_api",
            },
        )
        async with async_session()() as session:
            session.add(ctx)
            await session.commit()
            await session.refresh(ctx)
        return ctx

    async def mark_self_service(self, artefact_id: str, ctx: AuthContext) -> ConsentArtefact:
        """Record on the consent that the subject approved it themselves, logged
        in with national ID (``login``: bene_api or consent_login)."""
        async with async_session()() as session:
            artefact = await session.get(ConsentArtefact, artefact_id)
            artefact.assurance = {
                "method": RequestMethod.self_service.value,
                "evidence": [],
                "verified_by": None,
                "verified_at": _now().isoformat(),
                "subject_authenticated": True,
                "subject_authentication": {
                    "provider": ctx.auth_provider,
                    "method": ctx.auth_method,
                    "acr": (ctx.verified_claims or {}).get("acr"),
                    "login": (ctx.verified_claims or {}).get("login"),
                    "authenticated_at": ctx.auth_timestamp.isoformat() if ctx.auth_timestamp else None,
                    "auth_context_id": ctx.id,
                },
            }
            await session.commit()
            await session.refresh(artefact)
            return artefact
