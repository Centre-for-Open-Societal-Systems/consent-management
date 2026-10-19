"""The caller's declared use is bound to the consent's purpose: with
``request_context.purpose``, /validate permits only when its code is the
purpose the subject consented to (else deny ``purpose_mismatch``); a permit
returns the consent's purpose so the caller can carry it on (e.g. the
aggregation layer to the registries)."""
from test_assisted_consent import as_partner, bank, portal, realm_key  # noqa: F401
from test_grants import FR, GRANTS, validate
from test_otp_consent import OTP, codes, otp_config, raise_request  # noqa: F401

SUBJECT = {"type": "FAYDA_FAN", "value": "123456789012"}


def stored(call, consent_id, audience, **ctx):
    status, body = call("POST", "/consent/v1/validate", json={
        "consent_id": consent_id, "consent_partner_id": audience,
        "request_context": {"subject_id": SUBJECT, **ctx}})
    assert status == 200, body
    return body


def test_stored_consent_purpose_is_bound(call, portal, as_partner, bank, codes):
    pc = as_partner(bank.audience)
    req = raise_request(pc)  # purpose credit-assessment
    consent_id = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})[1][
        "consent_id"]

    same = stored(call, consent_id, bank.audience, purpose={"code": "credit-assessment"})
    assert same["decision"] == "permit" and same["purpose"]["code"] == "credit-assessment"

    other = stored(call, consent_id, bank.audience, purpose={"code": "marketing"})
    assert other["decision"] == "deny" and other["reason_code"] == "purpose_mismatch"
    assert "credit-assessment" in other["detail"]

    # Not declared: permitted as before, and the consent's purpose comes back.
    unsaid = stored(call, consent_id, bank.audience)
    assert unsaid["decision"] == "permit" and unsaid["purpose"]["code"] == "credit-assessment"


def test_signed_consent_purpose_is_bound(call, bank):
    other = validate(call, bank.sign(bank.claims(grants=GRANTS)), FR,
                     purpose={"code": "marketing"})
    assert other["decision"] == "deny" and other["reason_code"] == "purpose_mismatch"

    same = validate(call, bank.sign(bank.claims(grants=GRANTS)), FR,
                    purpose={"code": "credit-assessment"})
    assert same["decision"] == "permit" and same["purpose"]["code"] == "credit-assessment"
