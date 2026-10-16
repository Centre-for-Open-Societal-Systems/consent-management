import json
import logging
from typing import Optional

from fastapi import Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController
from pydantic import BaseModel

from ..auth import require_role
from ..config import Settings
from ..services.awe_client import ACTIONABLE, AweClient, AweClientError
from ..services.awe_webhook_service import AweWebhookService, WebhookError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class TaskDecisionRequest(BaseModel):
    action: str  # approve | reject | abstain
    comment: Optional[str] = None


def _bearer(authorization: Optional[str]) -> str:
    """Raw bearer token to forward to AWE (empty in dev / no-auth)."""
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :]
    return ""


class AweController(BaseController):
    """AWE integration surface:

    1. **Webhook receiver** (`/webhooks/decision`) — HMAC-only, no bearer. AWE
       POSTs terminal policy-approval decisions here.
    2. **Approver proxy** (`/tasks`, `/tasks/{id}/decision`, `/tasks/{id}/claim`,
       `/requests/{id}`, `/requests/{id}/events`) — approvers act in CM's UI, and
       CM forwards these to AWE with the approver's OWN JWT (AWE has no approver
       UI). Gated on the approver role.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.webhooks = AweWebhookService.get_component()
        self.awe = AweClient.get_component()
        self.router.prefix += "/consent/v1/awe"
        self.router.tags += ["AWE"]

        approver = [Depends(require_role(_config.auth_approver_role))]

        self.router.add_api_route(
            "/webhooks/decision", self.receive_decision, methods=["POST"],
        )
        self.router.add_api_route(
            "/tasks", self.list_my_tasks, dependencies=approver, methods=["GET"],
        )
        self.router.add_api_route(
            "/tasks/{task_id}/decision", self.submit_decision,
            dependencies=approver, methods=["POST"],
        )
        self.router.add_api_route(
            "/tasks/{task_id}/claim", self.claim_task,
            dependencies=approver, methods=["POST"],
        )
        self.router.add_api_route(
            "/requests/{request_id}", self.get_request,
            dependencies=approver, methods=["GET"],
        )
        self.router.add_api_route(
            "/requests/{request_id}/events", self.get_request_events,
            dependencies=approver, methods=["GET"],
        )

    # ── Webhook (HMAC-only) ──────────────────────────────────────────────────

    async def receive_decision(
        self,
        request: Request,
        x_approval_event_id: str = Header(default="", alias="X-Approval-Event-Id"),
        x_approval_timestamp: str = Header(default="", alias="X-Approval-Timestamp"),
        x_approval_signature: str = Header(default="", alias="X-Approval-Signature"),
    ):
        # Verify against the RAW bytes — re-serialising would change the MAC.
        raw = await request.body()
        try:
            self.webhooks.verify_signature(
                raw, x_approval_timestamp, x_approval_signature
            )
        except WebhookError as exc:
            return JSONResponse(status_code=exc.status, content={"error": exc.message})

        try:
            event = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": "invalid_json"})

        try:
            result = await self.webhooks.process(x_approval_event_id, event)
        except WebhookError as exc:
            return JSONResponse(status_code=exc.status, content={"error": exc.message})
        except Exception as exc:  # noqa: BLE001 — surface as retryable 422
            _logger.exception("AWE webhook processing error")
            return JSONResponse(status_code=422, content={"error": str(exc)})

        return JSONResponse(status_code=200, content={"status": result})

    # ── Approver proxy (approver JWT forwarded) ──────────────────────────────

    async def list_my_tasks(
        self,
        status: Optional[str] = Query(
            ACTIONABLE,
            description="`actionable` (default) = open or claimed; or any AWE task status",
        ),
        artifact_type: Optional[str] = Query("consent_manager.policy_change"),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        if not _config.awe_enabled:
            # Approvals are off (policies take effect at once) and AWE may not even be
            # installed: an empty inbox that says so, not AWE's 404.
            return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 1,
                    "awe_enabled": False}
        return await self._forward(
            self.awe.list_my_tasks(
                _bearer(authorization), status=status,
                artifact_type=artifact_type, page=page, page_size=page_size,
            )
        )

    async def submit_decision(
        self,
        task_id: str,
        data: TaskDecisionRequest,
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        return await self._forward(
            self.awe.submit_decision(
                _bearer(authorization), task_id, data.action, data.comment
            )
        )

    async def claim_task(
        self,
        task_id: str,
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        return await self._forward(
            self.awe.claim_task(_bearer(authorization), task_id)
        )

    async def get_request(
        self,
        request_id: str,
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        return await self._forward(
            self.awe.get_request(_bearer(authorization), request_id)
        )

    async def get_request_events(
        self,
        request_id: str,
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        return await self._forward(
            self.awe.get_request_events(_bearer(authorization), request_id)
        )

    @staticmethod
    async def _forward(coro):
        """Await an AweClient proxy call and translate its errors to responses."""
        try:
            return await coro
        except AweClientError as exc:
            return JSONResponse(
                status_code=exc.status_code if 400 <= exc.status_code < 600 else 502,
                content={"error": exc.message},
            )
