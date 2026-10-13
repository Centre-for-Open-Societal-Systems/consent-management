from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class IssuedReceipt(BaseORMModelWithId):
    """A consent token (compact JWS) this CM issued in the exchange role.

    ``id`` is the token ``jti``. Its status follows the consent artefact it
    was issued from (revoked with it) and its own ``expires_at``. The JWS
    itself is not stored — it is self-verifying and held by the presenter.
    """

    __tablename__ = "issued_receipts"

    consent_id: Mapped[str] = mapped_column(String, index=True)
    data_controller: Mapped[str] = mapped_column(String(255))
    presenter: Mapped[str] = mapped_column(String(255))
    partner: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
