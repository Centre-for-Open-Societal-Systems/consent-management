import logging
from dataclasses import dataclass
from typing import Optional

from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from .pm_policy_client import PmPolicyClient, pm_partner_id

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


@dataclass(frozen=True)
class PartnerRef:
    """The partner a policy is for. ``audience`` is what the partner presents as
    ``aud`` (and what records store as ``partner_id``); ``partner_mgmt_id`` its
    Partner Management id (keys and policies); ``controller_id`` the registry
    the policy is for (None for terms sent by a token presenter)."""

    audience: str
    controller_id: Optional[str]
    partner_mgmt_id: str


class PartnerService(BaseService):
    """Partners and their access policies, both owned by Partner Management.
    The Consent Manager keeps no partner records: it reads the partner's active
    scope policy for a data controller from PM, cached."""

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.policies = PmPolicyClient.get_component()

    async def get_verification_material(
        self, audience: str, controller_id: Optional[str]
    ) -> Optional[dict]:
        """The partner's active scope policy for ``controller_id`` from Partner
        Management, for the validation hot path.

        Partner public keys are not included: the shared fastapi-common
        CryptoHelper fetches them from PM by ``partner_mgmt_id``.

        Shape: {"partner": PartnerRef, "policy": PmPolicy}; None when PM has no
        active policy (unknown or inactive partner, or no policy for it).
        Raises PolicyUnavailable when PM cannot be reached and nothing usable is
        cached (fail closed)."""
        if not audience:
            return None
        policy = await self.policies.get_policy(audience, controller_id)
        if policy is None:
            return None
        partner = PartnerRef(
            audience=audience, controller_id=controller_id or None,
            partner_mgmt_id=pm_partner_id(audience),
        )
        return {"partner": partner, "policy": policy}

    @staticmethod
    def terms_material(audience: str, terms) -> dict:
        """Caller-terms mode: the material for the hot path, with the
        presenter's terms (``CallerTerms``) in place of a PM policy. Nothing is
        read from Partner Management here (the partner's keys still are, by
        the crypto helper)."""
        partner = PartnerRef(
            audience=audience, controller_id=None, partner_mgmt_id=pm_partner_id(audience)
        )
        return {"partner": partner, "policy": terms}

    async def scope_policies(self, audience: str) -> list:
        """The partner's active scope policies (one per data controller), by
        controller. Raises PolicyUnavailable."""
        policies = await self.policies.list_policies(audience)
        return sorted(
            (p for p in policies if p.data_controller), key=lambda p: p.data_controller
        )
