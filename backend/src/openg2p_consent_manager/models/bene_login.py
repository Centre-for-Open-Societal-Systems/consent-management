from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class BeneLoginSession(BaseORMModelWithId):
    """One beneficiary API auth login (national ID) of a subject. ``state`` is
    single-use (claimed with an atomic UPDATE), like a SubjectAuthSession; on
    success the callback hands back a one-time ``exchange`` code (only its hash
    is kept) that is swapped once for the subject's token. ``return_url`` is
    the web page to return to, "" for an API client."""

    __tablename__ = "bene_login_sessions"

    state: Mapped[str] = mapped_column(String(128), unique=True)
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    return_url: Mapped[str] = mapped_column(Text)
    # SubjectAuthStatus values; "exchanged" once the token was issued.
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    subject_id_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # acr / amr / auth_time + subject_auth_stored_claims, as for a request.
    verified_claims: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    issuer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    id_token_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    exchange_hash: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    exchange_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
