from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class ArtefactStatus(str, Enum):
    active = "active"
    revoked = "revoked"
    expired = "expired"


class ArtefactSource(str, Enum):
    embedded = "embedded"  # partner-signed object, verified on the hot path
    originated = "originated"  # collected by the CM via the lifecycle flow
    receipt = "receipt"  # an exchange CM's consent receipt (department role)


class RequestStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    expired = "expired"
    # Assisted consent (scenario 1): the partner user submitted the evidence;
    # staff verify it (approved) or reject it. The partner may cancel before.
    pending_verification = "pending_verification"
    rejected = "rejected"
    cancelled = "cancelled"


class RequestMethod(str, Enum):
    """How the subject confirms the request."""

    assisted = "assisted"  # in person; evidence (signed form) verified by staff
    sms = "sms"
    self_service = "self_service"


class ConsentRequest(BaseORMModelWithId):
    """Origination flow — a pending request before the subject authenticates
    (or, for an assisted request, before staff verify its evidence)."""

    __tablename__ = "consent_requests"

    subject_id_type: Mapped[str] = mapped_column(String(50), index=True)
    subject_id_value: Mapped[str] = mapped_column(String(255), index=True)
    # The single controller for a legacy request; NULL when the request carries
    # several ``grants`` (one per controller).
    controller_id: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    partner_id: Mapped[str] = mapped_column(String, index=True)
    purpose: Mapped[dict] = mapped_column(JSONB)
    # Union of all requested scopes (for a grants request too).
    requested_scopes: Mapped[list] = mapped_column(JSONB, default=list)
    # Requested grants: [{"data_controller", "data_scopes", "partner_binding_id"}].
    # NULL for a legacy single-controller request.
    grants: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.pending.value, index=True)
    # Other identifiers of the subject: [{"type": "FARMER_ID", "value": "FR-0007"}].
    subject_identifiers: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    # ── Partner portal (consent scenario 1); NULL for other requests ──
    method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Display label of the use case the request was made for (e.g.
    # loan-profile@1); the grants are still what is asked for.
    use_case: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # The partner user (preferred_username in the partner realm) and partner.
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    partner_audience: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The staff user who approved/rejected the evidence.
    verified_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EvidenceKind(str, Enum):
    signed_form = "signed_form"
    other = "other"


class ConsentEvidence(BaseORMModelWithId):
    """A file uploaded on a consent request (e.g. the subject's signed form).
    The bytes live in object storage under ``storage_key``; ``sha256`` is of
    the stored bytes."""

    __tablename__ = "consent_evidence"

    consent_request_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String(20), default=EvidenceKind.signed_form.value)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(512))
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SubjectAuthStatus(str, Enum):
    pending = "pending"  # login started, waiting for the provider's callback
    processing = "processing"  # callback received (the state is used up)
    success = "success"
    failed = "failed"


class SubjectAuthSession(BaseORMModelWithId):
    """One national-ID login of a request's subject: started by a partner user,
    completed by the provider's callback. ``state`` is single-use (claimed with
    an atomic UPDATE); ``nonce`` and the PKCE ``code_verifier`` never leave the
    CM. On success ``auth_context_id`` names the AuthContext recorded."""

    __tablename__ = "subject_auth_sessions"

    consent_request_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String(128), unique=True)
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default=SubjectAuthStatus.pending.value)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_context_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class AuthContext(BaseORMModelWithId):
    """Built from a validated OIDC ID token. The raw token is never stored."""

    __tablename__ = "auth_contexts"

    consent_request_id: Mapped[str] = mapped_column(String, index=True)
    auth_provider: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    auth_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    auth_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    issuer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    id_token_hash: Mapped[str] = mapped_column(String(128))
    token_validated: Mapped[bool] = mapped_column(default=False)
    verified_claims: Mapped[dict] = mapped_column(JSONB, default=dict)


class ConsentArtefact(BaseORMModelWithId):
    """Canonical consent decision — from an embedded object or an origination.

    Embedded: one artefact per (consent jti, data controller) — a consent with
    several grants validated by two registries yields two artefacts (and two
    receipts). Originated: one artefact per approved request; when the request
    carried grants, ``grants`` holds them and ``controller_id`` is the single
    controller only if exactly one grant was approved (else NULL).
    """

    __tablename__ = "consent_artefacts"
    __table_args__ = (
        UniqueConstraint("object_jti", "controller_id", name="uq_artefact_jti_controller"),
    )

    subject_id_type: Mapped[str] = mapped_column(String(50), index=True)
    subject_id_value: Mapped[str] = mapped_column(String(255), index=True)
    # Other identifiers of the subject (from its request), e.g. the Farmer ID.
    subject_identifiers: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    controller_id: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    partner_id: Mapped[str] = mapped_column(String, index=True)

    purpose: Mapped[dict] = mapped_column(JSONB)
    data_scopes: Mapped[list] = mapped_column(JSONB, default=list)
    effective_data_scopes: Mapped[list] = mapped_column(JSONB, default=list)
    fetch_type: Mapped[str] = mapped_column(String(20), default="oneshot")

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    source: Mapped[str] = mapped_column(String(20), default=ArtefactSource.embedded.value)
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auth_context_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Idempotency / replay: the jti of the embedded object that produced this
    # artefact. Unique per (object_jti, controller_id) so the same object never
    # mints duplicates for one controller, while each granted controller gets
    # its own decision.
    object_jti: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Originated grants consent: [{"data_controller", "data_scopes",
    # "effective_data_scopes", "partner_binding_id", "policy_version"}].
    grants: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # Originated consent: how the subject confirmed, e.g. {"method": "assisted",
    # "evidence": ["signed_form"], "verified_by", "verified_at",
    # "subject_authenticated": false}. NULL for other artefacts.
    assurance: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # The consent request it was originated from (NULL for embedded/receipt).
    consent_request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(20), default=ArtefactStatus.active.value, index=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsentReceipt(BaseORMModelWithId):
    """Kantara/ISO-27560-aligned receipt, signed by the CM private key."""

    __tablename__ = "consent_receipts"

    consent_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    artefact_hash: Mapped[str] = mapped_column(String(128))
    algorithm: Mapped[str] = mapped_column(String(20))
    kid: Mapped[str] = mapped_column(String(255))
    signature: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(16), default="1.1")
    # The full signed receipt document (JSON-LD) for retrieval.
    document: Mapped[dict] = mapped_column(JSONB)


class RevocationRecord(BaseORMModelWithId):
    """Append-only record of a revocation event."""

    __tablename__ = "revocation_records"

    consent_id: Mapped[str] = mapped_column(String, index=True)
    originated_by: Mapped[str] = mapped_column(String(20))  # subject | controller | partner
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
