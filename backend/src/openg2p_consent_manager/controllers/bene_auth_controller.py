import html
import logging
from typing import Optional

from fastapi import Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from openg2p_fastapi_common.controller import BaseController
from pydantic import BaseModel

from ..auth import CallerIdentity, current_identity
from ..config import Settings
from ..services import BeneAuthService, LifecycleError
from ..services.bene_auth_service import with_query

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class BeneTokenRequest(BaseModel):
    code: str


class BeneAuthController(BaseController):
    """Beneficiary API auth with national ID (eSignet): login → provider →
    callback → one-time code → token. The token is the subject's access to the
    beneficiary API (/my/*, their own consent requests).

    A web page passes ``return_url`` and gets the code back on that page
    (``?login=``); an API client leaves it out and the callback answers the code
    as JSON."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bene_auth = BeneAuthService.get_component()
        self.router.prefix += "/consent/v1/beneficiary/auth"
        self.router.tags += ["Beneficiary API auth (eSignet)"]

        self.router.add_api_route("/config", self.get_config, methods=["GET"])
        self.router.add_api_route("/login", self.login, methods=["GET"], response_class=RedirectResponse)
        self.router.add_api_route(
            "/callback", self.callback, methods=["GET"], response_class=RedirectResponse,
        )
        self.router.add_api_route("/token", self.token, methods=["POST"])
        self.router.add_api_route("/me", self.me, methods=["GET"])

    async def get_config(self):
        return {"enabled": self.bene_auth.enabled, "provider": _config.subject_auth_provider_name}

    async def login(self, return_url: Optional[str] = Query(None)):
        """Start a login; redirects to the provider. Without ``return_url`` the
        callback answers JSON (API clients)."""
        try:
            url = await self.bene_auth.start(return_url)
        except LifecycleError as exc:
            # Provider down / not configured: back to the page, which shows it.
            if return_url and exc.status_code >= 500 and self.bene_auth.return_url_allowed(return_url):
                return RedirectResponse(with_query(return_url, login_error=exc.detail), status_code=302)
            return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
        return RedirectResponse(url, status_code=302)

    async def callback(
        self,
        state: Optional[str] = Query(None),
        code: Optional[str] = Query(None),
        error_code: Optional[str] = Query(None, alias="error"),
        error_description: Optional[str] = Query(None),
    ):
        """The provider's redirect: back to the page with the one-time code, or
        (API client) the code as JSON."""
        try:
            return_url, result = await self.bene_auth.complete(state, code, error_code, error_description)
        except LifecycleError as exc:
            return _error_page(exc.detail, exc.status_code)
        if not return_url:
            if "login_error" in result:
                return JSONResponse(status_code=400, content={"error": result["login_error"]})
            return JSONResponse(content={"code": result["login"], "expires_in": result["expires_in"]})
        if "login_error" in result:
            return RedirectResponse(with_query(return_url, login_error=result["login_error"]), status_code=302)
        return RedirectResponse(with_query(return_url, login=result["login"]), status_code=302)

    async def token(self, data: BeneTokenRequest):
        try:
            return await self.bene_auth.exchange(data.code)
        except LifecycleError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    async def me(self, identity: CallerIdentity = Depends(current_identity)):
        if not identity.is_subject or identity.consent_request_id:
            return JSONResponse(status_code=403, content={"error": "not a beneficiary API login"})
        claims = identity.raw_claims
        return {
            "subject_id": {"type": identity.subject_id_type, "value": identity.subject_id_value},
            "name": identity.name,
            "provider": claims.get("idp"),
            "acr": claims.get("acr"),
            "auth_time": claims.get("auth_time"),
            "expires_at": claims.get("exp"),
        }


def _error_page(message: str, status_code: int) -> HTMLResponse:
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Login failed</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
        "<h1 style='color:#b42318;font-size:1.4rem'>Login failed</h1>"
        f"<p>{html.escape(message)}</p>"
        "<p style='color:#666'>Go back and log in again.</p>"
        "</body></html>"
    )
    return HTMLResponse(content=body, status_code=status_code)
