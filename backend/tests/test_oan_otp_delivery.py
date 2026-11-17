"""OTP delivery through the OAN Messaging Service (services/otp_delivery.py).

The OAN API is an in-process httpx MockTransport shaped like contract 3.0.0. No network,
no database: the request object is a stand-in with the fields the providers set.

    pip install pytest pytest-asyncio && pytest backend/tests -q
"""

import json
import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from openg2p_consent_manager.services import otp_delivery, otp_provider

PHONE = "0900000601"


class FakeOan:
    def __init__(self, code=202):
        self.code, self.requests = code, []

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


@pytest.fixture
def cfg(monkeypatch):
    c = otp_delivery._config
    monkeypatch.setattr(c, "oan_delivery_enabled", True, raising=False)
    monkeypatch.setattr(c, "oan_base_url", "https://oan-messaging.example.invalid", raising=False)
    monkeypatch.setattr(c, "oan_token", "oanmsg_" + "t" * 43, raising=False)
    monkeypatch.setattr(c, "oan_max_attempts", 1, raising=False)
    monkeypatch.setattr(c, "oan_otp_phone_map", json.dumps({"FIN-1001": PHONE}), raising=False)

    async def name(_pid):
        return "Awash Bank"

    monkeypatch.setattr(otp_delivery, "_partner_name", name)
    fake = FakeOan()
    monkeypatch.setattr(otp_delivery, "_transport", httpx.MockTransport(fake))
    return fake


@pytest.mark.asyncio
async def test_consent_otp_is_sent_as_otp_with_its_own_deadline(cfg):
    req = request()
    mid = await otp_delivery.deliver_otp(req, "482913")
    assert mid and req.otp_channel is None  # the provider's configured channel is left alone
    body = json.loads(cfg.requests[0].content)
    assert body["purpose"] == "otp" and body["recipient"] == PHONE
    assert "482913" in body["text"] and "Awash Bank" in body["text"]
    sent = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert abs(sent - req.otp_expires_at) < timedelta(seconds=1)
    assert "482913" not in body["source_reference"]


@pytest.mark.asyncio
async def test_reissue_is_a_new_key(cfg):
    await otp_delivery.deliver_otp(request(), "111111")
    await otp_delivery.deliver_otp(
        request(otp_expires_at=datetime.now(timezone.utc) + timedelta(seconds=301)), "222222"
    )
    keys = [r.headers["Idempotency-Key"] for r in cfg.requests]
    assert len(set(keys)) == 2


@pytest.mark.asyncio
async def test_unknown_subject_phone_fails_visibly(cfg):
    with pytest.raises(otp_provider.OtpError) as err:
        await otp_delivery.deliver_otp(request(subject_id_value="FIN-404"), "482913")
    assert err.value.status == 422 and not cfg.requests


@pytest.mark.asyncio
async def test_oan_unavailable_fails_visibly_without_leaking_the_code(cfg, caplog):
    cfg.code = 503
    with pytest.raises(otp_provider.OtpError) as err:
        await otp_delivery.deliver_otp(request(), "482913")
    assert err.value.status == 503
    assert "482913" not in caplog.text


@pytest.mark.asyncio
async def test_disabled_delivery_sends_nothing(cfg, monkeypatch):
    monkeypatch.setattr(otp_delivery._config, "oan_delivery_enabled", False, raising=False)
    assert await otp_delivery.deliver_otp(request(), "482913") is None
    assert not cfg.requests


def test_full_text_names_the_partner_when_it_fits():
    text = otp_delivery.render_otp_text("123456", "Awash Bank", 5)
    assert "Awash Bank" in text and otp_delivery.fits_one_sms(text)


@pytest.mark.parametrize("partner", ["ሙከራ ባንክ", "Awash ባንክ", "Bank “Abay”", "Bank \u2013 Oromia"])
def test_non_gsm_partner_name_falls_back_to_a_text_that_fits(partner):
    text = otp_delivery.render_otp_text("123456", partner, 30)
    assert otp_delivery.fits_one_sms(text)
    assert partner not in text and "123456" in text


def test_longest_ascii_partner_still_fits():
    partner = "W" * otp_delivery.PARTNER_MAX
    text = otp_delivery.render_otp_text("12345678", partner, 30)
    assert partner in text and otp_delivery.fits_one_sms(text)


@pytest.mark.asyncio
async def test_amharic_partner_otp_is_sent_and_fits(cfg, monkeypatch):
    async def amharic(_pid):
        return "ሙከራ ባንክ"

    monkeypatch.setattr(otp_delivery, "_partner_name", amharic)
    assert await otp_delivery.deliver_otp(request(), "123456")
    body = json.loads(cfg.requests[0].content)
    assert otp_delivery.fits_one_sms(body["text"]) and "123456" in body["text"]


@pytest.mark.asyncio
async def test_oan_refusal_is_a_visible_502(cfg):
    cfg.code = 422
    with pytest.raises(otp_provider.OtpError) as err:
        await otp_delivery.deliver_otp(request(), "482913")
    assert (err.value.status, err.value.reason) == (502, "otp_delivery_rejected")
    assert len(cfg.requests) == 1  # a refusal is never retried


@pytest.mark.asyncio
async def test_non_https_base_url_is_a_configuration_error(cfg, monkeypatch):
    monkeypatch.setattr(otp_delivery._config, "oan_base_url", "http://oan.example.invalid")
    with pytest.raises(otp_provider.OtpError) as err:
        await otp_delivery.deliver_otp(request(), "482913")
    assert (err.value.status, err.value.reason) == (500, "otp_delivery_misconfigured")
    assert not cfg.requests
