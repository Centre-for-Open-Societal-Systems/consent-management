"""Partner portal (consent scenario 1) and staff verification of assisted
consents."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import SubjectId
from .verification import ConsentGrant


class PartnerMe(BaseModel):
    username: str
    name: Optional[str] = None
    partner_id: str
    roles: List[str]


class PartnerBindingView(BaseModel):
    """What the partner may ask for from one data controller (its active policy)."""

    data_controller: str
    allowed_data_scopes: List[str]
    allowed_purposes: List[str]
    allowed_subject_id_types: List[str]
    max_validity_duration: Optional[str] = None


class Purpose(BaseModel):
    model_config = {"extra": "allow"}

    code: str = Field(..., min_length=1)


class Validity(BaseModel):
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class AssistedRequestCreate(BaseModel):
    subject_id: SubjectId
    # Other identifiers of the subject, e.g. [{"type": "FARMER_ID", "value": "FR-0007"}];
    # those in the CM's subject_required_identifiers must be present.
    subject_identifiers: List[SubjectId] = []
    purpose: Purpose
    grants: List[ConsentGrant] = Field(..., min_length=1)
    validity: Optional[Validity] = None
    use_case: Optional[str] = Field(None, max_length=255)

    @field_validator("subject_id")
    @classmethod
    def _subject_present(cls, value: SubjectId) -> SubjectId:
        if not value.type.strip() or not value.value.strip():
            raise ValueError("subject_id needs a type and a value")
        return value

    @model_validator(mode="after")
    def _check(self):
        types = [i.type.strip() for i in self.subject_identifiers]
        if any(not t or not i.value.strip() for t, i in zip(types, self.subject_identifiers)):
            raise ValueError("each subject identifier needs a type and a value")
        if len(types) != len(set(types)) or self.subject_id.type in types:
            raise ValueError("subject_identifiers name an ID type twice (or the subject's own)")
        controllers = [g.data_controller for g in self.grants]
        if len(controllers) != len(set(controllers)):
            raise ValueError("'grants' names the same data_controller more than once")
        if any(not g.data_scopes for g in self.grants):
            raise ValueError("each grant needs at least one data scope")
        v = self.validity
        if v and v.valid_from and v.valid_until and v.valid_until <= v.valid_from:
            raise ValueError("validity.valid_until must be after valid_from")
        return self


class RequestGrant(BaseModel):
    data_controller: str
    data_scopes: List[str]


class EvidenceResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    uploaded_by: Optional[str] = None
    uploaded_at: datetime


class SubjectAuthSummary(BaseModel):
    """The latest national-ID login of the request's subject."""

    authentication_id: str
    status: str  # pending | processing | success | failed | expired
    error: Optional[str] = None
    started_by: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    provider: Optional[str] = None
    method: Optional[str] = None
    acr: Optional[str] = None
    authenticated_at: Optional[datetime] = None
    # Demographics the identity provider returned at this login (e.g. the name
    # as per the national ID). From the provider, NOT from any registry.
    demographics: Optional[Dict[str, Any]] = None
    demographics_source: Optional[str] = None
    # A later attempt failed or is running, but an earlier one succeeded.
    previous_success: Optional[Dict[str, Any]] = None


class SubjectAuthStart(BaseModel):
    authentication_id: str
    authorization_url: str
    provider_name: str
    expires_at: datetime


class SubjectAuthConfig(BaseModel):
    enabled: bool
    provider_name: Optional[str] = None
    id_types: List[str] = []


class RequestSettings(BaseModel):
    """What a new assisted request needs (for the portal's form)."""

    required_subject_identifiers: List[str] = []
    subject_auth: SubjectAuthConfig


class AssistedRequestResponse(BaseModel):
    id: str
    status: str
    method: Optional[str] = None
    use_case: Optional[str] = None
    subject_id: SubjectId
    subject_identifiers: List[SubjectId] = []
    purpose: Dict[str, Any]
    grants: List[RequestGrant]
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: datetime
    submitted_at: Optional[datetime] = None
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    verification_note: Optional[str] = None
    consent_id: Optional[str] = None
    subject_authentication: Optional[SubjectAuthSummary] = None


class AssistedRequestDetail(AssistedRequestResponse):
    evidence: List[EvidenceResponse] = []


class AssistedRequestList(BaseModel):
    total: int
    items: List[AssistedRequestResponse]


class VerificationRequestResponse(AssistedRequestResponse):
    partner_audience: Optional[str] = None


class VerificationRequestDetail(VerificationRequestResponse):
    evidence: List[EvidenceResponse] = []


class VerificationRequestList(BaseModel):
    total: int
    items: List[VerificationRequestResponse]


class ApproveVerification(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)
    # The approver confirms the request's other subject identifiers (e.g. the
    # Farmer ID) against the registry. Required when the request carries any.
    identifiers_checked: bool = False


class RejectVerification(BaseModel):
    note: str = Field(..., min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a reason is required")
        return value


class ConsentGrantView(BaseModel):
    data_controller: Optional[str] = None
    data_scopes: List[str]
    effective_data_scopes: List[str]


class PartnerConsentResponse(BaseModel):
    consent_id: str
    status: str
    subject_id: SubjectId
    subject_identifiers: List[SubjectId] = []
    purpose: Dict[str, Any]
    grants: List[ConsentGrantView]
    valid_from: datetime
    valid_until: datetime
    assurance: Optional[Dict[str, Any]] = None
    use_case: Optional[str] = None
    consent_request_id: Optional[str] = None
    created_at: datetime
    revoked_at: Optional[datetime] = None


class PartnerConsentList(BaseModel):
    total: int
    items: List[PartnerConsentResponse]
