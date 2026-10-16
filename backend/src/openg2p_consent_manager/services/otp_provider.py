"""Where the OTP actually comes from — selected by config, not by code.

Two providers, one interface:

``fayda`` (default)
    Fayda OTP semantics, in-process, via ``utils/fayda_otp``. A random code
    bound to ``(transactionID, individualId)``, stored only as a salted hash,
    compared in constant time. A port of the OAN Gen 1 mock
    (``g2p_ati_consent_mgt/utils/mock_fayda_otp_api.py``).

``internal``
    The generic equivalent: a random code with no Fayda framing.

Neither provider *delivers* anything yet: there is no SMS gateway, so both
write the code to the service log and say so. A real Fayda / SMS deployment
plugs in by implementing ``issue`` and ``verify``; nothing above this file
changes.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..config import Settings
from ..utils import fayda_otp

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class OtpError(Exception):
    """Carries an HTTP status so the controller does not have to map reasons."""

    def __init__(self, status: int, reason: str, detail: str = ""):
        self.status, self.reason, self.detail = status, reason, detail
        super().__init__(detail or reason)


class OtpProvider(Protocol):
    name: str

    async def issue(self, request, destination: str) -> None: ...
    async def verify(self, request, code: str) -> None: ...


def _check_window(request) -> None:
    """Shared preconditions: not already spent, not expired, attempts left."""
    if request.otp_verified_at is not None:
        raise OtpError(409, "already_verified", "this OTP has already been used")

    expires = request.otp_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires is None or expires < datetime.now(timezone.utc):
        raise OtpError(410, "otp_expired", "the OTP has expired; resend it")

    if (request.otp_attempts or 0) >= _config.otp_max_attempts:
        raise OtpError(429, "too_many_attempts", "attempt limit reached; resend the OTP")


async def _publish(request, code: str, individual_id: str) -> None:
    """Drop the code in the S3/MinIO test bucket when that is switched on.
    Imported here: services/__init__ imports this module while building the
    package. A convenience copy is never fatal."""
    try:
        from .otp_publisher import OtpPublisher
        publisher = OtpPublisher.get_component()
        if publisher is not None:
            await publisher.publish(request, code, individual_id=individual_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("OTP publish skipped: %s", exc)


def _count_failure(request) -> OtpError:
    request.otp_attempts = (request.otp_attempts or 0) + 1
    left = max(0, _config.otp_max_attempts - request.otp_attempts)
    return OtpError(401, "otp_invalid", "incorrect OTP; %d attempt(s) remaining" % left)


class FaydaOtpProvider:
    """Fayda's rules, applied here rather than called over HTTP. The
    transaction IS the consent-request row, so it is as durable as the request."""

    name = "fayda"

    async def issue(self, request, destination: str) -> None:
        transaction_id = request.id
        code = fayda_otp.generate_otp(_config.otp_length)
        request.otp_hash = fayda_otp.hash_otp(code, transaction_id, destination, _config.otp_salt)
        request.otp_reference = transaction_id
        request.otp_expires_at = datetime.now(timezone.utc) + timedelta(seconds=_config.otp_ttl_sec)
        request.otp_attempts = 0
        request.otp_verified_at = None
        request.otp_channel = _config.otp_channel
        # What the code was bound TO; verify() re-derives the hash from it.
        request.otp_destination = destination
        request.otp_provider = self.name
        await _publish(request, code, destination)
        _logger.warning(
            "[OTP] %s for individualId '%s' (consent request %s) - generated in-process "
            "with Fayda rules; NO message was delivered. Wire a real Fayda or an SMS "
            "gateway before any non-demo use.",
            code, destination, transaction_id,
        )

    async def verify(self, request, code: str) -> None:
        _check_window(request)
        try:
            fayda_otp.verify_otp(
                code=code,
                stored_hash=request.otp_hash,
                transaction_id=request.otp_reference or request.id,
                individual_id=request.subject_id_value,
                expected_individual_id=request.otp_destination,
                salt=_config.otp_salt,
            )
        except fayda_otp.FaydaError as exc:
            if exc.reason == "otp_transaction_unknown":
                raise OtpError(409, exc.reason, exc.message) from exc
            if exc.reason == "otp_subject_mismatch":
                _logger.error("Consent request %s: %s", request.id, exc.message)
                raise OtpError(500, exc.reason, exc.message) from exc
            if exc.reason == "otp_missing":
                raise OtpError(400, exc.reason, exc.message) from exc
            raise _count_failure(request) from exc
        request.otp_verified_at = datetime.now(timezone.utc)


class InternalOtpProvider:
    """A random code with no Fayda framing. The simplest thing that works."""

    name = "internal"

    @staticmethod
    def _hash(code: str, request_id: str) -> str:
        return hashlib.sha256(
            ("%s|%s|%s" % (code, request_id, _config.otp_salt)).encode("utf-8")
        ).hexdigest()

    async def issue(self, request, destination: str) -> None:
        code = fayda_otp.generate_otp(_config.otp_length)
        request.otp_hash = self._hash(code, request.id)
        request.otp_reference = request.id
        request.otp_expires_at = datetime.now(timezone.utc) + timedelta(seconds=_config.otp_ttl_sec)
        request.otp_attempts = 0
        request.otp_verified_at = None
        request.otp_channel = "log"
        request.otp_destination = destination
        request.otp_provider = self.name
        await _publish(request, code, destination)
        _logger.warning(
            "[OTP] %s for subject '%s' (consent request %s) - the internal provider only "
            "logs the code; no message was delivered.",
            code, destination, request.id,
        )

    async def verify(self, request, code: str) -> None:
        if not request.otp_hash:
            raise OtpError(409, "no_otp_issued", "no OTP has been issued for this request")
        _check_window(request)
        if not code:
            raise OtpError(400, "otp_missing", "Missing OTP value")
        if not hmac.compare_digest(self._hash(code, request.id), request.otp_hash):
            raise _count_failure(request)
        request.otp_verified_at = datetime.now(timezone.utc)


def build_otp_provider() -> OtpProvider:
    choice = (_config.otp_provider or "fayda").strip().lower()
    if choice == "fayda":
        return FaydaOtpProvider()
    if choice != "internal":
        _logger.warning("Unknown otp_provider '%s'; falling back to internal.", choice)
    return InternalOtpProvider()
