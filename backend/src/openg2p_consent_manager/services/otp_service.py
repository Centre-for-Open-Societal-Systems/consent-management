"""One-time codes for partner-raised consent requests.

The mechanism lives in ``otp_provider``, chosen by config (``otp_provider``:
``fayda`` or ``internal``). This class is the seam the rest of the service
talks to, so swapping backends never reaches the consent flow.
"""
import logging

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

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", "unknown")

    def set_provider(self, provider: OtpProvider) -> None:
        """Override the configured backend (tests, or a real Fayda / SMS client)."""
        self._provider = provider

    async def issue(self, request, destination: str) -> None:
        """Ask the provider for a code and stamp the request with its state."""
        await self._provider.issue(request, destination)

    async def verify(self, request, code: str) -> None:
        """Raise OtpError unless the code is the live one for this request.
        Mutates ``otp_attempts`` on failure and sets ``otp_verified_at`` on
        success; the caller commits."""
        await self._provider.verify(request, code)
