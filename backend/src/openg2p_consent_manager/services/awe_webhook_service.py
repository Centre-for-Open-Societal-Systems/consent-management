import hashlib
import hmac
import logging
import time
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..db import async_session
from ..models import AweProcessedEvent
from .partner_service import PartnerService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

# Terminal AWE event types and how they map onto the onboarding gate.
_APPROVE_EVENTS = {"request_approved"}
_REJECT_EVENTS = {"request_rejected", "request_cancelled"}


class WebhookError(Exception):
    """Raised for webhook processing failures. ``status`` is the HTTP code CM
    should return so AWE retries (or not) per its documented contract."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AweWebhookService(BaseService):
    """Verifies and applies inbound AWE terminal webhooks.

    Auth is HMAC-only (no bearer): the signature proves the delivery came from
    the AWE holding CM's per-caller callback secret. Processing is idempotent —
    a duplicate ``event_id`` is ACKed without re-applying state.
    """

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.partners = PartnerService.get_component()

    # ── Signature ────────────────────────────────────────────────────────────

    def verify_signature(
        self, raw_body: bytes, timestamp: Optional[str], signature: Optional[str]
    ) -> None:
        """Raise WebhookError(401) unless the signature is valid and fresh.

        Scheme (matches AWE): X-Approval-Signature = "sha256=" +
        HMAC_SHA256(secret, "<timestamp>." + raw_body). The timestamp is inside
        the MAC so a captured body can't be replayed later.
        """
        secret = _config.awe_callback_hmac_secret
        if not secret:
            raise WebhookError(500, "AWE callback secret is not configured")
        if not signature or not timestamp:
            raise WebhookError(401, "Missing signature headers")

        try:
            ts = int(timestamp)
        except ValueError as exc:
            raise WebhookError(401, "Invalid timestamp header") from exc
        if abs(time.time() - ts) > _config.awe_webhook_max_skew_sec:
            raise WebhookError(401, "Signature timestamp outside allowed skew")

        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            f"{ts}.".encode("utf-8") + raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookError(401, "Signature verification failed")

    # ── Processing ───────────────────────────────────────────────────────────

    async def process(self, event_id: str, event: dict) -> str:
        """Apply a verified webhook. Returns a short status string. Raises
        WebhookError on unknown correlation (422) so AWE retries."""
        if not event_id:
            raise WebhookError(401, "Missing event id")

        event_type = event.get("event_type", "")
        request_id = event.get("request_id", "")
        artifact_id = event.get("artifact_id", "")

        # Idempotency: claim the event id first. A duplicate insert means we have
        # already processed (or are concurrently processing) this delivery.
        if not await self._claim_event(event_id, event_type, request_id):
            return "duplicate"

        # Only terminal events change state; others are recorded and ACKed.
        if event_type not in _APPROVE_EVENTS and event_type not in _REJECT_EVENTS:
            return "ignored"

        approved = event_type in _APPROVE_EVENTS
        actor = event.get("actor")
        if event_type == "request_cancelled":
            reason = "Cancelled in AWE" + (f" by {actor}" if actor else "")
        elif not approved:
            reason = "Rejected in AWE" + (f" by {actor}" if actor else "")
        else:
            reason = ""
        # artifact_id is the pending PartnerPolicy version id; request_id is the
        # AWE request. Applying the decision activates, rejects, or (if the
        # active policy changed meanwhile) marks that version stale.
        result = await self.partners.apply_policy_decision(
            awe_request_id=request_id,
            artifact_id=artifact_id,
            approved=approved,
            reason=reason,
        )
        if result is None:
            # Roll back the idempotency claim so a genuine retry can succeed once
            # the correlating policy version exists.
            await self._release_event(event_id)
            raise WebhookError(422, f"No policy for AWE request {request_id or artifact_id}")

        return result

    async def _claim_event(self, event_id: str, event_type: str, request_id: str) -> bool:
        async with async_session()() as session:
            session.add(
                AweProcessedEvent(
                    event_id=event_id, event_type=event_type, request_id=request_id
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def _release_event(self, event_id: str) -> None:
        async with async_session()() as session:
            result = await session.execute(
                select(AweProcessedEvent).where(AweProcessedEvent.event_id == event_id)
            )
            row = result.scalars().first()
            if row is not None:
                await session.delete(row)
                await session.commit()
