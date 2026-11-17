"""Stable, opaque per-delivery Idempotency-Keys.

Preferred: generate a random UUID4 when a delivery is first recorded in the caller's
outbox and store it with that row, so every retry and restart reuses it. A stored UUID4
is also the only safe choice when the caller's own ids can be reused (for example a
Frappe naming series that restarts when a site is recreated).

When there is no outbox, ``delivery_key`` derives a deterministic UUID5 from the
originating service, its source event ID, the delivery role and the channel. It is the
same for every retry of the same intended delivery, and different for every other
delivery.

Algorithm (``KEY_ALGORITHM = "oan-delivery-key/v2"``)::

    material = json.dumps(["oan-delivery-key/v2", service, event_id, role, channel],
                          separators=(",", ":"), ensure_ascii=True)
    key      = str(uuid.uuid5(OAN_KEY_NAMESPACE, material))

A JSON array keeps every element's boundaries, so no two different tuples share the same
material, even when an element contains ``:`` (allowed in inputs). The version tag is
part of the material, so a future algorithm can never reproduce a v2 key by accident.

Persisted keys: a key is fixed once it has been stored or sent. Never recompute and
overwrite a stored key with a newer algorithm: OAN would see a different key for the same
delivery and could send it twice. The earlier MSG-06 draft (``service:event:role:channel``
joined with ``:``) was never released or deployed, so no stored keys are known; any that
exist must be reused exactly as stored until their delivery is finished.

Never put OTPs, phone numbers, chat IDs or other personal data into the inputs:
UUID5 is a hash, and a hash of a short value such as a phone number can be reversed by
brute force.
"""

from __future__ import annotations

import json
import re
import uuid

#: Fixed namespace for OAN delivery keys (randomly generated once; never change it).
OAN_KEY_NAMESPACE = uuid.UUID("5b0f6c1e-6f2a-4d4e-9c63-2b7a1f0d8e41")
#: Version tag included in the hashed material. Changing it changes every key.
KEY_ALGORITHM = "oan-delivery-key/v2"
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
#: An isolated Ethiopian mobile number in any accepted form (+2519…, 2519…, 09…/07…).
_PHONE_LIKE = re.compile(r"(?<!\d)(?:\+?251|0)[79]\d{8}(?!\d)")


def key_material(service: str, event_id: str, role: str, channel: str) -> str:
    """The exact, unambiguous string that ``delivery_key`` hashes (for audits/tests)."""
    for part in (service, event_id, role, channel):
        if not isinstance(part, str) or not _TOKEN.fullmatch(part):
            raise ValueError("key inputs must be short opaque tokens")
        if _PHONE_LIKE.search(part):
            raise ValueError("key inputs must not contain phone numbers")
    return json.dumps([KEY_ALGORITHM, service, event_id, role, channel],
                      separators=(",", ":"), ensure_ascii=True)


def delivery_key(service: str, event_id: str, role: str, channel: str) -> str:
    """UUID5 over the JSON array ``[KEY_ALGORITHM, service, event_id, role, channel]``.
    Refuses inputs that contain an Ethiopian mobile number as a guard against PII in
    keys."""
    return str(uuid.uuid5(OAN_KEY_NAMESPACE, key_material(service, event_id, role, channel)))
