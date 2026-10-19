import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import Depends, Query
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import get_current_subject
from ..config import Settings
from ..schemas.common import Paginated
from ..schemas.lifecycle import ArtefactResponse, RevokeRequest, RevokeResponse
from ..services import ConsentService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _artefact_response(artefact) -> ArtefactResponse:
    resp = ArtefactResponse.model_validate(artefact)
    resp.consent_id = artefact.id
    return resp


class SubjectController(BaseController):
    """GDPR data-subject rights — access and withdraw. Scoped to the caller."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.consents = ConsentService.get_component()
        self.router.prefix += "/consent/v1/my"
        self.router.tags += ["Subject (GDPR)"]

        self.router.add_api_route(
            "/consents", self.list_my_consents,
            responses={200: {"model": Paginated[ArtefactResponse]}}, methods=["GET"],
        )
        self.router.add_api_route(
            "/consents/{consent_id}", self.get_my_consent,
            responses={200: {"model": ArtefactResponse}}, methods=["GET"],
        )
        self.router.add_api_route(
            "/receipts/{receipt_id}", self.get_my_receipt, methods=["GET"],
        )
        self.router.add_api_route(
            "/consents/{consent_id}/revoke", self.revoke_my_consent,
            responses={200: {"model": RevokeResponse}}, methods=["POST"],
        )

    async def list_my_consents(
        self,
        subject: Dict[str, str] = Depends(get_current_subject),
        status: Optional[str] = Query(None),
        page: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=100),
    ) -> Paginated[ArtefactResponse]:
        result = await self.consents.list_subject_consents(
            subject["subject_id_type"], subject["subject_id_value"],
            status=status, page=page, size=size,
        )
        return Paginated[ArtefactResponse](
            items=[_artefact_response(a) for a in result["items"]],
            total=result["total"], page=result["page"],
            size=result["size"], pages=result["pages"],
        )

    async def get_my_consent(
        self, consent_id: str,
        subject: Dict[str, str] = Depends(get_current_subject),
    ):
        artefact = await self.consents.get_subject_artefact(
            consent_id, subject["subject_id_type"], subject["subject_id_value"]
        )
        if artefact is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return _artefact_response(artefact)

    async def get_my_receipt(
        self, receipt_id: str,
        subject: Dict[str, str] = Depends(get_current_subject),
    ):
        receipt = await self.consents.get_receipt(receipt_id)
        if receipt is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        # Ensure the receipt belongs to the authenticated subject.
        artefact = await self.consents.get_subject_artefact(
            receipt.consent_id, subject["subject_id_type"], subject["subject_id_value"]
        )
        if artefact is None:
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        return JSONResponse(content=receipt.document)

    async def revoke_my_consent(
        self, consent_id: str,
        subject: Dict[str, str] = Depends(get_current_subject),
        data: RevokeRequest = RevokeRequest(),
    ):
        try:
            artefact = await self.consents.revoke(
                consent_id, originated_by="subject", reason=data.reason,
                subject_claims=subject,
            )
        except PermissionError:
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        if artefact is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return RevokeResponse(
            consent_id=artefact.id, status=artefact.status,
            revoked_at=artefact.revoked_at or datetime.now(timezone.utc),
        )
