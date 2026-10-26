import logging
from typing import Optional

from fastapi import Query
from fastapi.responses import JSONResponse, RedirectResponse
from openg2p_fastapi_common.controller import BaseController
from pydantic import BaseModel

from ..config import Settings
from ..services import ConsentLoginService, LifecycleError
from ..services.bene_auth_service import with_query
from .bene_auth_controller import _error_page

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class ConsentLoginTokenRequest(BaseModel):
    code: str


class ConsentLoginController(BaseController):
    """Consent login with national ID (eSignet), for ONE consent request:
    start → provider → callback (the national ID must be the request's
    subject) → one-time code → token valid only to read, grant or decline that
    request. Separate from the beneficiary API auth (/beneficiary/auth/*).

    A web page passes ``return_url`` and gets the code back on that page
    (``?login=``); an API client leaves it out and the callback answers the code
    as JSON."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.consent_login = ConsentLoginService.get_component()
        self.router.prefix += "/consent/v1/consent-login"
        self.router.tags += ["Consent login (eSignet)"]

        self.router.add_api_route("/config", self.get_config, methods=["GET"])
        self.router.add_api_route("/start", self.start, methods=["GET"], response_class=RedirectResponse)
        self.router.add_api_route(
            "/callback", self.callback, methods=["GET"], response_class=RedirectResponse,
        )
        self.router.add_api_route("/token", self.token, methods=["POST"])

    async def get_config(self):
        return {"enabled": self.consent_login.enabled, "provider": _config.subject_auth_provider_name}

    async def start(self, request_id: str = Query(...), return_url: Optional[str] = Query(None)):
        """Start a consent login for one request; redirects to the provider."""
        try:
            url = await self.consent_login.start(request_id, return_url)
        except LifecycleError as exc:
            # Request not answerable, provider down, not configured: back to the
            # page, which shows it.
            if return_url and self.consent_login.return_url_allowed(return_url):
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
            return_url, result = await self.consent_login.complete(state, code, error_code, error_description)
        except LifecycleError as exc:
            return _error_page(exc.detail, exc.status_code)
        if not return_url:
            if "login_error" in result:
                return JSONResponse(status_code=400, content={
                    "error": result["login_error"], "consent_request_id": result["request_id"]})
            return JSONResponse(content={"code": result["login"], "expires_in": result["expires_in"],
                                         "consent_request_id": result["request_id"]})
        if "login_error" in result:
            return RedirectResponse(with_query(return_url, login_error=result["login_error"]), status_code=302)
        return RedirectResponse(with_query(return_url, login=result["login"]), status_code=302)

    async def token(self, data: ConsentLoginTokenRequest):
        try:
            return await self.consent_login.exchange(data.code)
        except LifecycleError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
