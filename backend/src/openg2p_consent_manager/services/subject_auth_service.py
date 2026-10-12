"""Subject authentication with national ID (consent scenario 1, phase 2).

A partner user starts the subject's login from the partner portal; the subject
authenticates at an OIDC provider such as eSignet (OTP, biometrics) in a popup;
the provider redirects to the CM's callback, which exchanges the code and checks
the result before recording it. Same design as the registry's beneficiary
authentication for credentials (authorization code + PKCE, private_key_jwt,
the subject matched on ``individual_id``), with these checks on every login:

* ``state`` is single-use: claimed with one atomic UPDATE, so a replayed or
  concurrent callback finds nothing to claim;
* the ID token's signature (provider JWKS), ``iss``, ``aud`` (the client ID),
  ``exp`` and ``nonce``;
* the userinfo answer comes straight from the provider over HTTPS with the
  access token, which is what makes it authentic (OIDC Core 5.3.2). When it is a
  JWT signed with a key the provider publishes, its signature is checked too;
  eSignet signs it with the identity system's key, which it does not publish,
  so then only its claims are checked. Either way its ``iss`` / ``aud`` (when
  present) must be the provider and this client, and its ``sub`` the ID token's;
* the subject claim (the national ID) must equal the request's subject ID.

Only acr / amr / auth_time and the configured claims are kept; the tokens are
not (a hash of the ID token is).
"""
import base64
import hashlib
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import select, update

from ..config import Settings
from ..db import async_session
from ..models import (
    AuthContext,
    ConsentRequest,
    RequestStatus,
    SubjectAuthSession,
    SubjectAuthStatus,
)
from .lifecycle_service import LifecycleError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_ID_TOKEN_ALGS = ["RS256", "PS256", "ES256"]
_METADATA_TTL = 3600
_JWKS_TTL = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(value: Any) -> str:
    return "".join(str(value or "").split())


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class SubjectAuthService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self._metadata: Optional[dict] = None
        self._metadata_at = 0.0
        self._jwks: dict = {}  # url -> (fetched_at, PyJWKSet)
        self._key = None

    # ── configuration ────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(_config.subject_auth_enabled)

    def http(self) -> httpx.AsyncClient:
        """The HTTP client for the provider (replaced in tests)."""
        return httpx.AsyncClient(timeout=_config.subject_auth_http_timeout_seconds)

    async def metadata(self) -> dict:
        """issuer and endpoints: the discovery document, overridden per value."""
        if self._metadata is None or time.monotonic() - self._metadata_at > _METADATA_TTL:
            found: dict = {}
            if _config.subject_auth_discovery_url:
                try:
                    async with self.http() as client:
                        r = await client.get(_config.subject_auth_discovery_url)
                        r.raise_for_status()
                        found = r.json()
                except Exception as exc:
                    if self._metadata is None:
                        raise LifecycleError(503, f"national ID provider unavailable: {exc}") from exc
                    found = self._metadata  # keep the last good document
            self._metadata = {
                "issuer": _config.subject_auth_issuer or found.get("issuer", ""),
                "authorization_endpoint": _config.subject_auth_authorize_url
                or found.get("authorization_endpoint", ""),
                "token_endpoint": _config.subject_auth_token_url or found.get("token_endpoint", ""),
                "userinfo_endpoint": _config.subject_auth_userinfo_url
                or found.get("userinfo_endpoint", ""),
                "jwks_uri": _config.subject_auth_jwks_url or found.get("jwks_uri", ""),
            }
            self._metadata_at = time.monotonic()
        missing = [k for k, v in self._metadata.items() if not v and k != "userinfo_endpoint"]
        missing += [name for name, value in (("client_id", _config.subject_auth_client_id),
                                             ("redirect_uri", _config.subject_auth_redirect_uri)) if not value]
        if missing:
            raise LifecycleError(
                503, f"national ID authentication is not configured (missing: {', '.join(missing)})"
            )
        return self._metadata

    def private_key(self):
        if self._key is None:
            pem = _config.subject_auth_private_key_pem
            if not pem and _config.subject_auth_private_key_path:
                try:
                    with open(_config.subject_auth_private_key_path) as fh:
                        pem = fh.read()
                except OSError:
                    pem = ""
            if not pem:
                raise LifecycleError(503, "national ID authentication: the client key is not available yet")
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            self._key = load_pem_private_key(pem.encode(), password=None)
        return self._key

    async def _jwk_set(self, url: str, refresh: bool = False) -> jwt.PyJWKSet:
        cached = self._jwks.get(url)
        if cached and not refresh and time.monotonic() - cached[0] < _JWKS_TTL:
            return cached[1]
        async with self.http() as client:
            r = await client.get(url)
            r.raise_for_status()
            keys = jwt.PyJWKSet.from_dict(r.json())
        self._jwks[url] = (time.monotonic(), keys)
        return keys

    async def _verify(self, token: str, meta: dict, audience: Optional[str]) -> dict:
        """Signature (provider JWKS; one refetch on an unknown kid), iss, aud, exp."""
        kid = jwt.get_unverified_header(token).get("kid")
        key = None
        for refresh in (False, True):
            keys = await self._jwk_set(meta["jwks_uri"], refresh=refresh)
            matches = [k for k in keys.keys if kid is None or k.key_id == kid]
            if matches:
                key = matches[0]
                break
        if key is None:
            raise ValueError(f"no provider key with kid {kid!r}")
        return jwt.decode(
            token, key.key, algorithms=_ID_TOKEN_ALGS, issuer=meta["issuer"],
            audience=audience, options={"verify_aud": audience is not None, "require": ["exp", "iss"]},
            leeway=30,
        )

    async def _userinfo_claims(self, token: str, meta: dict, client_id: str) -> dict:
        """Claims of a JWT userinfo answer (fetched from the provider over HTTPS).

        Signature checked when the key is one the provider publishes; otherwise
        (eSignet: the identity system's key) the HTTPS answer is what vouches for
        it. iss / aud / exp are checked whenever present."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            keys = await self._jwk_set(meta["jwks_uri"])
            if kid is None or any(k.key_id == kid for k in keys.keys):
                return await self._verify(token, meta, client_id if _aud_present(token) else None)
            info = jwt.decode(token, options={"verify_signature": False})
        except Exception as exc:
            raise LifecycleError(401, f"the userinfo answer is not valid: {exc}") from exc
        if info.get("iss") and info["iss"] != meta["issuer"]:
            raise LifecycleError(401, "the userinfo answer is not valid: unexpected issuer")
        aud = info.get("aud")
        if aud and client_id not in (aud if isinstance(aud, list) else [aud]):
            raise LifecycleError(401, "the userinfo answer is not valid: not for this client")
        if isinstance(info.get("exp"), (int, float)) and info["exp"] < time.time() - 30:
            raise LifecycleError(401, "the userinfo answer is not valid: expired")
        return info

    # ── start ────────────────────────────────────────────────────────────────

    def _check_request(self, req: Optional[ConsentRequest], audience: Optional[str]):
        if req is None or (audience is not None and req.partner_audience != audience):
            raise LifecycleError(404, "not_found")
        if req.status != RequestStatus.pending.value:
            raise LifecycleError(409, f"request is '{req.status}'")
        if req.subject_id_type not in _config.subject_auth_id_types:
            raise LifecycleError(
                422,
                f"a subject with ID type '{req.subject_id_type}' cannot authenticate with national ID; "
                f"create the request with one of {', '.join(_config.subject_auth_id_types)}",
            )

    async def start(self, request_id: str, audience: str, username: str) -> tuple[SubjectAuthSession, str]:
        """A new login for the request's subject; returns the session and the URL to open."""
        if not self.enabled:
            raise LifecycleError(404, "national ID authentication is not enabled")
        meta = await self.metadata()
        self.private_key()  # fail now, not after the subject has authenticated
        async with async_session()() as session:
            req = await session.get(ConsentRequest, request_id)
            self._check_request(req, audience)
            row = SubjectAuthSession(
                consent_request_id=request_id,
                state=secrets.token_urlsafe(32),
                nonce=secrets.token_urlsafe(32),
                code_verifier=secrets.token_urlsafe(48),
                status=SubjectAuthStatus.pending.value,
                started_by=username,
                expires_at=_now() + timedelta(seconds=_config.subject_auth_session_ttl_seconds),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        params = {
            "response_type": "code",
            "client_id": _config.subject_auth_client_id,
            "redirect_uri": _config.subject_auth_redirect_uri,
            "scope": _config.subject_auth_scope,
            "state": row.state,
            "nonce": row.nonce,
            "code_challenge": _challenge(row.code_verifier),
            "code_challenge_method": "S256",
        }
        if _config.subject_auth_acr_values:
            params["acr_values"] = _config.subject_auth_acr_values
        if _config.subject_auth_claims:
            params["claims"] = json.dumps(_config.subject_auth_claims, separators=(",", ":"))
        if _config.subject_auth_ui_locales:
            params["ui_locales"] = _config.subject_auth_ui_locales
        sep = "&" if "?" in meta["authorization_endpoint"] else "?"
        return row, f"{meta['authorization_endpoint']}{sep}{urlencode(params)}"

    # ── status ───────────────────────────────────────────────────────────────

    async def latest(self, request_id: str) -> Optional[SubjectAuthSession]:
        async with async_session()() as session:
            row = (
                await session.execute(
                    select(SubjectAuthSession)
                    .where(SubjectAuthSession.consent_request_id == request_id)
                    .order_by(SubjectAuthSession.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()
        return row

    @staticmethod
    def session_status(row: SubjectAuthSession) -> str:
        if row.status == SubjectAuthStatus.pending.value and _aware(row.expires_at) <= _now():
            return "expired"
        return row.status

    @staticmethod
    async def authenticated(session, request_id: str) -> Optional[AuthContext]:
        """The request's successful national-ID authentication, if any (latest)."""
        return (
            await session.execute(
                select(AuthContext)
                .join(SubjectAuthSession, SubjectAuthSession.auth_context_id == AuthContext.id)
                .where(
                    SubjectAuthSession.consent_request_id == request_id,
                    SubjectAuthSession.status == SubjectAuthStatus.success.value,
                )
                .order_by(AuthContext.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

    async def summaries(self, request_ids: list) -> dict:
        """request id → summary of its latest login (for the request views)."""
        if not request_ids:
            return {}
        async with async_session()() as session:
            rows = (
                await session.execute(
                    select(SubjectAuthSession, AuthContext)
                    .outerjoin(AuthContext, AuthContext.id == SubjectAuthSession.auth_context_id)
                    .where(SubjectAuthSession.consent_request_id.in_(request_ids))
                    .order_by(SubjectAuthSession.created_at)
                )
            ).all()
        out: dict = {}
        for row, ctx in rows:  # oldest first: the latest attempt wins, a success is kept
            summary = {
                "authentication_id": row.id,
                "status": self.session_status(row),
                "error": row.error,
                "started_by": row.started_by,
                "started_at": row.created_at,
                "completed_at": row.completed_at,
            }
            if ctx is not None:
                summary.update(
                    provider=ctx.auth_provider, method=ctx.auth_method,
                    acr=(ctx.verified_claims or {}).get("acr"), authenticated_at=ctx.auth_timestamp,
                )
            previous = out.get(row.consent_request_id)
            if previous and previous["status"] == "success" and summary["status"] != "success":
                summary["previous_success"] = {k: previous.get(k) for k in (
                    "authentication_id", "provider", "method", "acr", "authenticated_at")}
            out[row.consent_request_id] = summary
        return out

    # ── callback ─────────────────────────────────────────────────────────────

    async def _finish(self, session_id: str, status: str, error: Optional[str] = None,
                      context: Optional[AuthContext] = None) -> SubjectAuthSession:
        async with async_session()() as session:
            row = await session.get(SubjectAuthSession, session_id, with_for_update=True)
            if context is not None:
                session.add(context)
                row.auth_context_id = context.id
            row.status = status
            row.error = error
            row.completed_at = _now()
            await session.commit()
            await session.refresh(row)
            return row

    async def complete(self, state: Optional[str], code: Optional[str], provider_error: Optional[str] = None,
                       provider_error_description: Optional[str] = None) -> SubjectAuthSession:
        """Handle the provider's redirect. Raises LifecycleError when the state
        is unknown, used or expired (nothing is recorded then)."""
        if not state:
            raise LifecycleError(400, "missing state")
        async with async_session()() as session:
            claimed = (
                await session.execute(
                    update(SubjectAuthSession)
                    .where(
                        SubjectAuthSession.state == state,
                        SubjectAuthSession.status == SubjectAuthStatus.pending.value,
                        SubjectAuthSession.expires_at > _now(),
                    )
                    .values(status=SubjectAuthStatus.processing.value)
                    .returning(SubjectAuthSession.id)
                )
            ).scalar()
            await session.commit()
        if claimed is None:
            raise LifecycleError(400, "this login link is unknown, already used or expired; start again")
        try:
            context = await self._authenticate(claimed, code, provider_error, provider_error_description)
        except LifecycleError as exc:
            _logger.info("subject authentication %s failed: %s", claimed, exc.detail)
            return await self._finish(claimed, SubjectAuthStatus.failed.value, exc.detail)
        except Exception as exc:  # provider unreachable, malformed answer, …
            _logger.warning("subject authentication %s failed", claimed, exc_info=True)
            return await self._finish(claimed, SubjectAuthStatus.failed.value, f"authentication failed: {exc}")
        return await self._finish(claimed, SubjectAuthStatus.success.value, context=context)

    async def _authenticate(self, session_id: str, code: Optional[str], provider_error: Optional[str],
                            provider_error_description: Optional[str]) -> AuthContext:
        if provider_error:
            raise LifecycleError(400, f"the provider refused: {provider_error_description or provider_error}")
        if not code:
            raise LifecycleError(400, "the provider returned no code")
        async with async_session()() as session:
            row = await session.get(SubjectAuthSession, session_id)
            req = await session.get(ConsentRequest, row.consent_request_id)
        if req is None or req.status != RequestStatus.pending.value:
            raise LifecycleError(409, "the consent request is no longer open")
        meta = await self.metadata()
        client_id = _config.subject_auth_client_id

        now = int(time.time())
        assertion = jwt.encode(
            {"iss": client_id, "sub": client_id, "aud": meta["token_endpoint"], "jti": str(uuid.uuid4()),
             "iat": now, "exp": now + 300},
            self.private_key(), algorithm="RS256",
            headers={"kid": _config.subject_auth_key_kid or client_id},
        )
        async with self.http() as client:
            r = await client.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _config.subject_auth_redirect_uri,
                "client_id": client_id,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion,
                "code_verifier": row.code_verifier,
            })
            if r.status_code != 200:
                raise LifecycleError(502, f"the provider refused the code: HTTP {r.status_code} {r.text[:200]}")
            tokens = r.json()
            id_token = tokens.get("id_token")
            if not id_token:
                raise LifecycleError(502, "the provider returned no ID token")
            try:
                claims = await self._verify(id_token, meta, client_id)
            except Exception as exc:
                raise LifecycleError(401, f"the ID token is not valid: {exc}") from exc
            if claims.get("nonce") != row.nonce:
                raise LifecycleError(401, "the ID token's nonce does not match this login")

            info: dict = {}
            if meta.get("userinfo_endpoint") and tokens.get("access_token"):
                u = await client.get(meta["userinfo_endpoint"],
                                     headers={"Authorization": f"Bearer {tokens['access_token']}"})
                if u.status_code != 200:
                    raise LifecycleError(502, f"the provider's userinfo failed: HTTP {u.status_code}")
                if "jwt" in u.headers.get("content-type", ""):
                    info = await self._userinfo_claims(u.text.strip(), meta, client_id)
                else:
                    info = u.json()
                if info.get("sub") and info["sub"] != claims.get("sub"):
                    raise LifecycleError(401, "the userinfo answer is for another person")

        merged = {**claims, **info}
        subject = merged.get(_config.subject_auth_subject_claim)
        if not subject:
            raise LifecycleError(
                401, f"the provider did not return '{_config.subject_auth_subject_claim}' (the national ID)"
            )
        if _norm(subject) != _norm(req.subject_id_value):
            raise LifecycleError(403, "the authenticated person is not the request's subject")

        amr = merged.get("amr")
        method = (amr[0] if isinstance(amr, list) and amr else amr) or merged.get("acr")
        auth_time = merged.get("auth_time")
        kept = {k: merged[k] for k in ("acr", "amr", "auth_time") if k in merged}
        kept.update({k: merged[k] for k in _config.subject_auth_stored_claims if k in merged})
        return AuthContext(
            consent_request_id=req.id,
            auth_provider=_config.subject_auth_provider_name,
            auth_method=str(method)[:50] if method else None,
            auth_timestamp=datetime.fromtimestamp(auth_time, timezone.utc)
            if isinstance(auth_time, (int, float)) else _now(),
            issuer=claims.get("iss"),
            id_token_hash=hashlib.sha256(id_token.encode()).hexdigest(),
            token_validated=True,
            verified_claims=kept,
        )


def _aud_present(token: str) -> bool:
    return "aud" in jwt.decode(token, options={"verify_signature": False})

