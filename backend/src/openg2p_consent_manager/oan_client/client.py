"""Reusable async client for the OAN Messaging Service API (contract 3.0.0).

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION. Copy it into a calling
service (or vendor it) and wire it to that service's own configuration and outbox.

What it guarantees:

* Calls only the accepted endpoints: ``POST /api/v1/messages/sms``,
  ``POST /api/v1/messages/telegram`` and ``GET /api/v1/messages/{id}``.
* Every submission carries a caller-chosen ``Idempotency-Key``, kept separate from
  ``source_reference``. The request body is serialized **once**, and every retry resends
  the identical bytes with the identical key: same text, recipient, purpose and expiry.
* Retries only uncertain outcomes (timeouts, dropped connections, 5xx, API rate limiting)
  with bounded attempts and backoff. Never retries validation, authorization or
  conflicting-content errors.
* Never retries earlier than a server ``Retry-After``. If the server asks for a longer wait
  than the local wait budget, the call stops with ``OanRetryDeferred`` carrying
  ``retry_not_before``; the caller schedules the same stored delivery no earlier. Delays
  above 10 years are clamped (``MAX_RETRY_AFTER_SECONDS``); for submissions that always
  means ``ReconciliationRequired``.
* Never changes ``expires_at`` and never builds a replacement message or a new key.
* After the idempotency guarantee window it stops automatic retries
  (``ReconciliationRequired``).
* TLS is always verified (system CAs or a provided CA bundle). Redirects are never
  followed. Timeouts are bounded. Environment proxies are ignored.
* Logs and exception messages never contain message text, recipients, OTPs, chat IDs or
  credentials. Error codes and field names from a response are reduced to the contract's
  known value-free values; anything else becomes ``unrecognized``.

Retrying the OAN API is different from resending to an SMS gateway. A retry here is a
same-key replay: OAN returns the original message (HTTP 200) and sends nothing new.
Delivery to the gateway is OAN's job. OAN never resends a message whose provider outcome
is ``unknown``, and neither may the caller (no new key for the same intended delivery).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("oan_client")

CONTRACT_VERSION = "3.0.0"
API_PREFIX = "/api/v1/messages"
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{7,127}$")
SOURCE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$")
PURPOSES = frozenset({"otp", "transactional", "alert", "broadcast"})
#: Uncertain HTTP statuses: the message may or may not be stored. Retry with same key.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
#: Contract default guarantee window for completed messages (ServicePolicy.dedup_window).
DEFAULT_GUARANTEE_WINDOW = timedelta(days=30)


# ------------------------------------------------------------------ configuration


class Secret:
    """Opaque credential holder; never rendered by repr/str."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value or value != value.strip():
            raise ValueError("credential must be non-empty without surrounding space")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class ClientConfig:
    """Caller-supplied configuration. Load ``token`` from the caller's secret store."""

    base_url: str  #: e.g. the future dev URL; see docs/integrations (placeholder only)
    token: Secret  #: service credential issued to THIS calling service by OAN operators
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    #: Total attempts per call (first try + retries) for uncertain outcomes.
    max_attempts: int = 4
    backoff_base: float = 0.5
    backoff_cap: float = 8.0
    #: Local wait budget (seconds): the longest server ``Retry-After`` this client waits
    #: out in-process. A longer delay is not waited out or cut to the budget; the call
    #: stops with ``OanRetryDeferred`` and the caller retries no earlier than
    #: ``retry_not_before`` (see ``MAX_RETRY_AFTER_SECONDS`` for the one exception).
    max_retry_after: float = 30.0
    guarantee_window: timedelta = DEFAULT_GUARANTEE_WINDOW
    #: Optional CA bundle path for a private CA; verification can never be disabled.
    ca_bundle: str | None = None

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("base_url must be an https URL")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("base_url must not contain query, fragment or credentials")
        if not (0 < self.connect_timeout <= 60 and 0 < self.read_timeout <= 120):
            raise ValueError("timeouts must be positive and bounded")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if self.backoff_base <= 0 or self.backoff_cap < self.backoff_base:
            raise ValueError("backoff must be positive")
        if not (0 < self.max_retry_after <= 3600) or self.guarantee_window <= timedelta(0):
            raise ValueError("max_retry_after must be in (0, 3600]; guarantee_window positive")

    def __repr__(self) -> str:
        return f"ClientConfig(base_url={self.base_url!r}, token=<redacted>)"


# ---------------------------------------------------------------------- deliveries


def _check_common(key: str, source_reference: str, purpose: str,
                  expires_at: datetime | None, first_submitted_at: datetime) -> None:
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValueError("idempotency_key must be 8-128 chars of [A-Za-z0-9._:-]")
    if not SOURCE_REFERENCE_RE.fullmatch(source_reference):
        raise ValueError("source_reference has an invalid format")
    if purpose not in PURPOSES:
        raise ValueError("purpose must be otp, transactional, alert or broadcast")
    if expires_at is not None and expires_at.utcoffset() is None:
        raise ValueError("expires_at must be timezone-aware")
    if purpose == "otp" and expires_at is None:
        raise ValueError("expires_at is required for otp")
    if first_submitted_at.utcoffset() is None:
        raise ValueError("first_submitted_at must be timezone-aware")


@dataclass(frozen=True, repr=False)
class SmsDelivery:
    """One intended SMS delivery. Persist it (e.g. in an outbox) before the first attempt
    and reuse the SAME object for every retry, including after a process restart."""

    idempotency_key: str
    recipient: str
    text: str
    source_reference: str
    purpose: str
    expires_at: datetime | None
    #: When this delivery was first attempted; drives the guarantee window.
    first_submitted_at: datetime

    def __post_init__(self) -> None:
        _check_common(self.idempotency_key, self.source_reference, self.purpose,
                      self.expires_at, self.first_submitted_at)

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"recipient": self.recipient, "text": self.text,
                               "source_reference": self.source_reference,
                               "purpose": self.purpose}
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at.isoformat()  # exactly as supplied
        return out

    def __repr__(self) -> str:
        return (f"SmsDelivery(key=…{self.idempotency_key[-4:]}, "
                f"source_reference={self.source_reference!r}, purpose={self.purpose!r})")


@dataclass(frozen=True, repr=False)
class TelegramDelivery:
    """One intended Telegram delivery to an already-known chat. The bot is chosen by OAN
    from the caller's authorized configuration; the request cannot name one."""

    idempotency_key: str
    chat_id: int
    text: str
    source_reference: str
    purpose: str
    expires_at: datetime | None
    first_submitted_at: datetime

    def __post_init__(self) -> None:
        _check_common(self.idempotency_key, self.source_reference, self.purpose,
                      self.expires_at, self.first_submitted_at)
        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int):
            raise ValueError("chat_id must be an integer")

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"chat_id": self.chat_id, "text": self.text,
                               "source_reference": self.source_reference,
                               "purpose": self.purpose}
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at.isoformat()
        return out

    def __repr__(self) -> str:
        return (f"TelegramDelivery(key=…{self.idempotency_key[-4:]}, "
                f"source_reference={self.source_reference!r}, purpose={self.purpose!r})")


Delivery = SmsDelivery | TelegramDelivery


# ------------------------------------------------------------------------ results


@dataclass(frozen=True)
class Submission:
    """Result of a submission: 202 (``replayed=False``) or identical replay 200
    (``replayed=True``). ``status`` is OAN's current status, never a delivery claim
    unless it says ``delivered``."""

    id: str
    idempotency_key: str
    channel: str
    status: str
    source_reference: str
    created_at: str
    expires_at: str | None
    replayed: bool


STATUS_MEANINGS: Mapping[str, str] = {
    "accepted": "Stored by OAN. Nothing has been handed to a provider yet.",
    "sending": "OAN has started a provider call. Wait; do not resubmit.",
    "sent": ("The provider accepted the message (8028 queued it / Telegram accepted it). "
             "This is NOT handset delivery."),
    "delivered": "8028 reported handset delivery (SMS only; Telegram never reports this).",
    "failed": "Definite non-delivery as reported; see failure_code. Not retried by OAN.",
    "expired": "expires_at passed before any provider submission; it never left OAN.",
    "unknown": ("A provider submission was attempted but its outcome cannot be proven. "
                "OAN will not resend it. Do NOT submit again or use a new key for this "
                "delivery; reconcile operationally instead."),
}


@dataclass(frozen=True)
class MessageStatus:
    id: str
    idempotency_key: str
    channel: str
    purpose: str
    source_reference: str
    status: str
    final: bool
    delivery_receipts_supported: bool
    attempts: int
    failure_code: str | None
    provider_status: str | None
    recipient_masked: str
    raw: Mapping[str, Any] = field(repr=False)

    @property
    def meaning(self) -> str:
        text = STATUS_MEANINGS.get(self.status, "Unrecognized status; treat as unknown.")
        if self.status == "sent" and not self.delivery_receipts_supported:
            text += " Telegram has no delivery receipts: 'sent' is the strongest outcome."
        return text

    @property
    def may_resubmit(self) -> bool:
        """Always False. A same-key replay never creates a new message, and a new key for
        the same intended delivery would risk a duplicate. A new delivery decided by the
        originating service's own business rules (e.g. a user-requested OTP resend) is a
        different delivery with its own key, not a resubmission."""
        return False


# ------------------------------------------------------------------------- errors


class OanClientError(Exception):
    """Base class. Messages never include bodies, recipients, OTPs or credentials."""


class OanRejected(OanClientError):
    """A definite rejection (validation, authentication, permission, conflict, not found).
    Never retried automatically: fix the request or configuration."""

    def __init__(self, status: int, code: str, errors: tuple[tuple[str | None, str], ...]):
        self.status, self.code, self.errors = status, code, errors
        fields = ", ".join(f"{f or '-'}:{c}" for f, c in errors) or "-"
        super().__init__(f"OAN rejected the request: HTTP {status} {code} [{fields}]")


class OanConflict(OanRejected):
    """409: this Idempotency-Key is bound to a different body. Do not change the key or
    the body to 'fix' it: find out why the same delivery produced different content."""


class OanUncertain(OanClientError):
    """Retries exhausted on uncertain outcomes. The message MAY already be stored. Retry
    later with the SAME delivery (same key and body), within the guarantee window, and
    not before ``retry_not_before`` when the server gave one (``Retry-After``).

    For ``get_status`` the ``idempotency_key`` attribute holds the message id."""

    def __init__(self, key: str, attempts: int, last: str,
                 retry_not_before: datetime | None = None) -> None:
        self.idempotency_key, self.attempts, self.last = key, attempts, last
        self.retry_not_before = retry_not_before
        when = f"not before {retry_not_before.isoformat()} " if retry_not_before else ""
        super().__init__(f"outcome uncertain after {attempts} attempts (last: {last}); "
                         f"retry later {when}with the same key …{key[-4:]}")


class OanRetryDeferred(OanUncertain):
    """The server asked for a ``Retry-After`` longer than the local wait budget. Nothing
    was retried early. Keep the stored delivery unchanged (key, body, ``expires_at``,
    ``first_submitted_at``) and retry it no earlier than ``retry_not_before``."""

    def __init__(self, key: str, attempts: int, last: str,
                 retry_not_before: datetime) -> None:
        super().__init__(key, attempts, last, retry_not_before)


class ReconciliationRequired(OanClientError):
    """The guarantee window has passed, or the server's ``Retry-After`` ends at or after
    it: automatic submission retries stop. A completed message's binding may have been
    released, so a retry could create a NEW message. Reconcile with the message id (GET),
    or ask OAN operators to look the delivery up by caller and Idempotency-Key. There is
    no lookup-by-key API endpoint."""

    def __init__(self, key: str, retry_not_before: datetime | None = None) -> None:
        self.idempotency_key = key
        self.retry_not_before = retry_not_before
        super().__init__(f"idempotency guarantee window passed for key …{key[-4:]}; "
                         "stop retrying and reconcile")


class OanProtocolError(OanClientError):
    """Unexpected response shape or a redirect (redirects are never followed)."""


# ------------------------------------------------------------------------- client


#: Problem ``code`` values defined by contract 3.0.0 (``Problem.code``).
PROBLEM_CODES = frozenset({
    "unauthenticated", "forbidden", "not_found", "validation_failed",
    "idempotency_conflict", "payload_too_large", "unsupported_media_type",
    "service_unavailable",
})
#: ``ErrorItem.field`` locations the accepted API reports. Anything else becomes ``None``.
ERROR_FIELDS = frozenset({
    "recipient", "chat_id", "text", "source_reference", "purpose", "expires_at",
    "header.Idempotency-Key",
})
#: Value-free ``ErrorItem.code`` values the accepted API emits (oan_messaging.api and
#: storage expiry codes, plus the pydantic error types it passes through for these
#: fields). Anything else becomes ``unrecognized``.
ERROR_CODES = frozenset({
    # api/app.py and api/problems.py
    "method_not_allowed", "invalid_request", "outcome_uncertain_retry_same_key",
    "channel_not_permitted", "required", "invalid_format", "invalid_json",
    "purpose_not_permitted", "telegram_bot_not_configured", "payload_mismatch",
    "invalid_et_mobile", "too_long_for_single_sms", "too_long", "blank",
    "control_characters", "invalid_chat_id", "required_for_otp", "invalid",
    "unknown_field", "timezone_required",
    # storage expiry checks
    "not_in_future", "beyond_ceiling",
    # pydantic error types passed through unchanged
    "string_type", "string_too_short", "string_too_long", "int_type", "int_parsing",
    "int_from_float", "literal_error", "enum", "datetime_type", "datetime_parsing",
    "datetime_from_date_parsing", "model_type", "model_attributes_type", "dict_type",
    "json_invalid", "json_type",
})
UNRECOGNIZED = "unrecognized"
UNPARSEABLE = "unparseable_error"
#: At most this many error items are kept from one response.
MAX_ERROR_ITEMS = 16
#: Error bodies larger than this are not parsed at all.
MAX_ERROR_BODY_BYTES = 16 * 1024
#: Retry-After values above this (10 years) are CLAMPED to it, so date arithmetic stays
#: finite and storable. This is technically a shortening, and the only one:
#:
#: * submissions: 10 years is far beyond the 30-day guarantee window, so the call raises
#:   ``ReconciliationRequired`` and nothing is retried automatically;
#: * status reads: ``retry_not_before`` becomes ~10 years ahead, which in practice stops
#:   automatic polling for that message (operators reconcile it). A read cannot cause a
#:   duplicate send, so the clamp cannot affect delivery safety.
#:
#: A date that cannot be represented at all becomes ``datetime.max`` (UTC).
MAX_RETRY_AFTER_SECONDS = 10 * 365 * 86400.0
_DIGITS = re.compile(r"[0-9]{1,12}")


ErrorItems = tuple[tuple[str | None, str], ...]


def _parse_problem(resp: httpx.Response) -> tuple[str, ErrorItems]:
    """Reduce an error body to contract-known, value-free codes. Never returns a string
    taken from the response unless it is in the allow-lists above, so an echoed OTP,
    phone number, message text or token can never reach an exception or a log."""
    if len(resp.content) > MAX_ERROR_BODY_BYTES:
        return UNPARSEABLE, ()
    try:
        data = json.loads(resp.content)
    except (ValueError, RecursionError):  # includes JSONDecodeError and UnicodeDecodeError
        return UNPARSEABLE, ()
    if not isinstance(data, dict):
        return UNPARSEABLE, ()
    raw_code = data.get("code")
    code = raw_code if isinstance(raw_code, str) and raw_code in PROBLEM_CODES else UNRECOGNIZED
    raw_errors = data.get("errors")
    items: list[tuple[str | None, str]] = []
    if isinstance(raw_errors, list):
        for e in raw_errors[:MAX_ERROR_ITEMS]:
            if isinstance(e, dict):
                f, c = e.get("field"), e.get("code")
                item = (f if isinstance(f, str) and f in ERROR_FIELDS else None,
                        c if isinstance(c, str) and c in ERROR_CODES else UNRECOGNIZED)
            else:
                item = (None, UNRECOGNIZED)
            if item not in items:
                items.append(item)
    elif raw_errors is not None:
        items.append((None, UNRECOGNIZED))  # wrong shape: report it, never its content
    return code, tuple(items)


def _retry_after(resp: httpx.Response, now: datetime) -> float | None:
    """Seconds to wait from ``Retry-After`` (delta-seconds or HTTP-date), or ``None`` if
    absent or malformed. Never negative; clamped to ``MAX_RETRY_AFTER_SECONDS``."""
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isascii() and raw.isdigit():
        if not _DIGITS.fullmatch(raw):
            return MAX_RETRY_AFTER_SECONDS  # absurdly long: clamped (see constant)
        return min(float(int(raw)), MAX_RETRY_AFTER_SECONDS)
    if len(raw) > 64:
        return None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        return None
    seconds = (when - now).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _not_before(now: datetime, seconds: float) -> datetime:
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


class OanClient:
    """Async client. Use as ``async with OanClient(config) as client: ...``."""

    def __init__(
        self,
        config: ClientConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._cfg = config
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else True)
        self._http = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.read_timeout, connect=config.connect_timeout),
            verify=verify,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Authorization": f"Bearer {config.token.reveal()}",
                     "Accept": "application/json"},
        )

    async def __aenter__(self) -> OanClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def __repr__(self) -> str:
        return f"OanClient({self._cfg!r})"

    # ------------------------------------------------------------------ submit

    async def submit(self, delivery: Delivery) -> Submission:
        """Submit (or safely replay) one delivery. Returns on 202 or 200."""
        channel = "sms" if isinstance(delivery, SmsDelivery) else "telegram"
        body = json.dumps(delivery.body(), separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")  # serialized ONCE
        headers = {"Content-Type": "application/json",
                   "Idempotency-Key": delivery.idempotency_key}
        key = delivery.idempotency_key
        deadline = delivery.first_submitted_at + self._cfg.guarantee_window
        last = "none"
        for attempt in range(1, self._cfg.max_attempts + 1):
            if self._clock() >= deadline:
                raise ReconciliationRequired(key)
            try:
                resp = await self._http.post(f"{API_PREFIX}/{channel}", content=body,
                                             headers=headers, follow_redirects=False)
            except httpx.TransportError as exc:  # includes timeouts and dropped connections
                last = type(exc).__name__
                self._log(channel, attempt, last, key)
                await self._wait_or_raise(attempt, None, key, last, deadline)
                continue
            self._log(channel, attempt, str(resp.status_code), key)
            if resp.status_code in (200, 202):
                return self._submission(resp, replayed=resp.status_code == 200)
            if resp.status_code in RETRYABLE_STATUSES:
                last = f"http_{resp.status_code}"
                await self._wait_or_raise(attempt, _retry_after(resp, self._clock()),
                                          key, last, deadline)
                continue
            self._raise_for(resp)
        raise OanUncertain(key, self._cfg.max_attempts, last)  # pragma: no cover

    # ------------------------------------------------------------------ status

    async def get_status(self, message_id: str) -> MessageStatus:
        """``GET /api/v1/messages/{id}``. Read-only, so uncertain outcomes are retried
        (bounded). Never triggers a submission."""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", message_id):
            raise ValueError("message_id must be a UUID")
        last = "none"
        for attempt in range(1, self._cfg.max_attempts + 1):
            try:
                resp = await self._http.get(f"{API_PREFIX}/{message_id}",
                                            follow_redirects=False)
            except httpx.TransportError as exc:
                last = type(exc).__name__
                await self._wait_or_raise(attempt, None, message_id, last, None)
                continue
            if resp.status_code == 200:
                return self._status(resp)
            if resp.status_code in RETRYABLE_STATUSES:
                last = f"http_{resp.status_code}"
                await self._wait_or_raise(attempt, _retry_after(resp, self._clock()),
                                          message_id, last, None)
                continue
            self._raise_for(resp)
        raise OanUncertain(message_id, self._cfg.max_attempts, last)  # pragma: no cover

    # ------------------------------------------------------------------ helpers

    async def _wait_or_raise(self, attempt: int, retry_after: float | None, key: str,
                             last: str, deadline: datetime | None) -> None:
        """Wait before the next attempt, or raise. Never waits less than ``Retry-After``:
        a delay beyond the local budget raises ``OanRetryDeferred``, one ending at or after
        the guarantee window raises ``ReconciliationRequired``, and the last attempt
        raises ``OanUncertain`` (with ``retry_not_before`` when the server gave one)."""
        if retry_after is not None:
            not_before = _not_before(self._clock(), retry_after)
            if deadline is not None and not_before >= deadline:
                raise ReconciliationRequired(key, not_before)
            if attempt >= self._cfg.max_attempts:
                raise OanUncertain(key, attempt, last, not_before)
            if retry_after > self._cfg.max_retry_after:
                raise OanRetryDeferred(key, attempt, last, not_before)
            await self._sleep(retry_after)
            return
        if attempt >= self._cfg.max_attempts:
            raise OanUncertain(key, attempt, last)
        ceiling = min(self._cfg.backoff_cap, self._cfg.backoff_base * 2 ** (attempt - 1))
        await self._sleep(self._jitter(0.0, ceiling))

    def _raise_for(self, resp: httpx.Response) -> None:
        if 300 <= resp.status_code < 400:
            raise OanProtocolError(f"unexpected redirect (HTTP {resp.status_code}); "
                                   "redirects are never followed")
        code, errors = _parse_problem(resp)
        if resp.status_code == 409:
            raise OanConflict(409, code, errors)
        if 400 <= resp.status_code < 500:
            raise OanRejected(resp.status_code, code, errors)
        raise OanProtocolError(f"unexpected HTTP status {resp.status_code}")

    def _submission(self, resp: httpx.Response, *, replayed: bool) -> Submission:
        try:
            d = resp.json()
            return Submission(id=str(d["id"]), idempotency_key=str(d["idempotency_key"]),
                              channel=str(d["channel"]), status=str(d["status"]),
                              source_reference=str(d["source_reference"]),
                              created_at=str(d["created_at"]),
                              expires_at=_opt_str(d.get("expires_at")), replayed=replayed)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise OanProtocolError("unexpected submission response shape") from None

    def _status(self, resp: httpx.Response) -> MessageStatus:
        try:
            d = resp.json()
            return MessageStatus(
                id=str(d["id"]), idempotency_key=str(d["idempotency_key"]),
                channel=str(d["channel"]), purpose=str(d["purpose"]),
                source_reference=str(d["source_reference"]), status=str(d["status"]),
                final=bool(d["final"]),
                delivery_receipts_supported=bool(d["delivery_receipts_supported"]),
                attempts=int(d["attempts"]), failure_code=_opt_str(d.get("failure_code")),
                provider_status=_opt_str(d.get("provider_status")),
                recipient_masked=str(d["recipient_masked"]), raw=d)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise OanProtocolError("unexpected status response shape") from None

    @staticmethod
    def _log(channel: str, attempt: int, outcome: str, key: str) -> None:
        # Never the body, recipient, OTP, chat id or token; key shortened to 4 chars.
        log.info("oan submit channel=%s attempt=%s outcome=%s key=…%s",
                 channel, attempt, outcome, key[-4:])
