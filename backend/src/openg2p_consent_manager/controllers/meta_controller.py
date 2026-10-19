from fastapi import Depends
from openg2p_fastapi_common.controller import BaseController

from ..auth import require_role
from ..config import Settings
from ..schemas.partner import (
    FETCH_TYPES,
    PARTNER_STATUSES,
    PolicyMeta,
    allowed_signing_algorithms,
)
from ..services import PartnerService

_config = Settings.get_config()


class MetaController(BaseController):
    """Allowed values for the staff forms (binding + policy), served from the same
    constants the API validates against, so the UI does not hard-code them."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.partners = PartnerService.get_component()
        self.router.prefix += "/consent/v1"
        self.router.tags += ["Partners & Policy"]

        self.router.add_api_route(
            "/meta", self.meta,
            dependencies=[Depends(require_role(_config.auth_admin_role))],
            responses={200: {"model": PolicyMeta}}, methods=["GET"],
        )

    async def meta(self) -> PolicyMeta:
        known = await self.partners.known_values()
        return PolicyMeta(
            signing_algorithms=allowed_signing_algorithms(),
            fetch_types=FETCH_TYPES,
            partner_statuses=PARTNER_STATUSES,
            known_controller_ids=known["controller_ids"],
            known_data_scopes=known["data_scopes"],
            known_purposes=known["purposes"],
            known_subject_id_types=known["subject_id_types"],
        )
