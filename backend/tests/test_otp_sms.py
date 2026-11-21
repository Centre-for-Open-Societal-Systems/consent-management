"""OTP consent codes delivered by SMS through the OAN Messaging Service.

The real partner portal API and database (see conftest); the OAN Messaging API is an
in-process fake (httpx MockTransport) shaped like contract 3.0.0. Test numbers only.
"""

import json

import httpx
import pytest
from test_assisted_consent import (  # noqa: F401 - fixtures
    as_partner,
    bank,
    portal,
    realm_key,
    token,
    upload,
)
from test_otp_consent import (  # noqa: F401 - fixtures
    OTP,
    backdate_last_send,
    codes,
    otp_body,
    otp_config,
    raise_request,
)

SUBJECT = "123456789012"  # the subject of request_body()
PHONE = "0900000611"


class FakeOan:
    def __init__(self):
        self.code, self.requests = 202, []

    def __call__(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        if self.code == 202:
            return httpx.Response(
                202,
                json={
                    "id": "0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10",
                    "idempotency_key": request.headers["Idempotency-Key"],
                    "channel": "sms",
                    "status": "accepted",
                    "source_reference": body["source_reference"],
                    "created_at": "2026-09-01T05:12:13Z",
                    "expires_at": body["expires_at"],
                },
            )
        return httpx.Response(
            self.code,
            json={"title": "x", "status": self.code, "errors": [], "code": "service_unavailable"},
            headers={"content-type": "application/problem+json"},
        )


@pytest.fixture
def oan(monkeypatch, otp_config):  # noqa: F811 - the fixture imported above
    """OAN delivery on, the log harness off: the SMS is the only way the code travels."""
    from openg2p_consent_manager.services import otp_delivery

    monkeypatch.setattr(otp_config, "otp_log_codes", False)
    monkeypatch.setattr(otp_config, "oan_delivery_enabled", True)
    monkeypatch.setattr(otp_config, "oan_base_url", "https://oan-messaging.example.invalid")
    monkeypatch.setattr(otp_config, "oan_token", "oanmsg_" + "t" * 43)
    monkeypatch.setattr(otp_config, "oan_max_attempts", 1)
    monkeypatch.setattr(otp_config, "oan_otp_phone_map", json.dumps({SUBJECT: PHONE}))
    fake = FakeOan()
    monkeypatch.setattr(otp_delivery, "_transport", httpx.MockTransport(fake))
    return fake


def sms_bodies(fake):
    return [json.loads(r.content) for r in fake.requests]


def test_raise_sends_the_code_by_sms(portal, as_partner, bank, codes, oan):  # noqa: F811
    req = raise_request(as_partner(bank.audience))
    (body,) = sms_bodies(oan)
    assert body["recipient"] == PHONE and body["purpose"] == "otp"
    assert codes[-1] in body["text"]
    assert body["source_reference"] == f"dpi-consent:{req['id']}:otp-consent"


def test_the_sms_code_grants_the_consent(portal, as_partner, bank, codes, oan):  # noqa: F811
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    code = next(w for w in sms_bodies(oan)[0]["text"].split() if w.isdigit() and len(w) == 6)
    status, consent = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": code})
    assert status == 201, consent
    assert consent["status"] == "active" and consent["consent_request_id"] == req["id"]


def test_resend_is_a_new_sms_with_a_new_key(portal, as_partner, bank, codes, oan, run):  # noqa: F811
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    backdate_last_send(run, req["id"])
    status, body = pc("POST", f"{OTP}/{req['id']}/resend-otp")
    assert status == 200, body
    first, second = oan.requests
    assert first.headers["Idempotency-Key"] != second.headers["Idempotency-Key"]
    assert codes[-1] in json.loads(second.content)["text"]


def test_unreachable_oan_refuses_the_request(portal, as_partner, bank, codes, oan):  # noqa: F811
    oan.code = 503
    status, body = as_partner(bank.audience)("POST", OTP, json=otp_body())
    assert status == 503 and body["error"] == "otp_delivery_unavailable"


def test_unknown_subject_phone_is_a_visible_422(
    portal,  # noqa: F811
    as_partner,  # noqa: F811
    bank,  # noqa: F811
    codes,  # noqa: F811
    oan,
    otp_config,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(otp_config, "oan_otp_phone_map", "{}")
    status, body = as_partner(bank.audience)("POST", OTP, json=otp_body())
    assert status == 422 and body["error"] == "otp_destination_unknown"
    assert oan.requests == []


def test_delivery_switched_on_without_a_token_issues_nothing(
    portal,  # noqa: F811
    as_partner,  # noqa: F811
    bank,  # noqa: F811
    codes,  # noqa: F811
    oan,
    otp_config,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(otp_config, "oan_token", "")
    status, body = as_partner(bank.audience)("POST", OTP, json=otp_body())
    assert status == 503 and body["error"] == "otp_unavailable"
    assert codes == [] and oan.requests == []


def test_an_uncertain_resend_still_counts(portal, as_partner, bank, codes, oan, run):  # noqa: F811
    """OAN may still send a code it did not confirm, so the cooldown must hold."""
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    backdate_last_send(run, req["id"])
    oan.code = 503
    status, body = pc("POST", f"{OTP}/{req['id']}/resend-otp")
    assert status == 503 and body["error"] == "otp_delivery_unavailable"
    oan.code = 202
    status, body = pc("POST", f"{OTP}/{req['id']}/resend-otp")
    assert status == 429 and body["error"] == "resend_too_soon"
    assert len(oan.requests) == 2  # the first code and the uncertain resend; nothing more
