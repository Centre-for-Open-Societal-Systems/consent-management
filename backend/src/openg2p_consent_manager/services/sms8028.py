"""Send one SMS straight to the 8028 gateway (no OAN Messaging Service in between).

Used for consent OTPs when ``sms8028_enabled`` is on. The 8028 contract (Integration Guide
v1.1, Postman collection v1.3):

* ``POST {base}/sms/send-sms``, JSON, with mutual TLS (the client certificate 8028 registered
  for us, by CN and SHA-256 fingerprint) **and** ``clientSecret`` in the body;
* ``phonenums`` is a list of exactly ``09XXXXXXXX`` / ``07XXXXXXXX`` numbers, all or nothing;
  one number per request here, so one request is one ``sms_id``;
* success is HTTP 201 ``{"success": true, "data": [{"sms_id": "...", "phone_number": "..."}]}``:
  the SMS is queued, not delivered. 8028 has no idempotency key.

Because 8028 cannot de-duplicate, a request that may have reached it is never repeated:

* nothing was sent (connection refused, DNS, TLS handshake) -> retried, then ``unavailable``;
* HTTP 400/413/415 or another 4xx the gateway refused -> ``rejected``;
* HTTP 401 (secret wrong or certificate not registered), 429 -> ``unavailable`` (a
  configuration or operator problem, not the subject's);
* a timeout after the request was written, any 5xx, or a 2xx we cannot read -> ``ambiguous``:
  8028 may hold the message, so it is not sent again.

Neither the phone, the text, the secret nor 8028's error text is ever logged; only
``http_<status>``. The certificate and key come from settings as PEM text, are written to a
private temporary directory just long enough to load them, and are removed at once.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import ssl
import tempfile
from pathlib import Path

import httpx

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

#: Tests may inject an httpx transport (e.g. ``httpx.MockTransport``); then no TLS client
#: certificate is needed.
_transport = None
_context_cache: dict[tuple[str, str], ssl.SSLContext] = {}


class Sms8028Error(Exception):
    """``kind`` is ``rejected``, ``unavailable`` or ``ambiguous`` (see the module docstring)."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        super().__init__(detail or kind)


def _pem(value: str) -> str:
    """PEM text as given, with literal ``\\n`` turned into newlines, or base64 of a PEM."""
    value = (value or "").strip().replace("\\n", "\n")
    if value and not value.startswith("-----BEGIN"):
        try:
            value = base64.b64decode(value, validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return ""
    return value


def _ssl_context(cert_pem: str, key_pem: str) -> ssl.SSLContext:
    """TLS context with the client certificate loaded; the PEM files exist only while loading."""
    cached = _context_cache.get((cert_pem, key_pem))
    if cached is not None:
        return cached
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # httpx depends on certifi; fall back to the system store
        context = ssl.create_default_context()
    with tempfile.TemporaryDirectory(prefix="sms8028-") as folder:
        os.chmod(folder, 0o700)
        cert_file, key_file = Path(folder, "client.crt"), Path(folder, "client.key")
        for path, text in ((cert_file, cert_pem), (key_file, key_pem)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
        context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    _context_cache.clear()  # one certificate at a time: a renewed one replaces the old
    _context_cache[(cert_pem, key_pem)] = context
    return context


def _same_number(sent: str, returned: object) -> bool:
    """8028 answers with the number in international form (251XXXXXXXXX) or as sent."""
    digits = "".join(ch for ch in str(returned or "") if ch.isdigit())
    tail = sent[-9:]
    return len(tail) == 9 and digits.endswith(tail)


def _read_success(response: httpx.Response, phone: str) -> str:
    """The ``sms_id`` of a queued message, or ``ambiguous``: 8028 may have queued it."""
    try:
        body = response.json()
        data = body.get("data")
        if (
            body.get("success") is True
            and isinstance(data, list)
            and len(data) == 1
            and _same_number(phone, data[0].get("phone_number"))
            and str(data[0].get("sms_id") or "").strip()
        ):
            return str(data[0]["sms_id"]).strip()
    except (ValueError, AttributeError, TypeError):
        pass
    raise Sms8028Error("ambiguous", "malformed_response")


async def send_sms(phone: str, text: str) -> str:
    """Queue ``text`` for ``phone`` at 8028 and return its ``sms_id``; raises ``Sms8028Error``."""
    secret = _config.sms8028_client_secret
    payload = {"clientSecret": secret, "smstext": text, "phonenums": [phone]}
    url = _config.sms8028_base_url.rstrip("/") + "/sms/send-sms"
    attempts = max(1, _config.sms8028_max_attempts)
    timeout = httpx.Timeout(_config.sms8028_timeout_seconds)
    verify: ssl.SSLContext | bool = True
    if _transport is None:
        try:
            verify = _ssl_context(
                _pem(_config.sms8028_client_cert_pem), _pem(_config.sms8028_client_key_pem)
            )
        except (ssl.SSLError, OSError, ValueError):
            _logger.error("8028 client certificate could not be loaded")
            raise Sms8028Error("unavailable", "client_certificate") from None
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(
                verify=verify, timeout=timeout, transport=_transport
            ) as client:
                response = await client.post(url, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # Nothing reached 8028 (refused, DNS, TLS handshake): safe to try again.
            if attempt < attempts:
                await asyncio.sleep(min(2.0, 0.5 * attempt))
                continue
            _logger.error("8028 unreachable after %d attempt(s)", attempt)
            raise Sms8028Error("unavailable", "connect") from None
        except httpx.HTTPError as exc:
            # The request was (or may have been) written: never repeat it.
            _logger.error("8028 outcome unknown: %s", type(exc).__name__)
            raise Sms8028Error("ambiguous", type(exc).__name__) from None
        status = response.status_code
        if 200 <= status < 300:
            return _read_success(response, phone)
        _logger.error("8028 answered http_%d", status)
        if status in (401, 429):
            raise Sms8028Error("unavailable", f"http_{status}")
        if 400 <= status < 500:
            raise Sms8028Error("rejected", f"http_{status}")
        raise Sms8028Error("ambiguous", f"http_{status}")
    raise Sms8028Error("unavailable", "connect")  # not reached
