"""OTP consent on the partner portal API: ``method: "otp"`` on
POST /consent-requests sends the subject a code at once; the partner's
POST /consent-requests/{id}/verify-otp with that code grants the consent (no
evidence, no staff step). The consent is then presented at /validate by its ID.
Requests without ``method`` stay assisted (see test_assisted_consent).
"""
from datetime import datetime, timedelta, timezone

import pytest
from test_assisted_consent import (  # noqa: F401 - fixtures
    as_partner,
    bank,
    portal,
    realm_key,
    request_body,
    token,
    upload,
)
from test_grants import CSR, FR

OTP = "/consent-requests"
PORTAL = "/consent/v1/partner-portal"


@pytest.fixture
def codes(monkeypatch):
    """The codes the CM generated, newest last (no SMS gateway to read them from)."""
    from openg2p_consent_manager.utils import fayda_otp

    sent = []
    real = fayda_otp.generate_otp

    def capture(length=6):
        code = real(length)
        sent.append(code)
        return code

    monkeypatch.setattr(fayda_otp, "generate_otp", capture)
    return sent


def otp_body(**extra):
    return request_body(method="otp", **extra)


def raise_request(pc, **extra):
    status, body = pc("POST", OTP, json=otp_body(**extra))
    assert status == 201, body
    return body


def wrong(code):
    return "000000" if code != "000000" else "111111"


def test_raise_sends_the_code(portal, as_partner, bank, codes):
    pc = as_partner(bank.audience, username="api-client")
    req = raise_request(pc)
    assert req["status"] == "pending" and req["method"] == "otp"
    assert req["created_by"] == "api-client" and req["consent_id"] is None
    assert req["otp_channel"] == "PHONE" and req["otp_expires_at"]
    assert req["otp_attempts_left"] == 3
    assert len(codes) == 1 and len(codes[0]) == 6
    assert "otp" not in req  # the code itself is never returned

    status, again = pc("GET", f"{OTP}/{req['id']}")
    assert status == 200 and again["id"] == req["id"]


def test_raise_uses_the_portal_policy_checks(portal, as_partner, bank, codes):
    pc = as_partner(bank.audience)
    status, body = pc("POST", OTP, json=otp_body(purpose={"code": "marketing"}))
    assert status == 422 and "purpose_not_allowed" in body["error"]
    assert codes == []  # nothing sent for a refused request


def test_verify_grants_the_consent(call, portal, as_partner, bank, codes):
    pc = as_partner(bank.audience, username="api-client")
    req = raise_request(pc)
    status, consent = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})
    assert status == 201, consent
    assert consent["status"] == "active" and consent["consent_id"] and consent["receipt_id"]
    assert consent["consent_request_id"] == req["id"]
    assert {g["data_controller"]: g["effective_data_scopes"] for g in consent["grants"]} == {
        FR: ["farmer_personal_details"], CSR: ["crop_season", "measures"]}
    assurance = consent["assurance"]
    assert assurance["method"] == "otp" and assurance["subject_authenticated"] is True
    assert assurance["subject_authentication"]["method"] == "otp"
    assert assurance["subject_authentication"]["submitted_by"] == "api-client"

    status, req_now = pc("GET", f"{OTP}/{req['id']}")
    assert req_now["status"] == "approved" and req_now["consent_id"] == consent["consent_id"]
    # The consent is the partner's like any other: listed in the portal, receipt.
    status, listing = pc("GET", "/consents")
    assert consent["consent_id"] in [c["consent_id"] for c in listing["items"]]
    assert pc("GET", f"/consents/{consent['consent_id']}/receipt")[0] == 200

    # And presented by its ID at /validate.
    status, decision = call("POST", "/consent/v1/validate", json={
        "consent_id": consent["consent_id"], "consent_partner_id": bank.audience,
        "request_context": {"subject_id": {"type": "FAYDA_FAN", "value": "123456789012"}}})
    assert status == 200 and decision["decision"] == "permit", decision
    assert decision["grants"] == {FR: ["farmer_personal_details"],
                                  CSR: ["crop_season", "measures"]}

    # The code is spent.
    status, body = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})
    assert status == 409 and "approved" in body["error"]


def test_wrong_code_counts_attempts(portal, as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    path = f"{OTP}/{req['id']}/verify-otp"
    for left in (2, 1, 0):
        status, body = pc("POST", path, json={"otp": wrong(codes[-1])})
        assert status == 401 and body["error"] == "otp_invalid"
        assert pc("GET", f"{OTP}/{req['id']}")[1]["otp_attempts_left"] == left
    status, body = pc("POST", path, json={"otp": codes[-1]})
    assert status == 429 and body["error"] == "too_many_attempts"

    # A new code resets the count and the old one stops working.
    old = codes[-1]
    status, again = pc("POST", f"{OTP}/{req['id']}/resend-otp")
    assert status == 200 and again["otp_attempts_left"] == 3 and len(codes) == 2
    if old != codes[-1]:
        assert pc("POST", path, json={"otp": old})[0] == 401
    assert pc("POST", path, json={"otp": codes[-1]})[0] == 201


def test_expired_code(portal, as_partner, bank, codes, run):
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentRequest

    pc = as_partner(bank.audience)
    req = raise_request(pc)

    async def expire():
        async with async_session()() as session:
            row = await session.get(ConsentRequest, req["id"])
            row.otp_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()

    run(expire())
    status, body = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})
    assert status == 410 and body["error"] == "otp_expired"


def test_other_partner_sees_nothing(portal, as_partner, bank, new_partner, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    other = as_partner(new_partner().audience)
    rid = req["id"]
    assert other("GET", f"{OTP}/{rid}")[0] == 404
    assert other("POST", f"{OTP}/{rid}/verify-otp", json={"otp": codes[-1]})[0] == 404
    assert other("POST", f"{OTP}/{rid}/resend-otp")[0] == 404
    # Still open for its own partner.
    assert pc("POST", f"{OTP}/{rid}/verify-otp", json={"otp": codes[-1]})[0] == 201


def test_otp_and_assisted_requests_share_the_portal_api(portal, as_partner, bank, codes,
                                                         upload):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    rid = req["id"]
    # Listed and readable like any portal request, with its method.
    status, listing = pc("GET", "/consent-requests")
    assert rid in [i["id"] for i in listing["items"]]
    assert pc("GET", f"/consent-requests/{rid}")[1]["method"] == "otp"
    # The assisted steps do not apply to it.
    status, body = upload(pc, rid)
    assert status == 409 and "verify-otp" in body["error"]
    status, body = pc("POST", f"/consent-requests/{rid}/submit")
    assert status == 409 and "verify-otp" in body["error"]
    # An assisted request has no code to verify, and still works as before.
    status, assisted = pc("POST", "/consent-requests", json=request_body())
    assert status == 201 and assisted["method"] == "assisted" and len(codes) == 1
    assert pc("POST", f"/consent-requests/{assisted['id']}/verify-otp",
              json={"otp": "123456"})[0] == 404
    assert pc("POST", f"/consent-requests/{assisted['id']}/resend-otp")[0] == 404
    # Cancel works for an OTP request too.
    assert pc("POST", f"/consent-requests/{rid}/cancel")[1]["status"] == "cancelled"
    assert pc("POST", f"/consent-requests/{rid}/verify-otp",
              json={"otp": codes[-1]})[0] == 409


def test_needs_a_partner_token(call, portal, realm_key, bank):
    assert call("POST", PORTAL + OTP, json=otp_body())[0] == 401
    headers = {"Authorization": f"Bearer {token(realm_key, None)}"}
    assert call("POST", PORTAL + OTP, json=otp_body(), headers=headers)[0] == 403


def test_bad_otp_body(portal, as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    status, _ = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": "1"})
    assert status in (400, 422)


def test_otp_published_to_bucket_when_enabled(portal, as_partner, bank, codes, monkeypatch):
    """Test harness: each issued OTP is written as an A2C-style JSON object."""
    moto = pytest.importorskip("moto")
    import json

    import boto3
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import OtpPublisher

    config = Settings.get_config()
    monkeypatch.setattr(config, "otp_publish_enabled", True)
    monkeypatch.setattr(config, "otp_publish_bucket", "a2c-webhook")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    publisher = OtpPublisher.get_component()
    with moto.mock_aws():
        monkeypatch.setattr(publisher, "_client", None)
        publisher.ensure_bucket()
        req = raise_request(as_partner(bank.audience))
        s3 = boto3.client("s3", region_name=config.otp_publish_region)
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket="a2c-webhook")["Contents"]]
        assert len(keys) == 1 and keys[0].startswith("otp/") and keys[0].endswith(".json")
        body = json.loads(s3.get_object(Bucket="a2c-webhook", Key=keys[0])["Body"].read())
        assert body["otp"] == codes[-1] and body["requestId"] == req["id"]
        assert body["individualId"] == "123456789012"
        monkeypatch.setattr(publisher, "_client", None)


def test_otp_not_published_by_default(portal, as_partner, bank, codes):
    from openg2p_consent_manager.services import OtpPublisher

    assert not OtpPublisher.get_component().enabled
    raise_request(as_partner(bank.audience))  # no bucket configured: still issued
    assert len(codes) == 1


def test_migration_added_otp_columns(run):
    from openg2p_consent_manager.db import async_session
    from sqlalchemy import text

    async def columns():
        async with async_session()() as session:
            rows = await session.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'consent_requests' AND column_name LIKE 'otp_%'"))
            return {r[0] for r in rows}

    assert run(columns()) == {
        "otp_hash", "otp_expires_at", "otp_attempts", "otp_verified_at", "otp_channel",
        "otp_destination", "otp_provider", "otp_reference",
    }
