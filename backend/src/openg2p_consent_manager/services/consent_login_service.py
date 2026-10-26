"""Consent login with national ID (eSignet): the subject logs in for ONE
consent request.

Separate from the beneficiary API auth (BeneAuthService): own endpoints
(/consent/v1/consent-login/*), own sessions (``consent_login_sessions``), own
callback, own token. The provider part is SubjectAuthService's (same client,
PKCE, private_key_jwt, ID token + userinfo checks).

start (request id) → provider → callback → one-time code → token:

* a login starts only for a pending self-service request;
* ``state`` is single-use and bound to the request; the login expires after
  ``subject_auth_session_ttl_seconds``;
* the callback refuses a national ID that is not the request's subject;
* with ``return_url`` (a web page, one of ``consent_login_allowed_return_urls``)
  the callback redirects there with the code; without it (an API client) it
  answers the code as JSON;
* the token is a JWS signed with the CM signing key (iss = aud =
  ``consent_login_token_issuer``, ``crid`` = the request id), valid
  ``consent_login_token_ttl_seconds``, only to read, grant or decline that
  request.
"""
import logging
import secrets
import time
import uuid
from datetime import timedelta
from typing import Optional, Tuple
from urllib.parse import urlparse

import jwt
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import update

from ..config import Settings
from ..db import async_session
from ..models import ConsentLoginSession, SubjectAuthStatus
from .bene_auth_service import EXCHANGED, _hash, _now
from .bene_consent_service import BeneConsentService
from .crypto_service import CryptoService
from .lifecycle_service import LifecycleError, LifecycleService
from .subject_auth_service import SubjectAuthService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

TOKEN_TYPE = "consent-login+jwt"


def subject_id_types() -> list:
    """The national-ID types a consent login's subject ID counts for."""
    return list(dict.fromkeys([_config.consent_login_subject_id_type, *_config.subject_auth_id_types]))


class ConsentLoginService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.provider = SubjectAuthService.get_component()
        self.crypto = CryptoService.get_component()
        self.lifecycle = LifecycleService.get_component()
        self.bene_consents = BeneConsentService.get_component()

    @property
    def enabled(self) -> bool:
        return bool(_config.consent_login_enabled)

    def _require_enabled(self):
        if not self.enabled:
            raise LifecycleError(404, "consent login is not enabled")
        if not _config.consent_login_redirect_uri:
            raise LifecycleError(503, "consent login is not configured (missing: redirect_uri)")

    @staticmethod
    def return_url_allowed(url: str) -> bool:
        parts = urlparse(url or "")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        return any(url.startswith(prefix) for prefix in _config.consent_login_allowed_return_urls if prefix)

    async def _answerable(self, request_id: str):
        """The request, when its subject may log in to answer it."""
        req = await self.lifecycle.get_request(request_id) if request_id else None
        if req is None:
            raise LifecycleError(404, "consent request not found")
        if not self.bene_consents.self_service(req):
            raise LifecycleError(409, f"this request is confirmed through the partner ('{req.method}')")
        if req.status != "pending":
            raise LifecycleError(409, f"request is '{req.status}'")
        return req

    # ── login ────────────────────────────────────────────────────────────────

    async def start(self, request_id: str, return_url: Optional[str] = None) -> str:
        """A new consent login for one request; returns the provider URL.
        ``return_url`` = the web page to come back to; None = an API client."""
        self._require_enabled()
        if return_url and not self.return_url_allowed(return_url):
            raise LifecycleError(400, "return_url is not an allowed page")
        req = await self._answerable(request_id)
        meta = await self.provider.metadata(_config.consent_login_redirect_uri)
        self.provider.private_key()  # fail now, not after the subject has logged in
        row = ConsentLoginSession(
            consent_request_id=req.id,
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(48),
            return_url=return_url or "",
            status=SubjectAuthStatus.pending.value,
            expires_at=_now() + timedelta(seconds=_config.subject_auth_session_ttl_seconds),
        )
        async with async_session()() as session:
            session.add(row)
            await session.commit()
        return self.provider.authorization_url(
            meta, row.state, row.nonce, row.code_verifier, _config.consent_login_redirect_uri
        )

    async def complete(self, state: Optional[str], code: Optional[str], provider_error: Optional[str] = None,
                       provider_error_description: Optional[str] = None) -> Tuple[str, dict]:
        """Handle the provider's redirect. Returns (return_url, result): result is
        ``{"login": <one-time code>, "expires_in": …, "request_id": …}`` or
        ``{"login_error": …, "request_id": …}``; return_url is "" for an API
        client. Raises LifecycleError when the state is unknown, used or expired."""
        self._require_enabled()
        if not state:
            raise LifecycleError(400, "missing state")
        async with async_session()() as session:
            row = (
                await session.execute(
                    update(ConsentLoginSession)
                    .where(
                        ConsentLoginSession.state == state,
                        ConsentLoginSession.status == SubjectAuthStatus.pending.value,
                        ConsentLoginSession.expires_at > _now(),
                    )
                    .values(status=SubjectAuthStatus.processing.value)
                    .returning(ConsentLoginSession.id, ConsentLoginSession.return_url,
                               ConsentLoginSession.code_verifier, ConsentLoginSession.nonce,
                               ConsentLoginSession.consent_request_id)
                )
            ).first()
            await session.commit()
        if row is None:
            raise LifecycleError(400, "this login link is unknown, already used or expired; start again")
        session_id, return_url, code_verifier, nonce, request_id = row
        try:
            if provider_error:
                raise LifecycleError(400, f"the provider refused: {provider_error_description or provider_error}")
            if not code:
                raise LifecycleError(400, "the provider returned no code")
            meta = await self.provider.metadata(_config.consent_login_redirect_uri)
            merged, claims, id_token = await self.provider.exchange_code(
                meta, code, code_verifier, nonce, _config.consent_login_redirect_uri
            )
            subject = "".join(self.provider.subject_of(merged).split())
            req = await self._answerable(request_id)
            if not self.bene_consents.owns(req, subject, subject_id_types()):
                raise LifecycleError(403, "the national ID that logged in is not the request's subject")
        except Exception as exc:
            detail = exc.detail if isinstance(exc, LifecycleError) else f"login failed: {exc}"
            _logger.info("consent login %s (request %s) failed: %s", session_id, request_id, detail)
            await self._finish(session_id, status=SubjectAuthStatus.failed.value, error=str(detail))
            return return_url, {"login_error": str(detail), "request_id": request_id}

        exchange = secrets.token_urlsafe(32)
        await self._finish(
            session_id, status=SubjectAuthStatus.success.value,
            subject_id_value=subject,
            verified_claims=self.provider.kept_claims(merged),
            issuer=claims.get("iss"),
            id_token_hash=_hash(id_token),
            exchange_hash=_hash(exchange),
            exchange_expires_at=_now() + timedelta(seconds=_config.consent_login_exchange_ttl_seconds),
        )
        return return_url, {"login": exchange, "expires_in": _config.consent_login_exchange_ttl_seconds,
                            "request_id": request_id}

    async def _finish(self, session_id: str, **values):
        async with async_session()() as session:
            await session.execute(
                update(ConsentLoginSession).where(ConsentLoginSession.id == session_id)
                .values(completed_at=_now(), **values)
            )
            await session.commit()

    async def exchange(self, code: str) -> dict:
        """One-time code → the token for that one request (once)."""
        self._require_enabled()
        if not code:
            raise LifecycleError(400, "missing code")
        async with async_session()() as session:
            row = (
                await session.execute(
                    update(ConsentLoginSession)
                    .where(
                        ConsentLoginSession.exchange_hash == _hash(code),
                        ConsentLoginSession.status == SubjectAuthStatus.success.value,
                        ConsentLoginSession.exchange_expires_at > _now(),
                    )
                    .values(status=EXCHANGED)
                    .returning(ConsentLoginSession)
                )
            ).scalars().first()
            await session.commit()
        if row is None:
            raise LifecycleError(400, "this login code is unknown, already used or expired; log in again")
        claims = row.verified_claims or {}
        now = int(time.time())
        ttl = _config.consent_login_token_ttl_seconds
        token = self.crypto.sign_jws({
            "iss": _config.consent_login_token_issuer,
            "aud": _config.consent_login_token_issuer,
            "sub": row.subject_id_value,
            "crid": row.consent_request_id,
            "subject_id_type": _config.consent_login_subject_id_type,
            "subject_id_value": row.subject_id_value,
            "name": claims.get("name"),
            "acr": claims.get("acr"),
            "amr": claims.get("amr"),
            "auth_time": claims.get("auth_time"),
            "idp": _config.subject_auth_provider_name,
            "idp_iss": row.issuer,
            "idh": row.id_token_hash,
            "sid": row.id,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + ttl,
        }, typ=TOKEN_TYPE)
        return {
            "access_token": token, "token_type": "Bearer", "expires_in": ttl,
            "consent_request_id": row.consent_request_id,
            "subject_id": {"type": _config.consent_login_subject_id_type, "value": row.subject_id_value},
            "name": claims.get("name"),
        }

    # ── token check ──────────────────────────────────────────────────────────

    @staticmethod
    def is_consent_login_token(token: str) -> bool:
        """True when the token claims to be a consent login token (checked
        properly by verify)."""
        try:
            return (
                _config.consent_login_enabled
                and jwt.get_unverified_header(token).get("typ") == TOKEN_TYPE
                and jwt.decode(token, options={"verify_signature": False}).get("iss")
                == _config.consent_login_token_issuer
            )
        except jwt.PyJWTError:
            return False

    def verify(self, token: str) -> dict:
        """Signature (CM signing key), iss, aud, exp, crid; raises jwt.PyJWTError."""
        claims = jwt.decode(
            token, self.crypto._private_key.public_key(), algorithms=[self.crypto.algorithm],
            issuer=_config.consent_login_token_issuer, audience=_config.consent_login_token_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        if not claims.get("subject_id_value") or not claims.get("crid"):
            raise jwt.InvalidTokenError("token does not name a subject and a consent request")
        return claims
