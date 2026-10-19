from datetime import datetime
from enum import Enum
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field


class ReasonCode(str, Enum):
    ok = "ok"
    malformed_object = "malformed_object"
    unknown_partner = "unknown_partner"
    signature_invalid = "signature_invalid"
    audience_mismatch = "audience_mismatch"
    subject_not_allowed = "subject_not_allowed"
    purpose_not_allowed = "purpose_not_allowed"
    scope_exceeds_policy = "scope_exceeds_policy"
    validity_exceeds_policy = "validity_exceeds_policy"
    # The policy's consent_method does not accept this kind of consent (e.g.
    # a partner-signed consent_jws for an "otp" partner).
    consent_method_not_allowed = "consent_method_not_allowed"
    # request_context.purpose is not the purpose the subject consented to.
    purpose_mismatch = "purpose_mismatch"
    expired = "expired"
    revoked = "revoked"
    replay = "replay"
    # The consent has no grant for the data_controller named in the request
    # (or, for a legacy consent, the named controller differs from the consent's).
    controller_not_granted = "controller_not_granted"
    # request_context.subject_id has the consent subject's type but another value.
    subject_mismatch = "subject_mismatch"
    # ── Agri Stack exchange (only when its settings are configured) ──
    # issue_receipts requested by a caller that is not a configured presenter
    # (or consent-token issuing is not configured on this CM).
    receipt_presenter_not_allowed = "receipt_presenter_not_allowed"
    # A consent token from an issuer this CM does not trust (or none trusted).
    receipt_issuer_not_trusted = "receipt_issuer_not_trusted"
    # A trusted issuer's consent token that failed verification (signature, typ, claims).
    receipt_invalid = "receipt_invalid"
    # The consent token names another presenter than the caller.
    presenter_mismatch = "presenter_mismatch"
    # The consent token's status could not be checked at its issuer (fail closed).
    receipt_status_unavailable = "receipt_status_unavailable"
    # ── A stored (originated) consent presented by its ID ──
    # No originated consent with that ID.
    unknown_consent = "unknown_consent"
    # The consent's validity has not started yet.
    not_yet_valid = "not_yet_valid"
    # consent_partner_id is not the partner the consent was given to.
    partner_mismatch = "partner_mismatch"


class SubjectId(BaseModel):
    type: str = Field(..., examples=["national_id"])
    value: str = Field(..., examples=["FARMER_1234"])


class Problem(BaseModel):
    """Standard error body for non-decision endpoints."""

    error: str
    detail: Optional[str] = None
    trace_id: Optional[str] = None


DataT = TypeVar("DataT")


class Paginated(BaseModel, Generic[DataT]):
    items: List[DataT]
    total: int
    page: int
    size: int
    pages: int


class StatusResponse(BaseModel):
    consent_id: str
    status: str
    valid_until: Optional[datetime] = None
    checked_at: datetime


class ReceiptStatusResponse(BaseModel):
    jti: str
    status: str  # active | revoked | expired
    checked_at: datetime
