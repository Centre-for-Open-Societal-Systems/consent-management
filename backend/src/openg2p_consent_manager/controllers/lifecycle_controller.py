import logging
from datetime import datetime, timezone

from fastapi import Depends
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import (
    CallerIdentity,
    check_bene_login,
    check_not_consent_login,
    current_identity,
    subject_id_types,
)
from ..config import Settings
from ..schemas.lifecycle import (
    ApproveRequest,
    ArtefactResponse,
    AuthenticateRequest,
    AuthenticateResponse,
    ConsentRequestCreate,
    ConsentRequestResponse,
    DenyRequest,
    RevokeRequest,
    RevokeResponse,
)
from ..services import BeneConsentService, ConsentService, LifecycleError, LifecycleService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _err(exc: LifecycleError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


def _artefact_response(artefact) -> ArtefactResponse:
    resp = ArtefactResponse.model_validate(artefact)
    resp.consent_id = artefact.id
    return resp


class LifecycleController(BaseController):
    """Origination flow — create request, authenticate, approve/deny, revoke."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lifecycle = LifecycleService.get_component()
        self.consents = ConsentService.get_component()
        self.bene_consents = BeneConsentService.get_component()
        self.router.prefix += "/consent/v1"
        self.router.tags += ["Consent Lifecycle"]

        # Origination is driven by authenticated staff/service callers.
        auth = [Depends(current_identity)]
        self.router.add_api_route(
            "/consent-requests", self.create_request, dependencies=auth,
            responses={201: {"model": ConsentRequestResponse}}, methods=["POST"], status_code=201,
        )
        self.router.add_api_route(
            "/consent-requests/{request_id}", self.get_request, dependencies=auth,
            responses={200: {"model": ConsentRequestResponse}}, methods=["GET"],
        )
        self.router.add_api_route(
            "/consent-requests/{request_id}/authenticate", self.authenticate, dependencies=auth,
            responses={200: {"model": AuthenticateResponse}}, methods=["POST"],
        )
        self.router.add_api_route(
            "/consent-requests/{request_id}/approve", self.approve, dependencies=auth,
            responses={201: {"model": ArtefactResponse}}, methods=["POST"], status_code=201,
        )
        self.router.add_api_route(
            "/consent-requests/{request_id}/deny", self.deny, dependencies=auth,
            responses={200: {"model": ConsentRequestResponse}}, methods=["POST"],
        )
        self.router.add_api_route(
            "/consents/{consent_id}/revoke", self.revoke, dependencies=auth,
            responses={200: {"model": RevokeResponse}}, methods=["POST"],
        )

    async def _own_request(self, identity: CallerIdentity, request_id: str, answer: bool = False):
        """A subject (beneficiary API login) only sees and answers their own
        requests, and answers only self-service ones. None = allowed. On the
        beneficiary API with its auth on, only a subject may call (403)."""
        check_bene_login(identity)
        if not identity.is_subject:
            return None
        if identity.consent_request_id and identity.consent_request_id != request_id:
            check_not_consent_login(identity)  # a consent login is for its own request only
        req = await self.lifecycle.get_request(request_id)
        if not self.bene_consents.owns(req, identity.subject_id_value, subject_id_types(identity)):
            return JSONResponse(status_code=404, content={"error": "not_found"})
        if answer and not self.bene_consents.self_service(req):
            return JSONResponse(status_code=409, content={
                "error": f"this request is confirmed through the partner ('{req.method}')"})
        return None

    async def create_request(self, data: ConsentRequestCreate,
                             identity: CallerIdentity = Depends(current_identity)):
        if identity.is_subject:
            return JSONResponse(status_code=403, content={"error": "a subject cannot raise requests"})
        try:
            req = await self.lifecycle.create_request(data)
        except LifecycleError as exc:
            return _err(exc)
        return ConsentRequestResponse.model_validate(req)

    async def get_request(self, request_id: str, identity: CallerIdentity = Depends(current_identity)):
        if (refused := await self._own_request(identity, request_id)) is not None:
            return refused
        req = await self.lifecycle.get_request(request_id)
        if req is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return ConsentRequestResponse.model_validate(req)

    async def authenticate(self, request_id: str, data: AuthenticateRequest,
                           identity: CallerIdentity = Depends(current_identity)):
        if (refused := await self._own_request(identity, request_id)) is not None:
            return refused
        try:
            ctx = await self.lifecycle.authenticate(request_id, data.id_token)
        except LifecycleError as exc:
            return _err(exc)
        return AuthenticateResponse(
            request_id=request_id, auth_context_id=ctx.id,
            token_validated=ctx.token_validated, auth_method=ctx.auth_method,
        )

    async def approve(self, request_id: str, data: ApproveRequest,
                      identity: CallerIdentity = Depends(current_identity)):
        if (refused := await self._own_request(identity, request_id, answer=True)) is not None:
            return refused
        ctx = None
        if identity.is_subject:
            req = await self.lifecycle.get_request(request_id)
            if req.status != "pending":
                return JSONResponse(status_code=409, content={"error": f"request is '{req.status}'"})
            # The subject's beneficiary API login is the request's authentication.
            ctx = await self.bene_consents.record_login(request_id, identity.raw_claims)
        try:
            artefact = await self.lifecycle.approve(
                request_id, granted_scopes=data.granted_scopes, granted_grants=data.grants
            )
        except LifecycleError as exc:
            return _err(exc)
        if ctx is not None:
            artefact = await self.bene_consents.mark_self_service(artefact.id, ctx)
        return _artefact_response(artefact)

    async def deny(self, request_id: str, data: DenyRequest = DenyRequest(),
                   identity: CallerIdentity = Depends(current_identity)):
        if (refused := await self._own_request(identity, request_id, answer=True)) is not None:
            return refused
        try:
            req = await self.lifecycle.deny(request_id, data.reason)
        except LifecycleError as exc:
            return _err(exc)
        return ConsentRequestResponse.model_validate(req)

    async def revoke(self, consent_id: str, data: RevokeRequest = RevokeRequest(),
                     identity: CallerIdentity = Depends(current_identity)):
        check_bene_login(identity)
        check_not_consent_login(identity)
        try:
            artefact = await self.consents.revoke(
                consent_id, originated_by="subject" if identity.is_subject else data.originated_by,
                reason=data.reason,
                subject_claims={"subject_id_value": identity.subject_id_value,
                                "subject_id_types": subject_id_types(identity)}
                if identity.is_subject else None,
            )
        except PermissionError:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        if artefact is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return RevokeResponse(
            consent_id=artefact.id, status=artefact.status,
            revoked_at=artefact.revoked_at or datetime.now(timezone.utc),
        )
