"""Response builders shared by the partner portal and staff verification APIs."""
from typing import Optional
from urllib.parse import quote

from fastapi.responses import JSONResponse, Response

from ..schemas.common import SubjectId
from ..schemas.portal import (
    ConsentGrantView,
    EvidenceResponse,
    PartnerConsentResponse,
    RequestGrant,
)
from ..services import artefact_status


def error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": detail})


def not_found() -> JSONResponse:
    return error(404, "not_found")


def request_fields(req, consent_id: Optional[str] = None, subject_auth: Optional[dict] = None) -> dict:
    grants = req.grants or (
        [{"data_controller": req.controller_id, "data_scopes": req.requested_scopes or []}]
        if req.controller_id else []
    )
    return {
        "id": req.id,
        "status": req.status,
        "method": req.method,
        "template": req.template,
        "subject_id": SubjectId(type=req.subject_id_type, value=req.subject_id_value),
        "subject_identifiers": [SubjectId(**i) for i in (req.subject_identifiers or [])],
        "purpose": req.purpose or {},
        "grants": [
            RequestGrant(data_controller=g["data_controller"], data_scopes=g["data_scopes"])
            for g in grants
        ],
        "valid_from": req.valid_from,
        "valid_until": req.valid_until,
        "created_by": req.created_by,
        "created_at": req.created_at,
        "submitted_at": req.submitted_at,
        "verified_by": req.verified_by,
        "verified_at": req.verified_at,
        "verification_note": req.verification_note,
        "consent_id": consent_id,
        "subject_authentication": subject_auth,
    }


def evidence_list(rows) -> list:
    return [EvidenceResponse.model_validate(e) for e in rows]


def consent_view(artefact, templates: dict) -> PartnerConsentResponse:
    if artefact.grants:
        grants = [
            ConsentGrantView(
                data_controller=g.get("data_controller"),
                data_scopes=g.get("data_scopes") or [],
                effective_data_scopes=g.get("effective_data_scopes") or [],
                policy_version=g.get("policy_version"),
                policy_id=g.get("policy_id"),
                policy_ref=g.get("policy_ref"),
            )
            for g in artefact.grants
        ]
    else:
        grants = [
            ConsentGrantView(
                data_controller=artefact.controller_id,
                data_scopes=artefact.data_scopes or [],
                effective_data_scopes=artefact.effective_data_scopes or [],
                policy_version=artefact.policy_version,
                policy_id=artefact.policy_id,
                policy_ref=artefact.policy_ref,
            )
        ]
    return PartnerConsentResponse(
        consent_id=artefact.id,
        status=artefact_status(artefact),
        subject_id=SubjectId(type=artefact.subject_id_type, value=artefact.subject_id_value),
        subject_identifiers=[SubjectId(**i) for i in (artefact.subject_identifiers or [])],
        purpose=artefact.purpose or {},
        grants=grants,
        valid_from=artefact.valid_from,
        valid_until=artefact.valid_until,
        assurance=artefact.assurance,
        template=artefact.template or templates.get(artefact.consent_request_id),
        consent_request_id=artefact.consent_request_id,
        created_at=artefact.created_at,
        revoked_at=artefact.revoked_at,
    )


def file_response(evidence, data: bytes) -> Response:
    ascii_name = evidence.filename.encode("ascii", "replace").decode().replace('"', "_")
    disposition = (
        f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(evidence.filename)}'
    )
    return Response(
        content=data,
        media_type=evidence.content_type,
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
