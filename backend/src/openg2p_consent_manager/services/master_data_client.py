"""Code lists read from the Master Data Service (partner portal choices).

The purposes and subject ID types a partner user picks in the partner portal
come from Master Data code lists (``mds_purpose_list_code``,
``mds_subject_id_type_list_code``), as Partner Management's policy form does:

    POST {mds_url}/catalogue/get_list_values   (OpenG2P request envelope)

read with a client-credentials token of the CM's own Keycloak client — never
the partner user's token: Master Data applies the caller's data policies, which
must not narrow a shared cache. Master Data reports errors as HTTP 200 with
``response_header.response_status = ERROR``; ``G2P-CAT-404`` means the list is
not in the country's Master Data.

Helpful only, never blocking: a list that is not configured, absent or cannot
be read (Master Data down) is ``None`` — the portal falls back to free text and
requests are accepted. A loaded list is enforced on portal requests.
"""
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_RETIRED = "RETIRED"
# A failed read is retried after at most this long (seconds).
_FAILURE_TTL = 30.0

# kind -> settings attribute holding its list code
LISTS = {
    "purposes": "mds_purpose_list_code",
    "subject_id_types": "mds_subject_id_type_list_code",
}


@dataclass(frozen=True)
class CodeList:
    list_code: str
    # [(code, label)], in Master Data's order
    values: tuple

    @property
    def codes(self) -> set:
        return {c for c, _ in self.values}


class MasterDataClient(BaseService):
    """Cached reader of Master Data code lists."""

    def __init__(self, name="", clock=time.monotonic, **kwargs):
        super().__init__(name, **kwargs)
        self._clock = clock
        # (kind, mds_url, list_code) -> (expires_at, CodeList | None)
        self._cache: dict[tuple, tuple[float, Optional[CodeList]]] = {}
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        # Tests swap in an httpx.MockTransport.
        self.transport: Optional[httpx.AsyncBaseTransport] = None

    def invalidate(self) -> None:
        self._cache.clear()
        self._token = None

    async def code_list(self, kind: str) -> Optional[CodeList]:
        """The loaded list for ``kind`` (``purposes`` | ``subject_id_types``);
        None = free text (not configured, absent, empty or unreadable)."""
        base = (_config.mds_url or "").strip().rstrip("/")
        code = (getattr(_config, LISTS[kind]) or "").strip()
        if not base or not code:
            return None
        key = (kind, base, code)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]
        try:
            result = await self._fetch(base, code)
            ttl = float(_config.mds_cache_ttl)
        except Exception as exc:  # noqa: BLE001 - never blocks: free text
            _logger.warning("Master Data list %s: %s; free text until it can be read", code, exc)
            result, ttl = None, min(float(_config.mds_cache_ttl), _FAILURE_TTL)
        self._cache[key] = (now + ttl, result)
        return result

    async def _access_token(self, client: httpx.AsyncClient) -> Optional[str]:
        if not _config.mds_token_url:
            return None
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        resp = await client.post(
            _config.mds_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": _config.mds_client_id,
                "client_secret": _config.mds_client_secret,
            },
        )
        if resp.status_code >= 400:
            raise ValueError(f"token request refused (HTTP {resp.status_code})")
        body = resp.json() or {}
        token = body.get("access_token")
        if not token:
            raise ValueError("token response has no access_token")
        try:
            lifetime = float(body.get("expires_in") or 300)
        except (TypeError, ValueError):
            lifetime = 300.0
        self._token = token
        self._token_expires_at = self._clock() + max(lifetime - min(30.0, lifetime / 10), 1.0)
        return token

    async def _fetch(self, base: str, list_code: str) -> Optional[CodeList]:
        envelope = {
            "request_header": {
                "sender_app_mnemonic": "consent-manager",
                "sender_app_url": "",
                "request_id": str(uuid.uuid4()),
                "request_timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            },
            "request_body": {
                "request_payload": {
                    "list_code": list_code,
                    "version": "latest",
                    "include_retired": False,
                }
            },
        }
        async with httpx.AsyncClient(
            timeout=_config.mds_timeout, transport=self.transport
        ) as client:
            for attempt in (1, 2):
                token = await self._access_token(client)
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp = await client.post(
                    f"{base}/catalogue/get_list_values", json=envelope, headers=headers
                )
                if resp.status_code == 401 and attempt == 1 and token:
                    self._token = None  # expired or revoked early: once more
                    continue
                break
        if resp.status_code >= 400:
            raise ValueError(f"HTTP {resp.status_code}")
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("unexpected response (not a JSON object)")
        header = body.get("response_header") or {}
        if str(header.get("response_status") or "").upper() == "ERROR":
            code = str(header.get("response_error_code") or "ERROR")
            if code.endswith("-404"):
                _logger.info("Master Data list %s is not configured: free text", list_code)
                return None
            raise ValueError(f"{code} {header.get('response_error_message') or ''}".strip())
        payload = (body.get("response_body") or {}).get("response_payload") or {}
        values = tuple(
            (str(v["value_code"]), str(v.get("display") or v["value_code"]))
            for v in payload.get("values") or []
            if v.get("value_code") and str(v.get("status") or "").upper() != _RETIRED
        )
        return CodeList(list_code=list_code, values=values) if values else None
