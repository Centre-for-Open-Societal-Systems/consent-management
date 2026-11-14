import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..schemas.common import ReasonCode
from ..utils.canonical import iso_duration_to_timedelta

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


@dataclass
class PolicyResult:
    permit: bool
    reason: ReasonCode
    detail: Optional[str] = None
    effective_scopes: List[str] = field(default_factory=list)
    policy_version: Optional[int] = None
    # The PM policy evaluated: PM's id of that version and its kind:key.
    policy_id: Optional[str] = None
    policy_ref: Optional[str] = None


@dataclass(frozen=True)
class CallerTerms:
    """Caller-terms mode: the terms a configured token presenter sent with the
    request (``request_context``), standing in for a partner policy — the
    consent's purpose must be ``purpose``; ``requested_scopes`` are the scope
    ceiling; the limits apply. No Partner Management policy is read: no
    version or id; ``ref`` is ``template:<code>`` when the presenter named the
    template."""

    purpose: str
    template: Optional[str] = None
    allowed_subject_id_types: list = field(default_factory=list)
    max_validity_duration: Optional[str] = None
    allow_partner_consents: bool = False
    allowed_signing_algs: list = field(default_factory=list)
    fetch_type: str = "oneshot"
    version = None
    id = None
    # The scope ceiling is the consent itself ∩ the requested scopes.
    is_caller_terms = True

    @property
    def allowed_purposes(self) -> list:
        return [self.purpose]

    @property
    def allowed_data_scopes(self) -> list:
        return []

    @property
    def ref(self) -> Optional[str]:
        return f"template:{self.template}" if self.template else None

    @classmethod
    def from_context(cls, ctx) -> "CallerTerms":
        limits = ctx.limits
        return cls(
            purpose=ctx.purpose,
            template=ctx.template or None,
            allowed_subject_id_types=list((limits and limits.allowed_subject_id_types) or []),
            max_validity_duration=(limits and limits.max_validity_duration) or None,
            allow_partner_consents=bool(limits and limits.allow_partner_consents),
            allowed_signing_algs=list((limits and limits.allowed_signing_algs) or []),
        )


def template_fields(code: Optional[str]) -> dict:
    """The reference recorded for a grant or decision made under a consent
    template's terms (no PM policy): ``policy_ref`` ``template:<code>``."""
    return {
        "policy_version": None, "policy_id": None,
        "policy_ref": f"template:{code}" if code else None,
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class PolicyService(BaseService):
    """Deterministic, side-effect-free policy evaluation.

    Given a consent object, the partner's active policy/keys, and the request
    context, it always yields the same decision — testable and auditable.
    Signature verification, replay, and revocation are handled by the caller;
    this covers the policy/scope/validity ceiling.
    """

    def check_caller_terms(self, request_context, purpose_code) -> Optional[PolicyResult]:
        """Caller-terms mode gate: the requested scopes (the only scope ceiling
        in this mode) must be sent, and the consent must be for the purpose
        sent. None if both hold."""
        if not request_context.requested_scopes:
            return PolicyResult(
                False, ReasonCode.scope_exceeds_policy,
                "caller-terms mode needs request_context.requested_scopes",
            )
        if purpose_code != request_context.purpose:
            return PolicyResult(
                False, ReasonCode.purpose_not_allowed,
                f"consent purpose '{purpose_code}' is not the purpose "
                f"'{request_context.purpose}' of the request",
            )
        return None

    def evaluate(
        self, consent_object, material, request_context, grant=None
    ) -> PolicyResult:
        """See ``_evaluate``; the result carries the policy's full reference."""
        result = self._evaluate(consent_object, material, request_context, grant)
        policy = (material or {}).get("policy")
        if policy is not None:
            result.policy_id = getattr(policy, "id", None)
            result.policy_ref = getattr(policy, "ref", None)
        return result

    def _evaluate(
        self, consent_object, material, request_context, grant=None
    ) -> PolicyResult:
        """Evaluate one grant of ``consent_object`` against the partner's policy
        in ``material``: its scope policy for that grant's controller (from
        Partner Management) or, in caller-terms mode, the presenter's terms
        (``CallerTerms``: no controller; the scope ceiling is then the consent
        itself ∩ the requested scopes). ``grant`` defaults to the consent's
        legacy single grant."""
        if grant is None:
            grant = consent_object.grant_for(consent_object.data_controller or "")
        policy = material.get("policy")
        if policy is None:
            return PolicyResult(False, ReasonCode.unknown_partner, "No active policy")

        version = policy.version
        now = datetime.now(timezone.utc)
        partner = material["partner"]
        caller_terms = getattr(policy, "is_caller_terms", False)

        # 4. Audience — the object must name this partner (aud) and the grant
        #    must be for the controller the policy is for. One shared CM serves
        #    many controllers; a partner has one scope policy per controller.
        if consent_object.aud != partner.audience:
            return PolicyResult(
                False, ReasonCode.audience_mismatch,
                "aud does not match the partner", policy_version=version,
            )
        if grant is None or (
            not caller_terms and grant.data_controller != partner.controller_id
        ):
            return PolicyResult(
                False, ReasonCode.audience_mismatch,
                "data_controller does not match the partner's policy",
                policy_version=version,
            )

        # 5. Subject — present and of an allowed id type.
        subject_type = consent_object.subject_id.type
        if (
            policy.allowed_subject_id_types
            and subject_type not in policy.allowed_subject_id_types
        ):
            return PolicyResult(
                False, ReasonCode.subject_not_allowed,
                f"subject_id_type '{subject_type}' not permitted", policy_version=version,
            )

        # 6. Purpose ∈ allowed_purposes.
        purpose_code = (consent_object.purpose or {}).get("code")
        if policy.allowed_purposes and purpose_code not in policy.allowed_purposes:
            return PolicyResult(
                False, ReasonCode.purpose_not_allowed,
                f"purpose '{purpose_code}' not permitted", policy_version=version,
            )

        # 7. Scope — granted (for this controller) ∩ policy (for this
        #    controller) ∩ requested. Never widen. Caller terms have no
        #    scopes: the requested scopes are the ceiling.
        consented = set(grant.data_scopes or [])
        allowed = consented if caller_terms else set(policy.allowed_data_scopes or [])
        effective = consented & allowed
        requested = (
            set(request_context.requested_scopes)
            if request_context and request_context.requested_scopes
            else None
        )
        if requested is not None:
            effective = effective & requested
        if not effective:
            return PolicyResult(
                False, ReasonCode.scope_exceeds_policy,
                "no requested scope is permitted by policy", policy_version=version,
            )

        # 8. Validity window + duration ceiling.
        valid_from = _aware(consent_object.validity.valid_from)
        valid_until = _aware(consent_object.validity.valid_until)
        if now < valid_from:
            return PolicyResult(
                False, ReasonCode.expired, "not yet valid", policy_version=version
            )
        if now > valid_until:
            return PolicyResult(
                False, ReasonCode.expired, "consent expired", policy_version=version
            )
        if policy.max_validity_duration:
            try:
                max_delta = iso_duration_to_timedelta(policy.max_validity_duration)
                if (valid_until - valid_from) > max_delta:
                    return PolicyResult(
                        False, ReasonCode.validity_exceeds_policy,
                        "validity longer than policy allows", policy_version=version,
                    )
            except ValueError:
                _logger.warning(
                    "Invalid max_validity_duration on policy %s: %s",
                    policy.id, policy.max_validity_duration,
                )

        return PolicyResult(
            True, ReasonCode.ok,
            effective_scopes=sorted(effective), policy_version=version,
        )
