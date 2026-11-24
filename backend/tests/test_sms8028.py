"""OTP delivery straight to 8028 (services/sms8028.py, wired in services/otp_delivery.py).

8028 is an in-process httpx MockTransport shaped like its Integration Guide v1.1: HTTP 201 with
``{"success": true, "data": [{"sms_id": ..., "phone_number": ...}]}``. No network, no database.

    pip install pytest pytest-asyncio && pytest backend/tests/test_sms8028.py -q
"""

import json
import logging
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from openg2p_consent_manager.services import otp_delivery, otp_provider, sms8028

PHONE = "0900000601"
SECRET = "s3cr3t-client-secret-value"
CODE = "482913"


class Fake8028:
    """Answers like 8028; ``plan`` is one outcome per call (the last repeats)."""

    def __init__(self, *plan):
        self.plan, self.requests = list(plan or ["ok"]), []

    def __call__(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        step = self.plan[min(len(self.requests) - 1, len(self.plan) - 1)]
        if step == "ok":
            return httpx.Response(
                201,
                json={
                    "success": True,
                    "message": "SMS records queued successfully.",
                    "data": [{"sms_id": "4433006", "phone_number": "251" + body["phonenums"][0][1:]}],
                },
            )
        if step == "ok-local-format":
            return httpx.Response(
                201,
                json={"success": True, "data": [{"sms_id": 77, "phone_number": body["phonenums"][0]}]},
            )
        if step == "refused":
            raise httpx.ConnectError("connection refused")
        if step == "timeout":
            raise httpx.ReadTimeout("read timed out")
        if isinstance(step, int):
            return httpx.Response(step, json={"success": False, "message": f"bad {PHONE} {CODE}"})
        if step == "malformed":
            return httpx.Response(201, json={"success": True, "data": []})
        if step == "not-json":
            return httpx.Response(201, text="queued")
        raise AssertionError(step)


def request(**kw):
    base = dict(
        id="req-1",
        partner_id="partner-1",
        subject_id_value="FIN-1001",
        otp_channel=None,
        otp_expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def setup(monkeypatch, *plan):
    c = otp_delivery._config
    for name, value in dict(
        oan_delivery_enabled=False,
        oan_base_url="",
        oan_token="",
        sms8028_enabled=True,
        sms8028_base_url="https://8028.example.invalid:3400/sms-api/v1",
        sms8028_client_secret=SECRET,
        sms8028_client_cert_pem="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
        sms8028_client_key_pem="-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
        sms8028_max_attempts=2,
        oan_otp_phone_map=json.dumps({"FIN-1001": PHONE}),
    ).items():
        monkeypatch.setattr(c, name, value, raising=False)

    async def partner_name(_pid):
        return "Awash Bank"

    monkeypatch.setattr(otp_delivery, "_partner_name", partner_name)
    fake = Fake8028(*plan)
    monkeypatch.setattr(sms8028, "_transport", httpx.MockTransport(fake))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(sms8028.asyncio, "sleep", no_sleep)
    return fake


@pytest.mark.asyncio
async def test_the_code_is_posted_to_8028_with_the_contract_body(monkeypatch):
    fake = setup(monkeypatch)
    sms_id = await otp_delivery.deliver_otp(request(), CODE)
    assert sms_id == "4433006"
    sent = fake.requests[0]
    assert str(sent.url) == "https://8028.example.invalid:3400/sms-api/v1/sms/send-sms"
    body = json.loads(sent.content)
    assert body["clientSecret"] == SECRET
    assert body["phonenums"] == [PHONE]
    assert CODE in body["smstext"] and "Awash Bank" in body["smstext"]
    assert "pushdatetime" not in body  # omitted: 8028 sends now


@pytest.mark.asyncio
async def test_a_number_returned_in_local_form_is_accepted(monkeypatch):
    setup(monkeypatch, "ok-local-format")
    assert await otp_delivery.deliver_otp(request(), CODE) == "77"


@pytest.mark.asyncio
async def test_nothing_sensitive_is_logged(monkeypatch, caplog):
    setup(monkeypatch, 400)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(otp_provider.OtpError):
        await otp_delivery.deliver_otp(request(), CODE)
    logged = caplog.text
    assert SECRET not in logged and CODE not in logged and PHONE not in logged
    assert "http_400" in logged


@pytest.mark.asyncio
async def test_a_refusal_is_a_502_and_is_not_repeated(monkeypatch):
    fake = setup(monkeypatch, 400)
    with pytest.raises(otp_provider.OtpError) as exc:
        await otp_delivery.deliver_otp(request(), CODE)
    assert (exc.value.status, exc.value.reason) == (502, "otp_delivery_rejected")
    assert len(fake.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429])
async def test_a_credential_or_throttle_problem_is_unavailable_and_not_repeated(monkeypatch, status):
    fake = setup(monkeypatch, status)
    with pytest.raises(otp_provider.OtpError) as exc:
        await otp_delivery.deliver_otp(request(), CODE)
    assert (exc.value.status, exc.value.reason) == (503, "otp_delivery_unavailable")
    assert len(fake.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["timeout", 500, 503, "malformed", "not-json"])
async def test_an_uncertain_outcome_is_never_repeated(monkeypatch, step):
    fake = setup(monkeypatch, step)
    with pytest.raises(otp_provider.OtpError) as exc:
        await otp_delivery.deliver_otp(request(), CODE)
    assert (exc.value.status, exc.value.reason) == (503, "otp_delivery_unavailable")
    assert len(fake.requests) == 1, "8028 has no idempotency: a written request is never resent"


@pytest.mark.asyncio
async def test_a_refused_connection_is_retried_then_unavailable(monkeypatch):
    fake = setup(monkeypatch, "refused")
    with pytest.raises(otp_provider.OtpError) as exc:
        await otp_delivery.deliver_otp(request(), CODE)
    assert exc.value.status == 503
    assert len(fake.requests) == 2


@pytest.mark.asyncio
async def test_a_refused_connection_that_recovers_is_sent_once(monkeypatch):
    fake = setup(monkeypatch, "refused", "ok")
    assert await otp_delivery.deliver_otp(request(), CODE) == "4433006"
    assert len(fake.requests) == 2


@pytest.mark.asyncio
async def test_no_phone_is_a_422_and_nothing_is_sent(monkeypatch):
    fake = setup(monkeypatch)
    with pytest.raises(otp_provider.OtpError) as exc:
        await otp_delivery.deliver_otp(request(subject_id_value="FIN-404"), CODE)
    assert exc.value.status == 422
    assert not fake.requests


@pytest.mark.asyncio
async def test_two_transports_enabled_is_refused_and_nothing_is_sent(monkeypatch):
    fake = setup(monkeypatch)
    monkeypatch.setattr(otp_delivery._config, "oan_delivery_enabled", True, raising=False)
    with pytest.raises(otp_provider.OtpError) as exc:
        await otp_delivery.deliver_otp(request(), CODE)
    assert (exc.value.status, exc.value.reason) == (500, "otp_delivery_misconfigured")
    assert not fake.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["sms8028_client_secret", "sms8028_client_cert_pem", "sms8028_client_key_pem"])
async def test_missing_credentials_are_a_configuration_error(monkeypatch, missing):
    fake = setup(monkeypatch)
    monkeypatch.setattr(otp_delivery._config, missing, "", raising=False)
    with pytest.raises(otp_provider.OtpError) as exc:
        await otp_delivery.deliver_otp(request(), CODE)
    assert (exc.value.status, exc.value.reason) == (500, "otp_delivery_misconfigured")
    assert not fake.requests


@pytest.mark.asyncio
async def test_disabled_sends_nothing(monkeypatch):
    fake = setup(monkeypatch)
    monkeypatch.setattr(otp_delivery._config, "sms8028_enabled", False, raising=False)
    assert await otp_delivery.deliver_otp(request(), CODE) is None
    assert not fake.requests


def test_the_provider_reports_delivery_when_8028_is_ready(monkeypatch):
    setup(monkeypatch)
    assert otp_delivery._config.sms8028_ready and otp_delivery._config.sms_delivery_ready
    monkeypatch.setattr(otp_delivery._config, "sms8028_enabled", False, raising=False)
    assert not otp_delivery._config.sms_delivery_ready


def test_pem_accepts_text_escaped_newlines_and_base64():
    pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
    import base64

    assert sms8028._pem(pem) == pem
    assert sms8028._pem(pem.replace("\n", "\\n")) == pem
    assert sms8028._pem(base64.b64encode(pem.encode()).decode()) == pem
    assert sms8028._pem("not base64 !!") == ""
