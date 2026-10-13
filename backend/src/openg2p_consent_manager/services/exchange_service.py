"""Agri Stack exchange (G2P-5719): consent tokens between Consent Managers.

A consent token (called *receipt* in the API, settings and identifiers here)
is a short-lived JWS for one registry and one query — not the long-lived
Kantara consent receipt built by ReceiptService.

One codebase, two roles — both off unless configured:

- **Exchange role** (``receipt_issuer`` + ``receipt_presenters``): on a permit
  for ``/validate`` with ``issue_receipts=true``, sign one consent token per
  data controller with the CM signing key (same kid/alg, same JWKS) and keep a
  row per token so its status can be checked (revoked with the consent,
  expired by time).
- **Department role** (``trusted_receipt_issuers``): fetch and cache a trusted
  issuer's JWKS (refetched on an unknown kid) and check a token's status at
  its issuer (short cache). The decision itself is made in VerificationService.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings, TrustedReceiptIssuer
from ..db import async_session
from ..models import ArtefactStatus, ConsentArtefact, IssuedReceipt
from ..utils.jwks import jwk_to_pem_and_alg
from .crypto_service import CryptoService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

RECEIPT_TYP = "consent-receipt+jwt"
_JWKS_SUFFIX = "/.well-known/jwks.json"
_STATUS_PATH = "/consent/v1/receipts/{jti}/status"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_datetime(value) -> Optional[datetime]:
    """A consent-token time claim (epoch seconds or ISO-8601) as an aware datetime."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, OverflowError, OSError):
        return None
    return None


class ExchangeService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.crypto = CryptoService.get_component()
        # issuer → (fetched_at monotonic, {kid: (pem, alg)})
        self._jwks: dict[str, tuple[float, dict]] = {}
        # (issuer, jti) → (fetched_at monotonic, status)
        self._status: dict[tuple[str, str], tuple[float, str]] = {}
        # Swappable in tests (e.g. an ASGI transport to a second CM app).
        self.http_client_factory = lambda: httpx.AsyncClient(
            timeout=_config.receipt_fetch_timeout_seconds
        )

    # ── Exchange role: issuing ──────────────────────────────────────────────

    @staticmethod
    def issuing_refusal(presenter: Optional[str]) -> Optional[str]:
        """Why ``issue_receipts`` is refused for this caller, or None."""
        if not _config.receipt_issuer or not _config.receipt_presenters:
            return "receipt issuing is not enabled on this Consent Manager"
        if not presenter:
            return "issue_receipts needs partner_id (the caller that presents the receipts)"
        if presenter not in _config.receipt_presenters:
            return f"'{presenter}' is not a configured receipt presenter"
        return None

    async def issue(self, consent, decision, presenter: str, now: datetime) -> str:
        """Sign and record one consent token for ``decision`` (a permit for one
        controller of the partner's ``consent``)."""
        consent_exp = _aware(decision.valid_until)
        exp = min(consent_exp, now + timedelta(seconds=_config.receipt_ttl_seconds))
        jti = str(uuid.uuid4())
        claims = {
            "iss": _config.receipt_issuer,
            "jti": jti,
            "consent_id": decision.consent_id,
            "aud": decision.data_controller,
            "presenter": presenter,
            "partner": consent.aud,
            "sub": {"type": decision.subject_id.type, "value": decision.subject_id.value},
            "purpose": (consent.purpose or {}).get("code"),
            "scopes": list(decision.effective_data_scopes or []),
            "consent_issued_at": _aware(consent.issued_at).isoformat(),
            "consent_exp": int(consent_exp.timestamp()),
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        async with async_session()() as session:
            session.add(
                IssuedReceipt(
                    id=jti, consent_id=decision.consent_id,
                    data_controller=decision.data_controller, presenter=presenter,
                    partner=consent.aud, expires_at=exp,
                )
            )
            await session.commit()
        return self.crypto.sign_jws(claims, RECEIPT_TYP)

    async def receipt_status(self, jti: str) -> Optional[dict]:
        """active | revoked (its consent was revoked) | expired; None if unknown."""
        now = datetime.now(timezone.utc)
        async with async_session()() as session:
            receipt = await session.get(IssuedReceipt, jti)
            if receipt is None:
                return None
            artefact = await session.get(ConsentArtefact, receipt.consent_id)
        if artefact is None or artefact.status == ArtefactStatus.revoked.value:
            status = "revoked"
        elif (
            now >= _aware(receipt.expires_at)
            or artefact.status == ArtefactStatus.expired.value
            or _aware(artefact.valid_until) < now
        ):
            status = "expired"
        else:
            status = "active"
        return {"jti": jti, "status": status, "checked_at": now}

    # ── Department role: trusting an issuer ────────────────────────────────

    @staticmethod
    def trusted_issuer(iss: Optional[str]) -> Optional[TrustedReceiptIssuer]:
        return next(
            (t for t in _config.trusted_receipt_issuers if iss and t.issuer == iss), None
        )

    async def _fetch_jwks(self, trusted: TrustedReceiptIssuer) -> Optional[dict]:
        try:
            async with self.http_client_factory() as client:
                response = await client.get(trusted.jwks_url)
            response.raise_for_status()
            keys = {}
            for jwk in response.json().get("keys", []):
                try:
                    keys[jwk.get("kid")] = jwk_to_pem_and_alg(jwk)
                except ValueError as exc:
                    _logger.warning("Skipping JWK from %s: %s", trusted.issuer, exc)
            return keys
        except Exception as exc:
            _logger.warning("JWKS fetch for receipt issuer %s failed: %s", trusted.issuer, exc)
            return None

    async def issuer_key(
        self, trusted: TrustedReceiptIssuer, kid: Optional[str]
    ) -> Optional[tuple[str, str]]:
        """(PEM, alg) for ``kid`` from the issuer's JWKS — cached, and refetched
        (throttled) when the kid is unknown so a key rotation is picked up."""
        now = time.monotonic()
        entry = self._jwks.get(trusted.issuer)
        stale = entry is None or now - entry[0] >= _config.receipt_jwks_cache_ttl_seconds
        unknown = entry is not None and kid not in entry[1]
        cooled = entry is None or now - entry[0] >= _config.receipt_jwks_refresh_cooldown_seconds
        if stale or (unknown and cooled):
            keys = await self._fetch_jwks(trusted)
            if keys is not None:
                entry = (now, keys)
                self._jwks[trusted.issuer] = entry
        if entry is None:
            return None
        return entry[1].get(kid)

    @staticmethod
    def _status_url(trusted: TrustedReceiptIssuer, jti: str) -> Optional[str]:
        if trusted.status_url:
            return trusted.status_url.replace("{jti}", jti)
        if trusted.jwks_url.endswith(_JWKS_SUFFIX):
            return trusted.jwks_url[: -len(_JWKS_SUFFIX)] + _STATUS_PATH.format(jti=jti)
        return None

    async def remote_status(self, trusted: TrustedReceiptIssuer, jti: str) -> Optional[str]:
        """The consent token's status at its issuer (short cache); None if it could
        not be checked."""
        key = (trusted.issuer, jti)
        now = time.monotonic()
        cached = self._status.get(key)
        if cached and now - cached[0] < _config.receipt_status_cache_ttl_seconds:
            return cached[1]
        url = self._status_url(trusted, jti)
        if url is None:
            _logger.error("No status URL for receipt issuer %s", trusted.issuer)
            return None
        try:
            async with self.http_client_factory() as client:
                response = await client.get(url)
            if response.status_code == 404:
                status = "unknown"
            else:
                response.raise_for_status()
                status = response.json().get("status")
        except Exception as exc:
            _logger.warning("Receipt status check at %s failed: %s", trusted.issuer, exc)
            return None
        if len(self._status) > 10000:
            ttl = _config.receipt_status_cache_ttl_seconds
            self._status = {k: v for k, v in self._status.items() if now - v[0] < ttl}
        self._status[key] = (now, status)
        return status

    def clear_caches(self) -> None:
        self._jwks.clear()
        self._status.clear()
