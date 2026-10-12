import logging
from typing import Literal, Optional

from fastapi import Depends, Query
from openg2p_fastapi_common.controller import BaseController

from ..auth import CallerIdentity, require_any_role, require_role, staff_username
from ..config import Settings
from ..schemas.portal import (
    ApproveVerification,
    RejectVerification,
    VerificationRequestDetail,
    VerificationRequestList,
    VerificationRequestResponse,
)
from ..services import AssistedConsentService, EvidenceError, LifecycleError
from .assisted_views import error, evidence_list, file_response, not_found, request_fields

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_Status = Literal["pending_verification", "approved", "rejected", "cancelled"]


def _view(req, consent_id=None, subject_auth=None) -> VerificationRequestResponse:
    return VerificationRequestResponse(
        **request_fields(req, consent_id, subject_auth), partner_audience=req.partner_audience
    )


class ConsentVerificationController(BaseController):
    """STAFF api — verification of assisted consents (the subject's signed form
    uploaded by a partner user). Approvers act; the admin role may read."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.assisted = AssistedConsentService.get_component()
        self.router.prefix += "/consent/v1/verifications"
        self.router.tags += ["Consent verifications (staff)"]

        read = [Depends(require_any_role(_config.auth_approver_role, _config.auth_admin_role))]
        self.router.add_api_route(
            "", self.list_requests, dependencies=read, methods=["GET"],
            responses={200: {"model": VerificationRequestList}},
        )
        self.router.add_api_route(
            "/{request_id}", self.get_request, dependencies=read, methods=["GET"],
            responses={200: {"model": VerificationRequestDetail}},
        )
        self.router.add_api_route(
            "/{request_id}/evidence/{evidence_id}", self.download_evidence,
            dependencies=read, methods=["GET"],
        )
        self.router.add_api_route(
            "/{request_id}/approve", self.approve, methods=["POST"],
            responses={200: {"model": VerificationRequestResponse}},
        )
        self.router.add_api_route(
            "/{request_id}/reject", self.reject, methods=["POST"],
            responses={200: {"model": VerificationRequestResponse}},
        )

    async def list_requests(
        self,
        status: Optional[_Status] = Query(
            None, description="Default: every submitted assisted request"
        ),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> VerificationRequestList:
        total, rows = await self.assisted.list_requests(
            status=status, limit=limit, offset=offset, submitted_only=True
        )
        ids = await self.assisted.consent_ids([r.id for r in rows])
        auth = await self.assisted.subject_auth.summaries([r.id for r in rows])
        return VerificationRequestList(
            total=total, items=[_view(r, ids.get(r.id), auth.get(r.id)) for r in rows]
        )

    async def get_request(self, request_id: str):
        req = await self.assisted.get_request(request_id)
        if req is None:
            return not_found()
        ids = await self.assisted.consent_ids([req.id])
        auth = await self.assisted.subject_auth.summaries([req.id])
        return VerificationRequestDetail(
            **_view(req, ids.get(req.id), auth.get(req.id)).model_dump(),
            evidence=evidence_list(await self.assisted.list_evidence(req.id)),
        )

    async def download_evidence(self, request_id: str, evidence_id: str):
        evidence = await self.assisted.get_evidence(request_id, evidence_id)
        if evidence is None:
            return not_found()
        try:
            data = await self.assisted.evidence_bytes(evidence)
        except EvidenceError as exc:
            return error(exc.status_code, exc.detail)
        return file_response(evidence, data)

    async def approve(
        self, request_id: str, data: ApproveVerification = ApproveVerification(),
        identity: CallerIdentity = Depends(require_role(_config.auth_approver_role)),
    ):
        try:
            req, artefact = await self.assisted.approve(
                request_id, staff_username(identity), data.note
            )
        except LifecycleError as exc:
            return error(exc.status_code, exc.detail)
        auth = await self.assisted.subject_auth.summaries([req.id])
        return _view(req, artefact.id, auth.get(req.id))

    async def reject(
        self, request_id: str, data: RejectVerification,
        identity: CallerIdentity = Depends(require_role(_config.auth_approver_role)),
    ):
        try:
            req = await self.assisted.reject(request_id, staff_username(identity), data.note)
        except LifecycleError as exc:
            return error(exc.status_code, exc.detail)
        return _view(req)
