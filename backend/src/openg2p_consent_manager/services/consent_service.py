import logging
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import func, select

from ..config import Settings
from ..db import async_session
from ..models import (
    ArtefactStatus,
    ConsentArtefact,
    ConsentReceipt,
    RevocationRecord,
)

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ConsentService(BaseService):
    """Status, receipts, revocation, expiry, and subject-scoped queries."""

    async def get_status(self, consent_id: str) -> Optional[dict]:
        async with async_session()() as session:
            artefact = await session.get(ConsentArtefact, consent_id)
            if artefact is None:
                return None
            now = datetime.now(timezone.utc)
            status = artefact.status
            # Lazy expiry — treat a lapsed artefact as expired even before the job runs.
            if status == ArtefactStatus.active.value and _aware(artefact.valid_until) < now:
                artefact.status = ArtefactStatus.expired.value
                artefact.expired_at = now
                await session.commit()
                status = artefact.status
            return {
                "consent_id": artefact.id,
                "status": status,
                "valid_until": artefact.valid_until,
                "checked_at": now,
            }

    async def get_receipt(self, receipt_id: str) -> Optional[ConsentReceipt]:
        async with async_session()() as session:
            return await session.get(ConsentReceipt, receipt_id)

    async def get_receipt_by_consent(self, consent_id: str) -> Optional[ConsentReceipt]:
        async with async_session()() as session:
            result = await session.execute(
                select(ConsentReceipt).where(ConsentReceipt.consent_id == consent_id)
            )
            return result.scalars().first()

    async def revoke(
        self, consent_id: str, originated_by: str, reason: Optional[str],
        subject_claims: Optional[dict] = None,
    ) -> Optional[ConsentArtefact]:
        """Revoke an active consent. Append-only; raises ValueError on conflict.

        Returns None if not found. If subject_claims is given, enforces that the
        caller is the subject (used by the subject API).
        """
        async with async_session()() as session:
            artefact = await session.get(ConsentArtefact, consent_id)
            if artefact is None:
                return None
            if subject_claims is not None and (
                artefact.subject_id_type != subject_claims.get("subject_id_type")
                or artefact.subject_id_value != subject_claims.get("subject_id_value")
            ):
                raise PermissionError("not the subject of this consent")
            if artefact.status != ArtefactStatus.active.value:
                raise ValueError(f"consent already '{artefact.status}'")

            now = datetime.now(timezone.utc)
            artefact.status = ArtefactStatus.revoked.value
            artefact.revoked_at = now
            session.add(
                RevocationRecord(
                    consent_id=consent_id, originated_by=originated_by, reason=reason
                )
            )
            await session.commit()
            await session.refresh(artefact)
            return artefact

    async def expire_stale(self) -> int:
        """Mark active artefacts past valid_until as expired. Safe to run from a
        CronJob across replicas — each row transitions once and idempotently.
        """
        now = datetime.now(timezone.utc)
        async with async_session()() as session:
            result = await session.execute(
                select(ConsentArtefact).where(
                    ConsentArtefact.status == ArtefactStatus.active.value,
                    ConsentArtefact.valid_until < now,
                )
            )
            stale = result.scalars().all()
            for artefact in stale:
                artefact.status = ArtefactStatus.expired.value
                artefact.expired_at = now
            await session.commit()
            count = len(stale)
        if count:
            _logger.info("Expired %d stale consent artefacts", count)
        return count

    # ── Subject-scoped queries (GDPR access) ─────────────────────────────────

    async def list_subject_consents(
        self, subject_id_type: str, subject_id_value: str,
        status: Optional[str] = None, page: int = 1, size: int = 20,
    ) -> dict:
        async with async_session()() as session:
            base = select(ConsentArtefact).where(
                ConsentArtefact.subject_id_type == subject_id_type,
                ConsentArtefact.subject_id_value == subject_id_value,
            )
            if status:
                base = base.where(ConsentArtefact.status == status)
            total = int(
                (await session.execute(
                    select(func.count()).select_from(base.subquery())
                )).scalar() or 0
            )
            rows = (await session.execute(
                base.order_by(ConsentArtefact.created_at.desc())
                .offset((page - 1) * size).limit(size)
            )).scalars().all()
            pages = max(1, (total + size - 1) // size)
            return {"items": rows, "total": total, "page": page, "size": size, "pages": pages}

    async def get_subject_artefact(
        self, consent_id: str, subject_id_type: str, subject_id_value: str
    ) -> Optional[ConsentArtefact]:
        async with async_session()() as session:
            artefact = await session.get(ConsentArtefact, consent_id)
            if artefact is None or (
                artefact.subject_id_type != subject_id_type
                or artefact.subject_id_value != subject_id_value
            ):
                return None
            return artefact
