"""Consent templates, read from a template source (exchange role).

At the Exchange a consent made through the partner portal or the lifecycle API
is made from a consent template offered to the partner. A template names a
purpose, the scopes it needs per registry (data controller) and limits; the
Consent Manager records the purpose and the grants, with the template's code
as a label. The source (at the Agri Stack Exchange, the Composite's internal
endpoint) lists only the templates offered to that partner:

    GET {consent_template_source_url}?partner=<audience>
    GET {consent_template_source_url}/{code}?partner=<audience>

    {"templates": [{"code", "title", "description", "purpose",
                    "scopes": {controller: {"required": [...], "optional": [...]}},
                    "limits": {"allowed_subject_id_types", "max_validity_duration",
                               "allow_partner_consents"}}]}

(one template for ``/{code}``: the object itself, or ``{"templates": [it]}``).
404 = none offered. Answers are reused per (partner, code | list) for
min(``consent_template_cache_ttl_seconds``, the source's ``Cache-Control:
max-age``). When the source cannot be reached nothing stale is served: the
caller fails closed (``TemplateSourceUnavailable``).
"""
import logging
import re
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
TEMPLATE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")


def consent_by_template() -> bool:
    """Exchange role (this CM issues consent tokens to a presenter): consents
    made here through the portal or the lifecycle API are made from a consent
    template."""
    return bool(_config.receipt_issuer and _config.receipt_presenters)


EXCHANGE_NEEDS_TEMPLATE = (
    "at the Exchange, consents are made from a consent template: send 'template' "
    "(its code), and optionally 'grants' chosen within its scopes"
)


class TemplateSourceUnavailable(Exception):
    """The template source is not configured, cannot be reached or answered
    wrongly."""


@dataclass(frozen=True)
class TemplateLimits:
    """A template's limits on a consent made from it. Empty = no limit."""

    allowed_subject_id_types: list = field(default_factory=list)
    max_validity_duration: Optional[str] = None
    allow_partner_consents: bool = False

    @classmethod
    def from_source(cls, data) -> "TemplateLimits":
        data = data if isinstance(data, dict) else {}
        return cls(
            allowed_subject_id_types=[
                str(t) for t in data.get("allowed_subject_id_types") or [] if t
            ],
            max_validity_duration=data.get("max_validity_duration") or None,
            allow_partner_consents=data.get("allow_partner_consents") is True,
        )


@dataclass(frozen=True)
class ConsentTemplate:
    """A consent template, as far as a consent is concerned."""

    code: str
    title: Optional[str]
    description: Optional[str]
    purpose: Optional[str]
    # [{"data_controller", "data_scopes", "required_scopes", "optional_scopes"}]
    # for every registry with scopes (data_scopes = required ∪ optional).
    grants: list = field(default_factory=list)
    # Registries the template names without any scope: a consent cannot be
    # made from it.
    unscoped_controllers: list = field(default_factory=list)
    limits: TemplateLimits = field(default_factory=TemplateLimits)

    @classmethod
    def from_source(cls, data: dict) -> "ConsentTemplate":
        scopes = data.get("scopes") or {}
        grants, unscoped = [], []
        for controller in sorted(scopes):
            entry = scopes[controller] or {}
            required = sorted({str(s) for s in entry.get("required") or []})
            optional = sorted({str(s) for s in entry.get("optional") or []} - set(required))
            if not (required or optional):
                unscoped.append(str(controller))
                continue
            grants.append(
                {
                    "data_controller": str(controller),
                    "data_scopes": sorted(set(required) | set(optional)),
                    "required_scopes": required,
                    "optional_scopes": optional,
                }
            )
        return cls(
            code=str(data["code"]),
            title=data.get("title") or None,
            description=data.get("description") or None,
            purpose=data.get("purpose") or None,
            grants=grants,
            unscoped_controllers=unscoped,
            limits=TemplateLimits.from_source(data.get("limits")),
        )


class ConsentTemplateClient(BaseService):
    """Cached reader of the consent templates offered to a partner."""

    def __init__(self, name="", clock=time.monotonic, **kwargs):
        super().__init__(name, **kwargs)
        self._clock = clock
        # (partner, code or "") -> (fetched at, ttl, answer)
        self._cache: dict[tuple, tuple[float, float, object]] = {}
        # Swappable in tests (an httpx.MockTransport standing in for the source).
        self.transport: Optional[httpx.AsyncBaseTransport] = None

    async def list_templates(self, partner: str) -> list:
        """The templates offered to ``partner`` (its audience); [] when none.
        Raises TemplateSourceUnavailable."""
        return list(await self._get(partner, ""))

    async def get_template(self, partner: str, code: str) -> Optional[ConsentTemplate]:
        """Template ``code`` when it is offered to ``partner``, else None.
        Raises TemplateSourceUnavailable."""
        if not code:
            return None
        return await self._get(partner, code)

    def invalidate(self) -> None:
        self._cache.clear()

    async def _get(self, partner: str, code: str):
        key = (partner, code)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < cached[1]:
            return cached[2]
        base = (_config.consent_template_source_url or "").rstrip("/")
        if not base:
            raise TemplateSourceUnavailable("consent_template_source_url is not configured")
        url = f"{base}/{quote(code, safe='@')}" if code else base
        try:
            async with httpx.AsyncClient(
                timeout=_config.consent_template_fetch_timeout_seconds,
                transport=self.transport,
            ) as client:
                resp = await client.get(url, params={"partner": partner})
        except Exception as exc:
            _logger.error("Consent template source %s: request failed: %r", url, exc)
            raise TemplateSourceUnavailable(f"request failed: {exc!r}") from exc
        if resp.status_code == 404:
            answer = None if code else []
        elif resp.status_code != 200:
            _logger.error("Consent template source %s: HTTP %s", url, resp.status_code)
            raise TemplateSourceUnavailable(f"HTTP {resp.status_code}")
        else:
            try:
                answer = self._parse(resp.json(), code)
            except Exception as exc:
                _logger.error("Consent template source %s: bad response body: %r", url, exc)
                raise TemplateSourceUnavailable(f"bad response body: {exc!r}") from exc
        self._cache[key] = (now, self._ttl(resp), answer)
        return answer

    @staticmethod
    def _parse(body, code: str):
        items = body.get("templates") if isinstance(body, dict) and "templates" in body else None
        if code:
            if items is None:
                items = [body]
            # Another code served (e.g. another major version): not this template.
            found = [ConsentTemplate.from_source(t) for t in items or []]
            return next((t for t in found if t.code == code), None)
        return [ConsentTemplate.from_source(t) for t in items or []]

    @staticmethod
    def _ttl(resp: httpx.Response) -> float:
        ttl = _config.consent_template_cache_ttl_seconds
        match = _MAX_AGE.search(resp.headers.get("Cache-Control") or "")
        if match:
            ttl = min(ttl, int(match.group(1)))
        return max(ttl, 0)
