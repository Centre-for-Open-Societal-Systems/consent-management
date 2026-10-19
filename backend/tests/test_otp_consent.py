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
from test_grants import CSR, CSR_SCOPES, FR

OTP = "/consent-requests"
PORTAL = "/consent/v1/partner-portal"


@pytest.fixture(autouse=True)
def otp_config(monkeypatch):
    """A configured salt and the log as the (test) delivery channel. The
    per-subject budget is lifted: every test here uses the same subject."""
    from openg2p_consent_manager.config import Settings

    config = Settings.get_config()
    monkeypatch.setattr(config, "otp_salt", "test-only-salt")
    monkeypatch.setattr(config, "otp_log_codes", True)
    monkeypatch.setattr(config, "otp_max_sends_per_subject", 10_000)
    return config


def backdate_last_send(run, request_id, seconds=3600):
    """As if the last code had been sent ``seconds`` ago (past the cooldown)."""
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentRequest

    async def go():
        async with async_session()() as session:
            row = await session.get(ConsentRequest, request_id)
            row.otp_last_sent_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
            await session.commit()

    run(go())


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


def test_wrong_code_counts_attempts(portal, as_partner, bank, codes, run):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    path = f"{OTP}/{req['id']}/verify-otp"
    for left in (2, 1, 0):
        status, body = pc("POST", path, json={"otp": wrong(codes[-1])})
        assert status == 401 and body["error"] == "otp_invalid"
        assert pc("GET", f"{OTP}/{req['id']}")[1]["otp_attempts_left"] == left
    status, body = pc("POST", path, json={"otp": codes[-1]})
    assert status == 429 and body["error"] == "too_many_attempts"

    # After the cooldown, a new code resets the count and the old one stops working.
    backdate_last_send(run, req["id"])
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
        "otp_send_count", "otp_last_sent_at", "otp_failures_total",
    }


# ── Review fixes: budgets a resend cannot reset ──────────────────────────────

def test_resend_cannot_immediately_undo_a_lockout(portal, as_partner, bank, codes, run):
    pc = as_partner(bank.audience)
    req = raise_request(pc)
    rid, path = req["id"], f"{OTP}/{req['id']}/verify-otp"
    for _ in range(3):
        assert pc("POST", path, json={"otp": wrong(codes[-1])})[0] == 401
    assert pc("POST", path, json={"otp": codes[-1]})[1]["error"] == "too_many_attempts"

    # Locked out: immediate resends are refused and change nothing.
    for _ in range(5):
        status, body = pc("POST", f"{OTP}/{rid}/resend-otp")
        assert status == 429 and body["error"] == "resend_too_soon"
    assert len(codes) == 1
    assert pc("GET", f"{OTP}/{rid}")[1]["otp_attempts_left"] == 0

    # After the cooldown one more code, but the request's total budget of
    # wrong codes (6) still counts the first three: three more close it.
    backdate_last_send(run, rid)
    assert pc("POST", f"{OTP}/{rid}/resend-otp")[0] == 200
    for _ in range(2):
        assert pc("POST", path, json={"otp": wrong(codes[-1])})[0] == 401
    status, body = pc("POST", path, json={"otp": wrong(codes[-1])})
    assert status == 429 and body["error"] == "otp_failure_limit_reached"
    assert pc("GET", f"{OTP}/{rid}")[1]["status"] == "cancelled"
    # Closed for good: the right code and further resends are refused.
    assert pc("POST", path, json={"otp": codes[-1]})[0] == 409
    assert pc("POST", f"{OTP}/{rid}/resend-otp")[0] == 409


def test_resend_limit_per_request(portal, as_partner, bank, codes, run):
    pc = as_partner(bank.audience)
    rid = raise_request(pc)["id"]
    for _ in range(2):  # the first code + 2 resends
        backdate_last_send(run, rid)
        assert pc("POST", f"{OTP}/{rid}/resend-otp")[0] == 200
    backdate_last_send(run, rid)
    status, body = pc("POST", f"{OTP}/{rid}/resend-otp")
    assert status == 429 and body["error"] == "resend_limit_reached"
    assert len(codes) == 3
    # The live code still works.
    assert pc("POST", f"{OTP}/{rid}/verify-otp", json={"otp": codes[-1]})[0] == 201


def test_codes_per_subject_are_limited(portal, as_partner, bank, codes, otp_config,
                                       monkeypatch, run):
    import uuid

    monkeypatch.setattr(otp_config, "otp_max_sends_per_subject", 3)
    subject = {"type": "FAYDA_FAN", "value": f"9{uuid.uuid4().int % 10**11:011d}"}
    pc = as_partner(bank.audience)
    first = raise_request(pc, subject_id=subject)
    raise_request(pc, subject_id=subject)
    backdate_last_send(run, first["id"], seconds=120)  # inside the window still
    assert pc("POST", f"{OTP}/{first['id']}/resend-otp")[0] == 200
    # Three codes sent to this subject within the hour, by this or any partner.
    status, body = pc("POST", OTP, json=otp_body(subject_id=subject))
    assert status == 429 and body["error"] == "subject_otp_limit_reached"
    backdate_last_send(run, first["id"], seconds=120)
    status, body = pc("POST", f"{OTP}/{first['id']}/resend-otp")
    assert status == 429 and body["error"] == "subject_otp_limit_reached"
    assert len(codes) == 3
    # Another subject is unaffected.
    other = {"type": "FAYDA_FAN", "value": f"8{uuid.uuid4().int % 10**11:011d}"}
    raise_request(pc, subject_id=other)


# ── Review fixes: configuration required for real subjects ───────────────────

@pytest.mark.parametrize("setting, value", [("otp_salt", ""), ("otp_log_codes", False)])
def test_no_code_without_salt_or_delivery_channel(portal, as_partner, bank, codes,
                                                  otp_config, monkeypatch, setting, value):
    monkeypatch.setattr(otp_config, setting, value)
    pc = as_partner(bank.audience)
    before = len(pc("GET", "/consent-requests")[1]["items"])
    status, body = pc("POST", OTP, json=otp_body())
    assert status == 503 and body["error"] == "otp_unavailable"
    assert codes == []
    # Refused before anything is stored.
    assert len(pc("GET", "/consent-requests")[1]["items"]) == before


def test_codes_are_logged_only_when_asked(portal, as_partner, bank, codes, otp_config,
                                          monkeypatch):
    import logging

    from openg2p_consent_manager.services.otp_provider import FaydaOtpProvider

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger(otp_config.logging_default_logger_name)
    handler = Capture(level=logging.DEBUG)
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        # A provider that delivers: the log is not needed, so no code in it.
        monkeypatch.setattr(FaydaOtpProvider, "delivers", True)
        monkeypatch.setattr(otp_config, "otp_log_codes", False)
        raise_request(as_partner(bank.audience))
        assert records and not any(codes[-1] in m for m in records)

        monkeypatch.setattr(otp_config, "otp_log_codes", True)
        raise_request(as_partner(bank.audience))
        assert any(codes[-1] in m for m in records)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


# ── Review fixes: the whole current policy applies ───────────────────────────

def set_policy(call, binding, scopes, **changes):
    status, body = call("PUT", f"/consent/v1/partners/{binding}/policy", json={
        "allowed_data_scopes": scopes, "allowed_purposes": ["credit-assessment"],
        "allowed_subject_id_types": ["FAYDA_FAN"], "allowed_signing_algs": ["EdDSA"],
        "max_validity_duration": "P1Y", **changes,
    })
    assert status == 200 and body["status"] == "active", body


def thirty_days():
    return {"validity": {"valid_until": (
        datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}}


POLICY_CHANGES = [
    ({"max_validity_duration": "P1D"}, "validity_exceeds_policy"),
    ({"allowed_purposes": ["another-purpose"]}, "purpose_not_allowed"),
    ({"allowed_subject_id_types": ["national_id"]}, "subject_not_allowed"),
]


@pytest.mark.parametrize("change, reason", POLICY_CHANGES)
def test_verify_rechecks_the_current_policy(call, portal, as_partner, bank, codes,
                                            change, reason):
    pc = as_partner(bank.audience)
    req = raise_request(pc, **thirty_days())
    set_policy(call, bank.fr_binding, ["farmer_personal_details"], **change)
    set_policy(call, bank.csr_binding, CSR_SCOPES, **change)
    status, body = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})
    assert status == 422 and reason in body["error"], body
    assert pc("GET", f"{OTP}/{req['id']}")[1]["consent_id"] is None


def test_verify_drops_only_the_grant_the_policy_no_longer_allows(call, portal, as_partner,
                                                                 bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc, **thirty_days())
    set_policy(call, bank.csr_binding, CSR_SCOPES, max_validity_duration="P1D")
    status, consent = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})
    assert status == 201, consent
    assert [g["data_controller"] for g in consent["grants"]] == [FR]


@pytest.mark.parametrize("change, reason", POLICY_CHANGES)
def test_stored_consent_validated_against_the_current_policy(call, portal, as_partner, bank,
                                                             codes, change, reason):
    pc = as_partner(bank.audience)
    req = raise_request(pc, **thirty_days())
    status, consent = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})
    assert status == 201, consent
    payload = {
        "consent_id": consent["consent_id"], "consent_partner_id": bank.audience,
        "request_context": {"subject_id": {"type": "FAYDA_FAN", "value": "123456789012"}}}
    assert call("POST", "/consent/v1/validate", json=payload)[1]["decision"] == "permit"

    set_policy(call, bank.fr_binding, ["farmer_personal_details"], **change)
    set_policy(call, bank.csr_binding, CSR_SCOPES, **change)
    status, decision = call("POST", "/consent/v1/validate", json=payload)
    assert status == 200 and decision["decision"] == "deny", decision
    assert decision["reason_code"] == reason


# ── Review fixes: receipts signed with the configured (shared) key ───────────

def test_receipt_verifies_with_the_configured_key_elsewhere(portal, as_partner, bank, codes,
                                                            otp_config, monkeypatch):
    """The process that grants the consent (staff api) signs with the key from
    config; another process (partner api, or this one after a restart) loading
    the same key verifies it. An ephemeral key would fail here."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from openg2p_consent_manager.services import CryptoService
    from openg2p_consent_manager.utils.canonical import b64url_decode

    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(otp_config, "cm_signing_p12_path", "")
    monkeypatch.setattr(otp_config, "cm_signing_private_key_pem", pem)
    crypto = CryptoService.get_component()
    monkeypatch.setattr(crypto, "_private_key", crypto._load_signing_key())  # pod start

    pc = as_partner(bank.audience)
    req = raise_request(pc)
    consent = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})[1]
    status, receipt = pc("GET", f"/consents/{consent['consent_id']}/receipt")
    assert status == 200, receipt

    elsewhere = serialization.load_pem_private_key(pem.encode(), password=None).public_key()
    elsewhere.verify(  # raises InvalidSignature on mismatch
        b64url_decode(receipt["signature"]["value"]),
        receipt["consent_artefact"]["hash"].encode("utf-8"),
    )
