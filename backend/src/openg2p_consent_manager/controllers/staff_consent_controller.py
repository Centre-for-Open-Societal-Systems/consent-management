import logging

from fastapi import Depends
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import CallerIdentity, require_any_role, staff_username
from ..config import Settings
from ..schemas.lifecycle import RevokeResponse
from ..schemas.portal import RevocationView, StaffConsentResponse, StaffRevokeRequest
from ..services import AssistedConsentService, ConsentService
from .assisted_views import consent_view, error, evidence_list, not_found

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_staff = require_any_role(_config.auth_approver_role, _config.auth_admin_role)


class StaffConsentController(BaseController):
    """STAFF api — a consent by id, and its revocation on the subject's
    request (the subject asks the operating organisation; self-service is the
    beneficiary api's). Approvers and admins."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.consents = ConsentService.get_component()
        self.assisted = AssistedConsentService.get_component()
        self.router.prefix += "/consent/v1/consents"
        self.router.tags += ["Consents (staff)"]

        read = [Depends(_staff)]
        self.router.add_api_route(
            "/{consent_id}", self.get_consent, dependencies=read, methods=["GET"],
            responses={200: {"model": StaffConsentResponse}},
        )
        self.router.add_api_route(
            "/{consent_id}/receipt", self.get_receipt, dependencies=read, methods=["GET"],
        )
        # Not ".../revoke": that path is the beneficiary api's (lifecycle).
        self.router.add_api_route(
            "/{consent_id}/revocations", self.revoke, methods=["POST"],
            responses={200: {"model": RevokeResponse}},
        )

    async def get_consent(self, consent_id: str):
        artefact = await self.consents.get_artefact(consent_id)
        if artefact is None:
            return not_found()
        templates = await self.assisted.templates([artefact.consent_request_id])
        receipt = await self.consents.get_receipt_by_consent(consent_id)
        evidence = (
            await self.assisted.list_evidence(artefact.consent_request_id)
            if artefact.consent_request_id else []
        )
        revocations = await self.consents.list_revocations(consent_id)
        return StaffConsentResponse(
            **consent_view(artefact, templates).model_dump(),
            partner_id=artefact.partner_id,
            source=artefact.source,
            receipt_id=receipt.id if receipt else None,
            evidence=evidence_list(evidence),
            revocations=[RevocationView.model_validate(r) for r in revocations],
        )

    async def get_receipt(self, consent_id: str):
        receipt = await self.consents.get_receipt_by_consent(consent_id)
        if receipt is None:
            return not_found()
        return JSONResponse(content=receipt.document)

    async def revoke(
        self, consent_id: str, data: StaffRevokeRequest,
        identity: CallerIdentity = Depends(_staff),
    ):
        """Revoke a consent on the subject's request: exactly as the subject's
        own revocation (status revoked; /validate, lookup and consent tokens
        deny it), recorded with the staff user and the reason."""
        try:
            artefact = await self.consents.revoke(
                consent_id, originated_by="subject", reason=data.reason,
                revoked_by=staff_username(identity),
            )
        except ValueError as exc:
            return error(409, str(exc))
        if artefact is None:
            return not_found()
        return RevokeResponse(
            consent_id=artefact.id, status=artefact.status, revoked_at=artefact.revoked_at
        )
