import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..models.partner import ConsentMethod
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


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class PolicyService(BaseService):
    """Deterministic, side-effect-free policy evaluation.

    Given a consent object, the partner's active policy/keys, and the request
    context, it always yields the same decision — testable and auditable.
    Signature verification, replay, and revocation are handled by the caller;
    this covers the policy/scope/validity ceiling.
    """

    def evaluate(
        self, consent_object, material, request_context, grant=None
    ) -> PolicyResult:
        """Evaluate one grant of ``consent_object`` against the binding in
        ``material`` (the partner's binding + active policy for that grant's
        controller). ``grant`` defaults to the consent's legacy single grant."""
        if grant is None:
            grant = consent_object.grant_for(consent_object.data_controller or "")
        policy = material.get("policy")
        if policy is None:
            return PolicyResult(False, ReasonCode.unknown_partner, "No active policy")

        version = policy.version
        now = datetime.now(timezone.utc)
        partner = material["partner"]

        # 4. Audience — the object must name this partner (aud) and the grant
        #    must be for the controller this binding (and policy) belongs to. One
        #    shared CM serves many controllers; a partner has one binding (and
        #    policy) per controller.
        if consent_object.aud != partner.audience:
            return PolicyResult(
                False, ReasonCode.audience_mismatch,
                "aud does not match the partner", policy_version=version,
            )
        if grant is None or grant.data_controller != partner.controller_id:
            return PolicyResult(
                False, ReasonCode.audience_mismatch,
                "data_controller does not match the partner's binding",
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
        #    controller) ∩ requested. Never widen.
        consented = set(grant.data_scopes or [])
        allowed = set(policy.allowed_data_scopes or [])
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

    @staticmethod
    def consent_method_refusal(policy, *, otp_confirmed: bool) -> Optional[PolicyResult]:
        """The binding's ``consent_method``: ``otp`` accepts only a consent the
        subject confirmed with an OTP at the CM; ``partner_signed`` (or none)
        and ``internal`` accept what they always did. None when accepted."""
        if policy.consent_method == ConsentMethod.otp.value and not otp_confirmed:
            return PolicyResult(
                False, ReasonCode.consent_method_not_allowed,
                "this partner's policy requires a consent the subject confirmed "
                "with an OTP, presented by its consent_id",
                policy_version=policy.version,
            )
        return None

    def terms_refusal(
        self, policy, *, subject_id_type: str, purpose: Optional[dict],
        valid_from: datetime, valid_until: datetime,
    ) -> Optional[PolicyResult]:
        """Steps 5, 6 and the duration ceiling of 8 above, for a consent the CM
        holds (an OTP or assisted request about to be granted, or a stored
        consent presented by ID) against the binding's policy as it is NOW.
        None when the terms fit."""
        version = policy.version
        if (
            policy.allowed_subject_id_types
            and subject_id_type not in policy.allowed_subject_id_types
        ):
            return PolicyResult(
                False, ReasonCode.subject_not_allowed,
                f"subject_id_type '{subject_id_type}' not permitted", policy_version=version,
            )
        purpose_code = (purpose or {}).get("code")
        if policy.allowed_purposes and purpose_code not in policy.allowed_purposes:
            return PolicyResult(
                False, ReasonCode.purpose_not_allowed,
                f"purpose '{purpose_code}' not permitted", policy_version=version,
            )
        if policy.max_validity_duration:
            try:
                max_delta = iso_duration_to_timedelta(policy.max_validity_duration)
            except ValueError:
                _logger.warning(
                    "Invalid max_validity_duration on policy %s: %s",
                    policy.id, policy.max_validity_duration,
                )
            else:
                if _aware(valid_until) - _aware(valid_from) > max_delta:
                    return PolicyResult(
                        False, ReasonCode.validity_exceeds_policy,
                        "validity longer than policy allows", policy_version=version,
                    )
        return None
