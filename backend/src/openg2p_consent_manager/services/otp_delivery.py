"""Deliver issued OTPs by SMS: through the OAN Messaging Service, or straight to 8028.

Two interchangeable transports, chosen by configuration: ``oan_delivery_enabled`` (the OAN
Messaging Service) or ``sms8028_enabled`` (the 8028 gateway itself, ``services/sms8028.py``).
Both on is refused: the code would be sent twice. Everything below applies to both.

Without it, the OTP providers only write the code to the service log (development). With
``oan_delivery_enabled`` every issued code is sent as one SMS:

* purpose ``otp``; ``expires_at`` is the code's own ``otp_expires_at``, passed
  unchanged. OAN never sends it after that moment.
* Idempotency key: ``delivery_key("dpi-consent", <request id>, "otp-consent-<issue>",
  "sms")``, where ``issue`` is the code's expiry in microseconds. Every re-issue is a new
  code and a new key; the client's own retries of one code reuse the key, so a farmer
  never receives the same code twice from OAN.
* The code is never logged, and neither is the phone.

If the code cannot be handed to OAN (no phone for the subject, rejected, OAN unreachable
after the client's retries), ``OtpError`` is raised and the API answers with that status.
A new request is cancelled; a failed resend leaves the previous code in place. When OAN
may already hold the message (``otp_delivery_unavailable``), the send still counts
against the resend cooldown and the per-request and per-subject budgets, so retries
during an OAN slowdown cannot flood the farmer.

Phone resolution: ``oan_phone_resolver = map`` reads ``oan_otp_phone_map`` (dev and
staging test numbers). Production needs a resolver approved by the identity owner, for
example a registry lookup of ``demographic_info.phone_number`` by FIN; plug it in at
``resolve_phone``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Settings
from ..oan_client import (
    ClientConfig,
    OanClient,
    OanClientError,
    OanRejected,
    Secret,
    SmsDelivery,
    delivery_key,
)
from . import sms8028
from .sms_length import fits_one_sms

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

TEXT = (
    "OAN: {code} is your code to allow {partner} to use your registry data. "
    "It expires in {minutes} minutes. Never share this code."
)
#: Used when the full text does not fit one SMS part, for example when the partner name has
#: Amharic letters (the limit is then 70 characters). No partner name, ASCII only.
SHORT_TEXT = (
    "OAN: {code} is your consent code. It expires in {minutes} minutes. Never share this code."
)
PARTNER_MAX = 30
#: Tests may inject an httpx transport (e.g. ``httpx.MockTransport``).
_transport = None


def _otp_error(status: int, reason: str, detail: str):
    from .otp_provider import OtpError  # late: otp_provider imports this module

    return OtpError(status, reason, detail)


def resolve_phone(subject_value: str, individual_id: str | None) -> str | None:
    if _config.oan_phone_resolver != "map":
        raise _otp_error(
            500, "otp_resolver_unsupported", "configured phone resolver is not available"
        )
    phones = _config.oan_otp_phones
    return phones.get(subject_value) or (phones.get(individual_id) if individual_id else None)


async def _partner_name(partner_id: str) -> str:
    try:
        from .partner_service import PartnerService

        partner = await PartnerService.get_component().get_partner(partner_id)
        name = (getattr(partner, "name", None) or "").strip()
    except Exception:  # noqa: BLE001 - a display name never blocks the code
        name = ""
    name = name or "a service provider"
    return name if len(name) <= PARTNER_MAX else name[: PARTNER_MAX - 1].rstrip() + "."


def render_otp_text(code: str, partner: str, minutes: int) -> str:
    """The OTP text, guaranteed to fit one SMS part (OAN refuses anything longer)."""
    text = TEXT.format(code=code, partner=partner, minutes=minutes)
    if fits_one_sms(text):
        return text
    short = SHORT_TEXT.format(code=code, minutes=minutes)
    if fits_one_sms(short):
        return short
    raise _otp_error(500, "otp_text_too_long", "the OTP text does not fit one SMS part")


async def _send_direct(request, phone: str, text: str) -> str:
    """Hand the text to 8028. One attempt per code: 8028 cannot de-duplicate, so a request that
    may have reached it is never repeated (an ambiguous outcome counts as sent for the budgets)."""
    try:
        sms_id = await sms8028.send_sms(phone, text)
    except sms8028.Sms8028Error as exc:
        if exc.kind == "rejected":
            _logger.error("OTP delivery rejected by 8028 (request %s): %s", request.id, exc)
            raise _otp_error(
                502, "otp_delivery_rejected", "the SMS service refused the code"
            ) from None
        _logger.error("OTP delivery unavailable (request %s): %s", request.id, exc.kind)
        raise _otp_error(
            503, "otp_delivery_unavailable", "the SMS service is unavailable; ask for a new code"
        ) from None
    _logger.info("OTP handed to 8028 for consent request %s (sms %s)", request.id, sms_id)
    return sms_id


async def deliver_otp(request, code: str, individual_id: str | None = None) -> str | None:
    """Send ``code`` for ``request``. Returns the message id, or None when delivery is off."""
    if not (_config.oan_delivery_enabled or _config.sms8028_enabled):
        return None
    if _config.oan_delivery_enabled and _config.sms8028_enabled:
        raise _otp_error(500, "otp_delivery_misconfigured", "two SMS transports are enabled")
    direct = _config.sms8028_enabled
    if direct and not _config.sms8028_ready:
        raise _otp_error(500, "otp_delivery_misconfigured", "8028 delivery is not configured")
    if not direct and (not _config.oan_base_url or not _config.oan_token):
        raise _otp_error(500, "otp_delivery_misconfigured", "OAN delivery is not configured")
    transaction = str(request.id)
    expires_at = request.otp_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    phone = resolve_phone(request.subject_id_value, individual_id)
    if not phone:
        raise _otp_error(
            422, "otp_destination_unknown", "no phone number is registered for this subject"
        )
    minutes = max(1, round((expires_at - now).total_seconds() / 60))
    text = render_otp_text(code, await _partner_name(request.partner_id), minutes)
    if direct:
        return await _send_direct(request, phone, text)
    issue = str(int(expires_at.timestamp() * 1_000_000))
    delivery = SmsDelivery(
        idempotency_key=delivery_key("dpi-consent", transaction, f"otp-consent-{issue}", "sms"),
        recipient=phone,
        text=text,
        source_reference=f"dpi-consent:{transaction}:otp-consent",
        purpose="otp",
        expires_at=expires_at,
        first_submitted_at=now,
    )
    try:
        cfg = ClientConfig(
            base_url=_config.oan_base_url,
            token=Secret(_config.oan_token),
            connect_timeout=_config.oan_timeout_seconds,
            read_timeout=_config.oan_timeout_seconds,
            max_attempts=max(1, _config.oan_max_attempts),
            ca_bundle=_config.oan_ca_bundle or None,
        )
    except ValueError:  # e.g. a non-HTTPS base URL; the message never holds the token
        raise _otp_error(
            500, "otp_delivery_misconfigured", "OAN delivery is not configured"
        ) from None
    try:
        client = OanClient(cfg, transport=_transport) if _transport is not None else OanClient(cfg)
        async with client:
            submission = await client.submit(delivery)
    except OanRejected as exc:
        _logger.error(
            "OTP delivery rejected by OAN (request %s): HTTP %s %s",
            request.id,
            exc.status,
            exc.code,
        )
        raise _otp_error(502, "otp_delivery_rejected", "the SMS service refused the code") from None
    except (OanClientError, OSError) as exc:
        _logger.error("OTP delivery unavailable (request %s): %s", request.id, type(exc).__name__)
        raise _otp_error(
            503, "otp_delivery_unavailable", "the SMS service is unavailable; ask for a new code"
        ) from None
    _logger.info("OTP handed to OAN for consent request %s (message %s)", request.id, submission.id)
    return submission.id
