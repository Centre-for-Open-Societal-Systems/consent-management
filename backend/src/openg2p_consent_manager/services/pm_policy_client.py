"""Partner access policies, read from Partner Management (PM).

PM owns who a partner is, its keys and what it may access (its policies). The
Consent Manager only reads the active policies, from PM's internal policy API
(no auth, internal gateway — like ``/keys``):

    GET {partner_mgmt_api_url}/policies/{pm_partner_id}?data_controller=<c>
    GET {partner_mgmt_api_url}/policies/{pm_partner_id}

A partner has one scope policy per data controller (a registry); no filter
lists all its active policies (other kinds of policy PM may hold, without a
data controller, are not the Consent Manager's and are left out). PM answers
404 when the partner is unknown or not active or has no matching active policy
(the same body for every case).

Caching (per pod, per (partner, data controller | all)), as for partner keys:

* soft TTL — refresh window: min(PM's ``Cache-Control: max-age``,
  ``partner_policy_cache_ttl_seconds``);
* hard TTL — while PM is unreachable, the last answer PM gave is served up to
  ``partner_policy_hard_ttl_seconds`` after it was fetched; then fail closed
  (``PolicyUnavailable`` → deny ``policy_unavailable``);
* negative TTL — a 404 is remembered for ``partner_policy_negative_ttl_seconds``;
  a failed fetch is not retried for that long either (no fetch per request
  while PM is down);
* single-flight — concurrent misses for one key share one HTTP call.
"""
import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_MAX_AGE = re.compile(r"(?:^|[,\s])max-age\s*=\s*(\d+)", re.IGNORECASE)
# Cache-key kinds: one scope policy, all of a partner's.
_CONTROLLER = "data_controller"
_ALL = "all"


def pm_partner_id(audience: str) -> str:
    """A partner's PM id from its audience / DCI sender_id, by convention:
    ``bank-a`` → ``PARTNER_BANK_A`` (keys and policies)."""
    return "PARTNER_" + (audience or "").strip().upper().replace("-", "_")


class PolicyUnavailable(Exception):
    """PM could not be reached (or answered wrongly) and no usable answer is
    cached: callers fail closed."""


@dataclass(frozen=True)
class PmPolicy:
    """One active policy of a partner, as PM's ``PolicyOut``. Exposes the
    attributes ``PolicyService.evaluate`` reads (as the CM's own policy did)."""

    partner_id: str
    data_controller: Optional[str]
    version: int
    allowed_data_scopes: list = field(default_factory=list)
    allowed_purposes: list = field(default_factory=list)
    allowed_subject_id_types: list = field(default_factory=list)
    allowed_signing_algs: list = field(default_factory=list)
    max_validity_duration: Optional[str] = None
    fetch_type: str = "oneshot"
    max_fetch_frequency: Optional[str] = None
    data_life: Optional[str] = None
    effective_from: Optional[str] = None
    id: Optional[str] = None
    # The partner may present a consent it collected and signed itself
    # (consent_jws). Default false: only consents the CM holds are accepted.
    allow_partner_consents: bool = False

    @property
    def ref(self) -> str:
        """The policy's key in PM, as ``kind:key``:
        ``data_controller:farmer-registry``."""
        return f"data_controller:{self.data_controller or ''}"

    @classmethod
    def from_pm(cls, data: dict) -> "PmPolicy":
        def strings(key):
            return [str(v) for v in (data.get(key) or []) if v is not None and str(v)]

        return cls(
            partner_id=str(data.get("partner_id") or ""),
            data_controller=data.get("data_controller") or None,
            version=int(data["version"]),
            allowed_data_scopes=strings("allowed_data_scopes"),
            allowed_purposes=strings("allowed_purposes"),
            allowed_subject_id_types=strings("allowed_subject_id_types"),
            allowed_signing_algs=strings("allowed_signing_algs"),
            max_validity_duration=data.get("max_validity_duration") or None,
            fetch_type=data.get("fetch_type") or "oneshot",
            max_fetch_frequency=data.get("max_fetch_frequency") or None,
            data_life=data.get("data_life") or None,
            effective_from=(
                str(data["effective_from"]) if data.get("effective_from") else None
            ),
            id=str(data["id"]) if data.get("id") else None,
            allow_partner_consents=data.get("allow_partner_consents") is True,
        )


def policy_fields(policy) -> dict:
    """The full reference of the PM policy (version) a decision or consent
    used, as stored on decision logs, artefacts and grants: ``policy_version``,
    ``policy_id`` (PM's id of that version) and ``policy_ref`` (``kind:key``).
    All None without a policy."""
    if policy is None:
        return {"policy_version": None, "policy_id": None, "policy_ref": None}
    return {
        "policy_version": policy.version,
        "policy_id": getattr(policy, "id", None),
        "policy_ref": getattr(policy, "ref", None),
    }


def common_policy_fields(grants: list) -> dict:
    """The policy reference shared by all ``grants`` (e.g. one grant); all
    None when they differ."""
    keys = ("policy_version", "policy_id", "policy_ref")
    refs = {tuple(g.get(k) for k in keys) for g in grants or []}
    if len(refs) == 1:
        return dict(zip(keys, refs.pop()))
    return dict.fromkeys(keys)


class _Entry:
    __slots__ = ("value", "fetched_at", "last_attempt", "ttl")

    def __init__(self, value, fetched_at, last_attempt, ttl):
        # value: list[PmPolicy] (PM's answer; [] for a 404), or None when no
        # answer was ever obtained (failed fetch, nothing to fall back on).
        self.value = value
        self.fetched_at = fetched_at  # monotonic time of the last answer from PM
        self.last_attempt = last_attempt  # monotonic time of the last fetch attempt
        self.ttl = ttl  # soft TTL of this answer


class PmPolicyClient(BaseService):
    """Cached reader of partner policies from Partner Management."""

    def __init__(self, name="", clock=time.monotonic, **kwargs):
        super().__init__(name, **kwargs)
        self._clock = clock
        self._cache: dict[tuple, _Entry] = {}
        self._lock = threading.Lock()
        self._flights: dict[tuple, asyncio.Lock] = {}
        # Swappable in tests (e.g. an httpx.MockTransport standing in for PM).
        self.transport: Optional[httpx.AsyncBaseTransport] = None

    # ── Public ───────────────────────────────────────────────────────────────

    async def get_policy(self, audience: str, data_controller: str) -> Optional[PmPolicy]:
        """The partner's active scope policy for ``data_controller``. None when
        PM has none (404). Raises PolicyUnavailable."""
        if not data_controller:
            return None
        policies = await self._get((pm_partner_id(audience), _CONTROLLER, data_controller))
        return next((p for p in policies if p.data_controller == data_controller), None)

    async def list_policies(self, audience: str) -> list:
        """All active scope policies of the partner ([] when PM has none).
        Raises PolicyUnavailable."""
        return [
            p for p in await self._get((pm_partner_id(audience), _ALL, ""))
            if p.data_controller
        ]

    def invalidate(self, audience: Optional[str] = None) -> None:
        """Drop cached answers (all, or one partner's)."""
        with self._lock:
            if audience is None:
                self._cache.clear()
            else:
                pid = pm_partner_id(audience)
                for key in [k for k in self._cache if k[0] == pid]:
                    self._cache.pop(key, None)

    # ── Cache ────────────────────────────────────────────────────────────────

    async def _get(self, key: tuple) -> list:
        entry = self._cached(key)
        now = self._clock()
        if entry is not None and not self._needs_fetch(entry, now):
            return self._serve(key, entry, now)
        async with self._flight(key):
            entry = self._cached(key)
            now = self._clock()
            if entry is not None and not self._needs_fetch(entry, now):
                return self._serve(key, entry, now)
            return await self._fetch(key, entry, now)

    def _cached(self, key) -> Optional[_Entry]:
        with self._lock:
            return self._cache.get(key)

    def _store(self, key, entry: _Entry) -> None:
        with self._lock:
            self._cache[key] = entry

    def _flight(self, key) -> asyncio.Lock:
        with self._lock:
            lock = self._flights.get(key)
            if lock is None:
                lock = self._flights[key] = asyncio.Lock()
            return lock

    def _needs_fetch(self, entry: _Entry, now: float) -> bool:
        if entry.last_attempt > entry.fetched_at:
            # The last attempt failed: retry only after the negative TTL.
            return (now - entry.last_attempt) >= _config.partner_policy_negative_ttl_seconds
        return (now - entry.fetched_at) >= entry.ttl

    def _serve(self, key, entry: _Entry, now: float) -> list:
        """A cached answer: fresh, or a stale one within the hard TTL (PM down)."""
        if entry.value is not None and (
            entry.last_attempt <= entry.fetched_at
            or (now - entry.fetched_at) < _config.partner_policy_hard_ttl_seconds
        ):
            return entry.value
        raise PolicyUnavailable(f"Partner Management policy for {key[0]} is unavailable")

    async def _fetch(self, key: tuple, prev: Optional[_Entry], now: float) -> list:
        base = (_config.partner_mgmt_api_url or "").rstrip("/")
        pid, kind, value = key
        if not base:
            return self._on_error(key, prev, now, "partner_mgmt_api_url is not configured")
        url = f"{base}/policies/{quote(pid, safe='')}"
        params = None if kind == _ALL else {kind: value}
        try:
            async with httpx.AsyncClient(
                timeout=_config.partner_policy_fetch_timeout_seconds,
                transport=self.transport,
            ) as client:
                resp = await client.get(url, params=params)
        except Exception as exc:
            return self._on_error(key, prev, now, f"request failed: {exc!r}")

        if resp.status_code == 404:
            self._store(
                key, _Entry([], now, now, _config.partner_policy_negative_ttl_seconds)
            )
            return []
        if resp.status_code != 200:
            return self._on_error(key, prev, now, f"HTTP {resp.status_code}")
        try:
            policies = [PmPolicy.from_pm(p) for p in (resp.json() or {}).get("policies") or []]
        except Exception as exc:
            return self._on_error(key, prev, now, f"bad response body: {exc!r}")

        self._store(key, _Entry(policies, now, now, self._ttl(resp)))
        return policies

    def _on_error(self, key, prev: Optional[_Entry], now: float, why: str) -> list:
        if prev is not None and prev.value is not None and (
            now - prev.fetched_at
        ) < _config.partner_policy_hard_ttl_seconds:
            _logger.warning(
                "PM policy fetch for %s failed (%s); serving the last answer "
                "(age %.0fs, hard TTL %ss)",
                key, why, now - prev.fetched_at, _config.partner_policy_hard_ttl_seconds,
            )
            self._store(key, _Entry(prev.value, prev.fetched_at, now, prev.ttl))
            return prev.value
        _logger.error("PM policy fetch for %s failed (%s); failing closed", key, why)
        self._store(key, _Entry(None, -1.0, now, 0))
        raise PolicyUnavailable(f"Partner Management policy for {key[0]} is unavailable")

    @staticmethod
    def _ttl(resp: httpx.Response) -> float:
        ttl = _config.partner_policy_cache_ttl_seconds
        match = _MAX_AGE.search(resp.headers.get("Cache-Control") or "")
        if match:
            ttl = min(ttl, int(match.group(1)))
        return max(ttl, 0)
