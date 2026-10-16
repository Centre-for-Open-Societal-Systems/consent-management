import logging
from typing import Literal, Optional

from fastapi import Depends, Query
from openg2p_fastapi_common.controller import BaseController

from ..auth import require_role
from ..config import Settings
from ..schemas.verification import DecisionLogResponse
from ..services import VerificationService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class DecisionsController(BaseController):
    """Staff (admin) status/audit view of recent validation decisions.

    Lives on the STAFF api (Keycloak staff realm) — distinct from the partner api
    that actually serves /validate.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.verification = VerificationService.get_component()
        self.router.prefix += "/consent/v1"
        self.router.tags += ["Decisions (staff)"]

        self.router.add_api_route(
            "/decisions", self.list_decisions,
            dependencies=[Depends(require_role(_config.auth_admin_role))],
            responses={200: {"model": list[DecisionLogResponse]}}, methods=["GET"],
        )

    async def list_decisions(
        self,
        partner_id: Optional[str] = Query(None),
        decision: Optional[Literal["permit", "deny"]] = Query(None),
        limit: int = Query(50, ge=1, le=200),
    ):
        rows = await self.verification.list_decisions(
            partner_id=partner_id, decision=decision, limit=limit
        )
        return [DecisionLogResponse.model_validate(r) for r in rows]
