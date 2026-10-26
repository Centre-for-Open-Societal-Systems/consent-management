"""Beneficiary API auth with national ID (eSignet).

Who may call the beneficiary API: a subject (farmer) logs in with the
national-ID provider and gets a token for the beneficiary API (/my/*, their own
consent requests). Not tied to any consent request; what the subject may see or
answer is BeneConsentService's. The provider part is SubjectAuthService's (same
client, PKCE, private_key_jwt, ID token + userinfo checks, the national ID read
from ``subject_auth_subject_claim``).

login → provider → this API's callback → one-time code → token:

* ``state`` is single-use (one atomic UPDATE claims it), the login expires after
  ``subject_auth_session_ttl_seconds``;
* with ``return_url`` (a web page) the callback redirects there with the code;
  the page may only be one of ``bene_auth_allowed_return_urls`` (no open
  redirect). Without it (an API client) the callback answers the code as JSON;
* the one-time code lasts ``bene_auth_exchange_ttl_seconds`` (only its hash is
  stored) and is swapped once for the token: the token never appears in a URL;
* the token is a JWS signed with the CM signing key (iss = aud =
  ``bene_auth_token_issuer``), carrying the national ID; it is checked here
  on every call (``verify``).
"""
import hashlib
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import jwt
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import update

from ..config import Settings
from ..db import async_session
from ..models import BeneLoginSession, SubjectAuthStatus
from .crypto_service import CryptoService
from .lifecycle_service import LifecycleError
from .subject_auth_service import SubjectAuthService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

EXCHANGED = "exchanged"
TOKEN_TYPE = "bene+jwt"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def with_query(url: str, **params) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in params] + list(params.items())
    return urlunparse(parts._replace(query=urlencode(query)))


class BeneAuthService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.provider = SubjectAuthService.get_component()
        self.crypto = CryptoService.get_component()

    @property
    def enabled(self) -> bool:
        return bool(_config.bene_auth_enabled)

    def _require_enabled(self):
        if not self.enabled:
            raise LifecycleError(404, "beneficiary API auth is not enabled")
        if not _config.bene_auth_redirect_uri:
            raise LifecycleError(503, "beneficiary API auth is not configured (missing: redirect_uri)")

    @staticmethod
    def return_url_allowed(url: str) -> bool:
        parts = urlparse(url or "")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        return any(url.startswith(prefix) for prefix in _config.bene_auth_allowed_return_urls if prefix)

    # ── login ────────────────────────────────────────────────────────────────

    async def start(self, return_url: Optional[str] = None) -> str:
        """A new login; returns the provider URL to send the subject to.
        ``return_url`` = the web page to come back to; None = an API client."""
        self._require_enabled()
        if return_url and not self.return_url_allowed(return_url):
            raise LifecycleError(400, "return_url is not an allowed page")
        meta = await self.provider.metadata(_config.bene_auth_redirect_uri)
        self.provider.private_key()  # fail now, not after the subject has logged in
        row = BeneLoginSession(
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
            meta, row.state, row.nonce, row.code_verifier, _config.bene_auth_redirect_uri
        )

    async def complete(self, state: Optional[str], code: Optional[str], provider_error: Optional[str] = None,
                       provider_error_description: Optional[str] = None) -> Tuple[str, dict]:
        """Handle the provider's redirect. Returns (return_url, result): result is
        ``{"login": <one-time code>, "expires_in": …}`` or ``{"login_error": …}``;
        return_url is "" for an API client. Raises LifecycleError when the state
        is unknown, used or expired."""
        self._require_enabled()
        if not state:
            raise LifecycleError(400, "missing state")
        async with async_session()() as session:
            row = (
                await session.execute(
                    update(BeneLoginSession)
                    .where(
                        BeneLoginSession.state == state,
                        BeneLoginSession.status == SubjectAuthStatus.pending.value,
                        BeneLoginSession.expires_at > _now(),
                    )
                    .values(status=SubjectAuthStatus.processing.value)
                    .returning(BeneLoginSession.id, BeneLoginSession.return_url,
                               BeneLoginSession.code_verifier, BeneLoginSession.nonce)
                )
            ).first()
            await session.commit()
        if row is None:
            raise LifecycleError(400, "this login link is unknown, already used or expired; start again")
        session_id, return_url, code_verifier, nonce = row
        try:
            if provider_error:
                raise LifecycleError(400, f"the provider refused: {provider_error_description or provider_error}")
            if not code:
                raise LifecycleError(400, "the provider returned no code")
            meta = await self.provider.metadata(_config.bene_auth_redirect_uri)
            merged, claims, id_token = await self.provider.exchange_code(
                meta, code, code_verifier, nonce, _config.bene_auth_redirect_uri
            )
            subject = self.provider.subject_of(merged)
        except Exception as exc:
            detail = exc.detail if isinstance(exc, LifecycleError) else f"login failed: {exc}"
            _logger.info("beneficiary API login %s failed: %s", session_id, detail)
            await self._finish(session_id, status=SubjectAuthStatus.failed.value, error=str(detail))
            return return_url, {"login_error": str(detail)}

        exchange = secrets.token_urlsafe(32)
        await self._finish(
            session_id, status=SubjectAuthStatus.success.value,
            subject_id_value="".join(subject.split()),
            verified_claims=self.provider.kept_claims(merged),
            issuer=claims.get("iss"),
            id_token_hash=_hash(id_token),
            exchange_hash=_hash(exchange),
            exchange_expires_at=_now() + timedelta(seconds=_config.bene_auth_exchange_ttl_seconds),
        )
        return return_url, {"login": exchange, "expires_in": _config.bene_auth_exchange_ttl_seconds}

    async def _finish(self, session_id: str, **values):
        async with async_session()() as session:
            await session.execute(
                update(BeneLoginSession).where(BeneLoginSession.id == session_id)
                .values(completed_at=_now(), **values)
            )
            await session.commit()

    async def exchange(self, code: str) -> dict:
        """One-time code → the subject's token (once)."""
        self._require_enabled()
        if not code:
            raise LifecycleError(400, "missing code")
        async with async_session()() as session:
            row = (
                await session.execute(
                    update(BeneLoginSession)
                    .where(
                        BeneLoginSession.exchange_hash == _hash(code),
                        BeneLoginSession.status == SubjectAuthStatus.success.value,
                        BeneLoginSession.exchange_expires_at > _now(),
                    )
                    .values(status=EXCHANGED)
                    .returning(BeneLoginSession)
                )
            ).scalars().first()
            await session.commit()
        if row is None:
            raise LifecycleError(400, "this login code is unknown, already used or expired; log in again")
        claims = row.verified_claims or {}
        now = int(time.time())
        ttl = _config.bene_auth_token_ttl_seconds
        token = self.crypto.sign_jws({
            "iss": _config.bene_auth_token_issuer,
            "aud": _config.bene_auth_token_issuer,
            "sub": row.subject_id_value,
            "subject_id_type": _config.bene_auth_subject_id_type,
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
            "subject_id": {"type": _config.bene_auth_subject_id_type, "value": row.subject_id_value},
            "name": claims.get("name"),
        }

    # ── token check ──────────────────────────────────────────────────────────

    @staticmethod
    def is_bene_token(token: str) -> bool:
        """True when the token claims to be ours (checked properly by verify)."""
        try:
            return (
                _config.bene_auth_enabled
                and jwt.get_unverified_header(token).get("typ") == TOKEN_TYPE
                and jwt.decode(token, options={"verify_signature": False}).get("iss")
                == _config.bene_auth_token_issuer
            )
        except jwt.PyJWTError:
            return False

    def verify(self, token: str) -> dict:
        """Signature (CM signing key), iss, aud, exp; raises jwt.PyJWTError."""
        claims = jwt.decode(
            token, self.crypto._private_key.public_key(), algorithms=[self.crypto.algorithm],
            issuer=_config.bene_auth_token_issuer, audience=_config.bene_auth_token_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        if not claims.get("subject_id_value"):
            raise jwt.InvalidTokenError("token does not name a subject")
        return claims
