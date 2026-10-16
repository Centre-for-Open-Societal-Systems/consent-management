import logging
import time
from typing import Any, Dict, Optional

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


# Inbox pseudo-status: open OR claimed (both still await the approver).
ACTIONABLE = "actionable"


class AweClientError(Exception):
    """Raised when a call to AWE fails (network, non-2xx, or misconfiguration)."""

    def __init__(self, status_code: int, message: str, error_code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


class AweClient(BaseService):
    """Thin async client for the caller-facing AWE runtime endpoints CM needs.

    CM creates approval requests (``POST /v1/awe/requests``) and proxies the
    approver task endpoints for its own inbox; terminal outcomes come back via
    webhook. Modelled on the registry's ``AweHelper``.
    """

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    async def _bearer(self) -> str:
        """Return a service bearer token, using a static token if configured or
        else a cached Keycloak client-credentials grant."""
        if _config.awe_static_token:
            return _config.awe_static_token

        now = time.time()
        if self._token and now < self._token_exp - 30:
            return self._token

        if not (_config.awe_token_url and _config.awe_client_id):
            raise AweClientError(500, "AWE service credentials are not configured")

        try:
            async with httpx.AsyncClient(timeout=_config.awe_http_timeout_seconds) as client:
                resp = await client.post(
                    _config.awe_token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": _config.awe_client_id,
                        "client_secret": _config.awe_client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise AweClientError(502, f"Failed to obtain AWE service token: {exc}") from exc

        self._token = body["access_token"]
        self._token_exp = now + int(body.get("expires_in", 300))
        return self._token

    async def create_request(
        self,
        artifact_type: str,
        artifact_id: str,
        context: Dict[str, Any],
        requester: Optional[str] = None,
        caller_bearer: Optional[str] = None,
    ) -> str:
        """Submit a policy-change approval request to AWE. Returns the AWE
        ``request_id``. Raises AweClientError on any failure — the caller marks
        the pending version ``failed`` (no orphan pending rows).

        Auth: the acting admin's own bearer (``caller_bearer``) when given and
        ``awe_forward_caller_token`` is on, else a service token."""
        if not _config.awe_base_url:
            raise AweClientError(500, "awe_base_url is not configured")

        payload = {
            "policy_key": _config.awe_policy_change_policy_key,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "context": context,
            "callback_url": _config.awe_callback_url or None,
            "callback_secret_id": _config.awe_callback_secret_id or None,
            "requester": requester,
        }
        url = f"{_config.awe_base_url.rstrip('/')}/v1/awe/requests"

        try:
            if caller_bearer and _config.awe_forward_caller_token:
                token = caller_bearer
            else:
                token = await self._bearer()
            async with httpx.AsyncClient(timeout=_config.awe_http_timeout_seconds) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        # Idempotent on the policy VERSION id (a new row per
                        # version), so a retried submit of the same version never
                        # creates a second AWE request and a later version never
                        # reuses an earlier one.
                        "Idempotency-Key": f"cm-partner-{artifact_id}",
                    },
                )
        except httpx.HTTPError as exc:
            raise AweClientError(502, f"AWE unreachable: {exc}") from exc

        if resp.status_code >= 300:
            raise _error_from(resp)

        request_id = _safe_json(resp).get("request_id")
        if not request_id:
            raise AweClientError(502, "AWE response missing request_id")
        return request_id

    # ── Approver proxy (forwards the APPROVER's own JWT, not a service token) ──
    # AWE has no approver UI — approvers act in CM's UI and CM proxies these calls
    # with the approver's bearer so AWE's `sub`-based task ownership works.

    async def _proxy(
        self,
        method: str,
        path: str,
        bearer: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ):
        if not _config.awe_base_url:
            raise AweClientError(500, "awe_base_url is not configured")
        url = f"{_config.awe_base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(timeout=_config.awe_http_timeout_seconds) as client:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
                )
        except httpx.HTTPError as exc:
            raise AweClientError(502, f"AWE unreachable: {exc}") from exc
        if resp.status_code >= 300:
            raise _error_from(resp)
        return _safe_json(resp)

    async def list_my_tasks(
        self,
        bearer: str,
        *,
        status: Optional[str] = ACTIONABLE,
        artifact_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
    ) -> dict:
        """The approver's tasks. ``status=actionable`` (the inbox default) means
        open OR claimed — a claimed task still awaits this approver's decision.
        AWE filters on one status per call, so CM fetches up to
        ``awe_inbox_fetch_limit`` of each, merges newest first and pages here.
        Any other status is passed through to AWE as is."""
        if status != ACTIONABLE:
            params = {"assignee": "me", "page": page, "page_size": page_size}
            if status:
                params["status"] = status
            if artifact_type:
                params["artifact_type"] = artifact_type
            return await self._proxy("GET", "/v1/awe/tasks", bearer, params=params)

        items, total = [], 0
        limit = max(1, min(_config.awe_inbox_fetch_limit, 100))
        for one in ("open", "claimed"):
            params = {"assignee": "me", "status": one, "page": 1, "page_size": limit}
            if artifact_type:
                params["artifact_type"] = artifact_type
            data = await self._proxy("GET", "/v1/awe/tasks", bearer, params=params)
            items.extend(data.get("items") or [])
            total += int(data.get("total") or 0)
        items.sort(key=lambda t: t.get("created_at") or "", reverse=True)
        start = (page - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, -(-total // page_size)),
        }

    async def submit_decision(
        self, bearer: str, task_id: str, action: str, comment: Optional[str] = None
    ) -> dict:
        return await self._proxy(
            "POST", f"/v1/awe/tasks/{task_id}/decision", bearer,
            json_body={"action": action, "comment": comment},
        )

    async def claim_task(self, bearer: str, task_id: str) -> dict:
        return await self._proxy("POST", f"/v1/awe/tasks/{task_id}/claim", bearer)

    async def get_request(self, bearer: str, request_id: str) -> dict:
        return await self._proxy("GET", f"/v1/awe/requests/{request_id}", bearer)

    async def get_request_events(self, bearer: str, request_id: str):
        return await self._proxy("GET", f"/v1/awe/requests/{request_id}/events", bearer)


def _error_from(resp: httpx.Response) -> AweClientError:
    """Build an AweClientError from an AWE error response.

    AWE's envelope is ``{"errors": [{"errorCode", "message"}], ...}``; its auth
    layer (FastAPI) answers ``{"detail": ...}``. Fall back to the raw text."""
    body = _safe_json(resp)
    message, code = "", ""
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        message = errors[0].get("message") or ""
        code = errors[0].get("errorCode") or errors[0].get("code") or ""
    if not message:
        detail = body.get("detail")
        if isinstance(detail, str):
            message = detail
        elif detail is not None:
            message = str(detail)
    if not message:
        message = body.get("message") or (resp.text or "")[:500] or f"HTTP {resp.status_code}"
    code = code or body.get("error_code", "")
    return AweClientError(resp.status_code, message, code)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
