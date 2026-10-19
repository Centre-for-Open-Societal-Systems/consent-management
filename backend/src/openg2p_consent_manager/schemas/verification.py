from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from .common import ReasonCode, SubjectId


class ConsentObjectValidity(BaseModel):
    valid_from: datetime
    valid_until: datetime


class ConsentGrant(BaseModel):
    """One grant inside a consent: the scopes the subject agreed to share from
    one data controller (registry)."""

    data_controller: str = Field(..., min_length=1, examples=["farmer-registry"])
    data_scopes: List[str] = Field(..., examples=[["farmer_personal_details"]])


class ConsentObject(BaseModel):
    """The partner's consent claims — the *payload* of the consent JWS.

    The signature is NOT a field here: the whole object is signed as a compact
    JWS (RFC 7515) and these are the claims recovered from its payload. The JWS
    protected header carries the ``alg`` and ``kid`` used to verify it.

    Two shapes are accepted:

    - **grants** (one consent, one grant per registry):
      ``"grants": [{"data_controller": "farmer-registry", "data_scopes": [...]},
      {"data_controller": "crop-sown-registry", "data_scopes": [...]}]``
    - **legacy** (single controller): ``data_controller`` + ``data_scopes``,
      treated as one grant.

    A consent carrying both ``grants`` and the legacy fields, or a grant list
    naming the same controller twice, is malformed.
    """

    jti: str
    subject_id: SubjectId
    aud: str
    purpose: Dict[str, Any]
    grants: Optional[List[ConsentGrant]] = None
    # Legacy single-grant form.
    data_controller: Optional[str] = None
    data_scopes: Optional[List[str]] = None
    fetch_type: str = "oneshot"
    validity: ConsentObjectValidity
    issued_at: datetime

    # Tolerate JSON-LD framing keys (@context/@type) and any extra attributes.
    model_config = {"extra": "allow"}

    @model_validator(mode="after")
    def _one_shape(self):
        if self.grants is not None:
            if self.data_controller is not None or self.data_scopes is not None:
                raise ValueError(
                    "consent has both 'grants' and legacy data_controller/data_scopes"
                )
            if not self.grants:
                raise ValueError("'grants' must not be empty")
            controllers = [g.data_controller for g in self.grants]
            if len(controllers) != len(set(controllers)):
                raise ValueError("'grants' names the same data_controller more than once")
        elif self.data_controller is None or self.data_scopes is None:
            raise ValueError("consent needs 'grants' or data_controller + data_scopes")
        return self

    @property
    def has_grants(self) -> bool:
        return self.grants is not None

    def grant_for(self, data_controller: str) -> Optional[ConsentGrant]:
        """The grant for one controller (legacy consent = its single grant)."""
        if self.grants is not None:
            return next((g for g in self.grants if g.data_controller == data_controller), None)
        if self.data_controller == data_controller:
            return ConsentGrant(data_controller=self.data_controller, data_scopes=self.data_scopes)
        return None


class RequestContext(BaseModel):
    requested_scopes: Optional[List[str]] = None
    # The subject the caller is about to search for. If it has the same type as
    # the consent's subject, the values must match (else deny subject_mismatch).
    # A different type is resolved by the registry, not CM.
    subject_id: Optional[SubjectId] = None
    # The use the caller is about to make ({"code": ...}). Its code must equal
    # the consent's purpose code (else deny purpose_mismatch).
    purpose: Optional[Dict[str, Any]] = None


class ValidateRequest(BaseModel):
    # The partner-signed consent object, as a compact JWS (header.payload.sig).
    # CM recovers the claims from the payload and verifies the signature against
    # the partner's Partner-Management key referenced by the JWS ``kid``.
    # Exactly one of consent_jws / consent_id.
    consent_jws: Optional[str] = None
    # A stored (originated) consent, by its ID, and the partner that obtained
    # it (its audience, e.g. bank-a) — checked against the consent.
    consent_id: Optional[str] = None
    consent_partner_id: Optional[str] = None
    partner_id: Optional[str] = None
    # The calling registry. Required when the consent carries ``grants`` (selects
    # the grant); optional for a legacy consent, where it must equal the
    # consent's data_controller if given.
    data_controller: Optional[str] = None
    request_context: Optional[RequestContext] = None
    # Exchange role: on permit, also return one signed consent token per
    # granted controller (or just ``data_controller`` if given). Only for a
    # caller (``partner_id``) listed in the CM's ``receipt_presenters``.
    issue_receipts: bool = False

    @model_validator(mode="after")
    def _one_consent(self):
        if bool(self.consent_jws) == bool(self.consent_id):
            raise ValueError("give exactly one of 'consent_jws' or 'consent_id'")
        return self


class DecisionLogResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    partner_id: Optional[str] = None
    consent_id: Optional[str] = None
    object_jti: Optional[str] = None
    data_controller: Optional[str] = None
    decision: str
    reason_code: str
    detail: Optional[str] = None
    policy_version: Optional[int] = None
    receipt_jti: Optional[str] = None
    receipt_issuer: Optional[str] = None
    created_at: datetime


class Decision(BaseModel):
    decision: str  # permit | deny
    reason_code: ReasonCode
    detail: Optional[str] = None
    consent_id: Optional[str] = None
    receipt_id: Optional[str] = None
    # The consent's subject — registries check it against what they search.
    subject_id: Optional[SubjectId] = None
    # The consent's purpose: the use the caller may make of the data.
    purpose: Optional[Dict[str, Any]] = None
    # The controller this decision is for (the selected grant), when known.
    data_controller: Optional[str] = None
    effective_data_scopes: Optional[List[str]] = None
    valid_until: Optional[datetime] = None
    policy_version: Optional[int] = None
    evaluated_at: datetime
    # Exchange role, issue_receipts=true and permit: {data_controller: consent token JWS}.
    receipts: Optional[Dict[str, str]] = None
    # Permit for a stored consent (consent_id) or with issue_receipts:
    # {data_controller: effective scopes}.
    grants: Optional[Dict[str, List[str]]] = None
