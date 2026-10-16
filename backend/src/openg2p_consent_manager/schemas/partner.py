from datetime import datetime
from typing import List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    computed_field,
    field_validator,
)

from ..config import Settings
from ..models.partner import FetchType, PartnerStatus
from ..utils.canonical import iso_duration_to_timedelta

# ── Allowed values (single source of truth; served to the UI by GET /consent/v1/meta)


def allowed_signing_algorithms() -> List[str]:
    """JWS algorithms a policy may allow: the algorithms the verifier accepts
    (``crypto_allowed_algorithms``). A policy can only narrow this set."""
    raw = Settings.get_config().crypto_allowed_algorithms
    return [a.strip() for a in raw.split(",") if a.strip()]


FETCH_TYPES = [f.value for f in FetchType]
PARTNER_STATUSES = [s.value for s in PartnerStatus]
# Policy durations are stored in VARCHAR(32) columns.
_DURATION_MAX_LEN = 32


def duration_problem(value: Optional[str]) -> Optional[str]:
    """Why ``value`` is not a usable policy duration, or None if it is (or unset)."""
    if value is None:
        return None
    if len(value) > _DURATION_MAX_LEN:
        return f"longer than {_DURATION_MAX_LEN} characters"
    try:
        delta = iso_duration_to_timedelta(value)
    except ValueError:
        return "not an ISO-8601 duration such as P30D, P1Y or PT12H"
    if delta.total_seconds() <= 0:
        return "must be longer than zero"
    return None


def _clean_list(values: List[str]) -> List[str]:
    """Trim entries, drop blanks and duplicates (keeps the given order)."""
    out: List[str] = []
    for v in values:
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out


# A "partner" here is CM's policy *binding* (PM owns the partner identity/keys).
# One partner (audience) may have several bindings, one per data controller; each
# binding carries its own versioned policy.
class PartnerCreate(BaseModel):
    # Reference to the Partner-Management partner whose keys verify this partner's
    # consent objects. When omitted, CM uses the PM ref of the audience's existing
    # bindings, else falls back to `audience`.
    partner_mgmt_id: Optional[str] = Field(None, max_length=255)
    audience: str = Field(..., min_length=1, max_length=255)
    # Unique per audience: (audience, controller_id) identifies the binding.
    controller_id: str = Field(..., min_length=1, max_length=255)
    # Optional display label (identity is authoritative in Partner Management).
    name: Optional[str] = Field(None, max_length=255)


class PartnerUpdate(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    name: Optional[str] = None
    # This binding only. One of PARTNER_STATUSES.
    status: Optional[PartnerStatus] = None
    # Partner identity: applied to every binding of the audience.
    partner_mgmt_id: Optional[str] = None

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, value):
        if value is not None and value not in PARTNER_STATUSES:
            raise ValueError(f"status: {value!r} is not one of {PARTNER_STATUSES}")
        return value


class PartnerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: Optional[str] = None
    audience: str
    controller_id: str
    status: str
    partner_mgmt_id: Optional[str] = None
    created_at: datetime


class PolicyFields(BaseModel):
    """A policy as stored. Deliberately loose so versions saved before the
    current validation rules still load (see ``PolicyResponse.issues``)."""

    allowed_data_scopes: List[str] = Field(default_factory=list)
    allowed_purposes: List[str] = Field(default_factory=list)
    allowed_subject_id_types: List[str] = Field(default_factory=list)
    allowed_signing_algs: List[str] = Field(default_factory=lambda: ["EdDSA", "ES256"])
    max_validity_duration: Optional[str] = Field(None, examples=["P1Y"])
    fetch_type: str = FetchType.oneshot.value
    max_fetch_frequency: Optional[str] = None
    data_life: Optional[str] = Field(None, examples=["P30D"])


class PolicyUpsert(PolicyFields):
    """A new policy version. Values the admin UI cannot produce are rejected (400).

    - ``allowed_signing_algs``: at least one, each one of the verifier's accepted
      algorithms (``GET /consent/v1/meta`` → ``signing_algorithms``).
    - ``fetch_type``: ``oneshot`` | ``periodic``.
    - durations: ISO-8601 (``P30D``, ``P1Y``, ``PT12H``, ``P1DT6H``), > 0, or null.
    - scopes / purposes / subject id types are open sets (registry-defined);
      entries are trimmed, blanks and duplicates dropped.
    """

    model_config = ConfigDict(use_enum_values=True)

    allowed_signing_algs: List[str] = Field(
        default_factory=lambda: ["EdDSA", "ES256"]
    )
    fetch_type: FetchType = FetchType.oneshot

    @field_validator(
        "allowed_data_scopes", "allowed_purposes", "allowed_subject_id_types",
        "allowed_signing_algs",
    )
    @classmethod
    def _clean(cls, values: List[str]) -> List[str]:
        return _clean_list(values)

    # Error messages name the field: the platform's error handler returns only
    # the message (400, {"errors": [{"code", "message"}]}), not its location.
    @field_validator("allowed_signing_algs")
    @classmethod
    def _known_algs(cls, values: List[str]) -> List[str]:
        allowed = allowed_signing_algorithms()
        bad = [v for v in values if v not in allowed]
        if bad:
            raise ValueError(
                f"allowed_signing_algs: unsupported {bad}; allowed: {allowed}"
            )
        if not values:
            raise ValueError("allowed_signing_algs: at least one algorithm is required")
        return values

    @field_validator("fetch_type", mode="before")
    @classmethod
    def _fetch_type(cls, value):
        if value not in FETCH_TYPES:
            raise ValueError(f"fetch_type: {value!r} is not one of {FETCH_TYPES}")
        return value

    @field_validator("max_validity_duration", "max_fetch_frequency", "data_life", mode="before")
    @classmethod
    def _duration(cls, value, info: ValidationInfo):
        if isinstance(value, str):
            value = value.strip().upper() or None
        problem = duration_problem(value)
        if problem:
            raise ValueError(f"{info.field_name}: {value!r} {problem}")
        return value


def policy_issues(policy: PolicyFields) -> List[str]:
    """Problems with a stored policy under the current rules (empty if none)."""
    issues: List[str] = []
    allowed = allowed_signing_algorithms()
    bad_algs = [a for a in policy.allowed_signing_algs if a not in allowed]
    if bad_algs:
        issues.append(f"allowed_signing_algs: unsupported {bad_algs}")
    if not policy.allowed_signing_algs:
        issues.append("allowed_signing_algs: empty (any algorithm accepted)")
    if policy.fetch_type not in FETCH_TYPES:
        issues.append(f"fetch_type: unknown {policy.fetch_type!r}")
    for field in ("max_validity_duration", "max_fetch_frequency", "data_life"):
        problem = duration_problem(getattr(policy, field))
        if problem:
            issues.append(f"{field}: {getattr(policy, field)!r} {problem}")
    return issues


class PolicyResponse(PolicyFields):
    model_config = ConfigDict(from_attributes=True)

    id: str
    partner_id: str
    version: int
    status: str  # pending | active | superseded | rejected | failed | stale
    awe_request_id: Optional[str] = None
    # Version active when this one was created (0 = none); see PolicyStatus.stale.
    base_version: Optional[int] = None
    # Why the version ended failed / stale / rejected.
    status_reason: Optional[str] = None
    effective_from: Optional[datetime] = None

    @computed_field
    @property
    def issues(self) -> List[str]:
        """Values that the current rules reject (a version saved before them).
        Shown flagged in the UI; re-saving the policy requires fixing them."""
        return policy_issues(self)


class PolicyMeta(BaseModel):
    """Allowed values for the policy/binding forms, so the UI does not hard-code them."""

    signing_algorithms: List[str]
    fetch_types: List[str]
    partner_statuses: List[str]
    # Open sets: values already used in bindings/policies, offered as suggestions.
    known_controller_ids: List[str]
    known_data_scopes: List[str]
    known_purposes: List[str]
    known_subject_id_types: List[str]
