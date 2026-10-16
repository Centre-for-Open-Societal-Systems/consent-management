from typing import Optional

from pydantic import BaseModel, Field

from .portal import PartnerConsentResponse


class VerifyOtpRequest(BaseModel):
    otp: str = Field(..., min_length=4, max_length=10)


class OtpConsentGranted(PartnerConsentResponse):
    """The consent created by a correct code; ``consent_id`` is what the
    partner presents (``/validate`` with ``consent_id``)."""

    receipt_id: Optional[str] = None
