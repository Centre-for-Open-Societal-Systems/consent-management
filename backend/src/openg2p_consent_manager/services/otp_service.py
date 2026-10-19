"""One-time codes for partner-raised consent requests.

The mechanism lives in ``otp_provider``, chosen by config (``otp_provider``:
``fayda`` or ``internal``). This class is the seam the rest of the service
talks to, so swapping backends never reaches the consent flow.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from .otp_provider import OtpError, OtpProvider, build_otp_provider

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

__all__ = ["OtpService", "OtpError", "OtpProvider"]


class OtpService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name if name else "OtpService", **kwargs)
        self._provider: OtpProvider = build_otp_provider()
        _logger.info("OTP provider: %s", self.provider_name)
        problem = self.readiness_error()
        if problem:
            _logger.warning("OTP consent requests will be refused: %s", problem)

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", "unknown")

    def set_provider(self, provider: OtpProvider) -> None:
        """Override the configured backend (tests, or a real Fayda / SMS client)."""
        self._provider = provider

    def readiness_error(self) -> Optional[str]:
        """Why no code may be issued, or None. A code needs a configured salt
        and some way to reach the subject (a delivering provider, or one of
        the test harnesses)."""
        if not _config.otp_salt:
            return "otp_salt is not configured"
        if not (
            getattr(self._provider, "delivers", False)
            or _config.otp_log_codes
            or _config.otp_publish_enabled
        ):
            return "no OTP delivery channel is configured"
        return None

    async def issue(self, request, destination: str) -> None:
        """Ask the provider for a code and stamp the request with its state."""
        problem = self.readiness_error()
        if problem:
            _logger.error("OTP not issued for consent request %s: %s", request.id, problem)
            raise OtpError(503, "otp_unavailable", problem)
        await self._provider.issue(request, destination)
        request.otp_send_count = (request.otp_send_count or 0) + 1
        request.otp_last_sent_at = datetime.now(timezone.utc)

    async def verify(self, request, code: str) -> None:
        """Raise OtpError unless the code is the live one for this request.
        Mutates ``otp_attempts`` on failure and sets ``otp_verified_at`` on
        success; the caller commits."""
        await self._provider.verify(request, code)
