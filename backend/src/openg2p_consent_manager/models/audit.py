from typing import Optional

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseORMModelWithId


class DecisionLog(BaseORMModelWithId):
    """Append-only, immutable record of every validation decision.

    Written on both permit and deny for non-repudiation. Never updated.
    """

    __tablename__ = "decision_logs"
    __table_args__ = (
        Index("ix_decision_logs_partner_created", "partner_id", "created_at"),
        Index("ix_decision_logs_consent", "consent_id"),
    )

    partner_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    consent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    object_jti: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # The controller (registry) the decision was made for, when known.
    data_controller: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    decision: Mapped[str] = mapped_column(String(10))  # permit | deny
    reason_code: Mapped[str] = mapped_column(String(40))
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Hash of the request context — proves what was evaluated without storing PII.
    request_ctx_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Department role: the exchange consent token this decision was made on.
    receipt_jti: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    receipt_issuer: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class AuditLog(BaseORMModelWithId):
    """General append-only audit trail for entity state changes."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_entity", "entity_type", "entity_id"),)

    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
