"""The partner's copy of an assisted consent, for its records (disputes, audits).

One ZIP per consent, built on request; nothing in it changes the consent or its
receipt:

* ``consent-receipt.json``  the consent receipt, exactly as the CM issued it;
* ``evidence/…``            the files uploaded on the request (the signed form);
* ``consent-record.jws``    a CM-signed statement (same key and JWKS as the
                             receipts) that ties them together: the consent,
                             the receipt's and each file's SHA-256, the
                             subject's national-ID authentication and when the
                             CM verified the evidence;
* ``consent-record.json``   the same statement, readable;
* ``summary.html``          a printable summary;
* ``cm-jwks.json``, ``README.txt``  how to check it all without the CM.

The statement is signed when the record is downloaded (``iat``); the facts in
it are those recorded at approval.
"""
import hashlib
import html
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import select

from ..config import Settings
from ..db import async_session
from ..models import ConsentEvidence, ConsentReceipt, ConsentRequest
from .assisted_service import AssistedConsentService, artefact_status
from .crypto_service import CryptoService
from .evidence_service import EvidenceError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

RECORD_TYPE = "consent-record+jwt"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return str(value)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file").strip("._") or "file"
    return cleaned[:120]


class ConsentRecordService(BaseService):
    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self.assisted = AssistedConsentService.get_component()
        self.crypto = CryptoService.get_component()

    async def build(self, audience: str, consent_id: str) -> Optional[tuple[str, bytes]]:
        """(file name, ZIP bytes) for the partner's consent; None if not theirs."""
        artefact, use_cases = await self.assisted.get_consent(audience, consent_id)
        if artefact is None:
            return None
        async with async_session()() as session:
            receipt = (
                await session.execute(
                    select(ConsentReceipt).where(ConsentReceipt.consent_id == consent_id)
                )
            ).scalars().first()
            req = (
                await session.get(ConsentRequest, artefact.consent_request_id)
                if artefact.consent_request_id else None
            )
            evidence = list(
                (
                    await session.execute(
                        select(ConsentEvidence)
                        .where(ConsentEvidence.consent_request_id == artefact.consent_request_id)
                        .order_by(ConsentEvidence.uploaded_at)
                    )
                ).scalars().all()
            ) if artefact.consent_request_id else []

        files: list[tuple[str, bytes]] = []
        receipt_bytes = None
        if receipt is not None:
            receipt_bytes = json.dumps(receipt.document, indent=2, ensure_ascii=False).encode("utf-8")
            files.append(("consent-receipt.json", receipt_bytes))

        evidence_entries = []
        for i, ev in enumerate(evidence, start=1):
            data = await self.assisted.evidence_bytes(ev)
            digest = _sha256(data)
            if digest != ev.sha256:
                # The stored file no longer matches what was verified: say so
                # rather than hand out an unprovable copy.
                raise EvidenceError(500, f"evidence {ev.id} does not match its recorded SHA-256")
            path = f"evidence/{i:02d}-{_safe_name(ev.filename)}"
            files.append((path, data))
            evidence_entries.append({
                "file": path,
                "id": ev.id,
                "kind": ev.kind,
                "filename": ev.filename,
                "content_type": ev.content_type,
                "size_bytes": ev.size_bytes,
                "sha256": digest,
                "uploaded_at": _iso(ev.uploaded_at),
                "uploaded_by": ev.uploaded_by,
            })

        assurance = artefact.assurance or {}
        grants = artefact.grants or [{
            "data_controller": artefact.controller_id,
            "effective_data_scopes": artefact.effective_data_scopes or [],
        }]
        now = datetime.now(timezone.utc)
        statement = {
            "type": "consent-record",
            "version": 1,
            "iss": _config.receipt_issuer or "consent-manager",
            "iat": int(now.timestamp()),
            "consent_id": artefact.id,
            "consent_request_id": artefact.consent_request_id,
            "partner": audience,
            "subject_id": {"type": artefact.subject_id_type, "value": artefact.subject_id_value},
            "subject_identifiers": artefact.subject_identifiers or [],
            "purpose": artefact.purpose or {},
            "use_case": use_cases.get(artefact.consent_request_id),
            "grants": [
                {"data_controller": g.get("data_controller"),
                 "data_scopes": g.get("effective_data_scopes") or []}
                for g in grants
            ],
            "valid_from": _iso(artefact.valid_from),
            "valid_until": _iso(artefact.valid_until),
            "status": artefact_status(artefact, now),
            "receipt": {"file": "consent-receipt.json", "sha256": _sha256(receipt_bytes)}
            if receipt_bytes is not None else None,
            "evidence": evidence_entries,
            "subject_authenticated": bool(assurance.get("subject_authenticated")),
            "subject_authentication": assurance.get("subject_authentication"),
            # When the CM checked the evidence; the staff member is in the CM's
            # own audit trail, not in the partner's copy.
            "verified_by": "Consent Manager",
            "verified_at": _iso(req.verified_at) if req else assurance.get("verified_at"),
        }
        jws = self.crypto.sign_jws(statement, typ=RECORD_TYPE)
        files += [
            ("consent-record.jws", jws.encode("ascii")),
            ("consent-record.json", json.dumps(statement, indent=2, ensure_ascii=False).encode("utf-8")),
            ("cm-jwks.json", json.dumps(self.crypto.public_jwks(), indent=2).encode("utf-8")),
            ("summary.html", _summary(statement).encode("utf-8")),
            ("README.txt", _readme(statement).encode("utf-8")),
        ]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path, data in files:
                z.writestr(path, data)
        return f"consent-record-{artefact.id}.zip", buf.getvalue()


def _readme(s: dict) -> str:
    jwks = _config.consent_record_jwks_url or "the Consent Manager's /.well-known/jwks.json"
    lines = [
        f"Consent record — consent {s['consent_id']}",
        "",
        "consent-receipt.json  The consent receipt as issued by the Consent Manager (unchanged).",
        "evidence/             The documents uploaded on the request (the subject's signed form).",
        "consent-record.jws    The Consent Manager's signed statement tying them together.",
        "consent-record.json   The same statement, readable.",
        "summary.html          A printable summary.",
        "cm-jwks.json          The Consent Manager's public key(s) at the time of download.",
        "",
        "To check this record without the Consent Manager:",
        f"1. Verify the signature of consent-record.jws with the Consent Manager's public key: {jwks}",
        "   (cm-jwks.json is a copy; prefer the published one).",
        "2. Check that the SHA-256 of consent-receipt.json and of each file in evidence/ equals the",
        "   value in the statement (e.g. `sha256sum consent-receipt.json evidence/*`).",
        "",
        f"Statement signed at {datetime.fromtimestamp(s['iat'], timezone.utc).isoformat()}; "
        f"consent status then: {s['status']}.",
    ]
    return "\n".join(lines) + "\n"


def _summary(s: dict) -> str:
    e = html.escape

    def row(label, value):
        return f"<tr><th>{e(label)}</th><td>{value}</td></tr>"

    purpose = s.get("purpose") or {}
    auth = s.get("subject_authentication") or {}
    grants = "".join(
        f"<li><code>{e(str(g['data_controller']))}</code>: {e(', '.join(g['data_scopes']))}</li>"
        for g in s["grants"]
    )
    evidence = "".join(
        f"<li>{e(x['filename'])} ({e(x['kind'])}, {x['size_bytes']} bytes)<br>"
        f"<small>SHA-256 <code>{e(x['sha256'])}</code></small></li>"
        for x in s["evidence"]
    ) or "<li>none</li>"
    authenticated = (
        f"Yes — {e(str(auth.get('provider') or 'national ID'))}"
        f"{', ' + e(str(auth['method'])) if auth.get('method') else ''}"
        f"{', ' + e(str(auth['authenticated_at'])) if auth.get('authenticated_at') else ''}"
        if s["subject_authenticated"] else "No (signed form only)"
    )
    rows = "".join([
        row("Consent ID", f"<code>{e(s['consent_id'])}</code>"),
        row("Status", e(s["status"])),
        row("Partner", e(s["partner"])),
        row("Subject", f"{e(s['subject_id']['type'])} {e(s['subject_id']['value'])}"),
        *[row(i["type"], e(i["value"])) for i in s.get("subject_identifiers") or []],
        row("Purpose", e(str(purpose.get("code") or purpose))),
        row("Use case", e(s["use_case"] or "—")),
        row("Data", f"<ul>{grants}</ul>"),
        row("Valid", f"{e(s['valid_from'] or '—')} to {e(s['valid_until'] or '—')}"),
        row("Person authenticated", authenticated),
        row("Evidence", f"<ul>{evidence}</ul>"),
        row("Verified by the Consent Manager", e(s["verified_at"] or "—")),
        row("Receipt SHA-256", f"<code>{e(s['receipt']['sha256'])}</code>" if s.get("receipt") else "—"),
    ])
    signed = datetime.fromtimestamp(s["iat"], timezone.utc).isoformat()
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Consent record {e(s['consent_id'])}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}"
        "th{text-align:left;vertical-align:top;padding:.4rem 1rem .4rem 0;width:14rem}"
        "td{padding:.4rem 0}code{font-size:.85em;word-break:break-all}ul{margin:0;padding-left:1.2rem}"
        "</style></head><body>"
        "<h1>Consent record</h1>"
        f"<table>{rows}</table>"
        f"<p><small>Signed by the Consent Manager at {e(signed)} (consent-record.jws). "
        "See README.txt to check this record.</small></p>"
        "</body></html>"
    )
