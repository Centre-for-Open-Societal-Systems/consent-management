"""``consent_method`` on a partner's policy: how the subject's consent must
reach /validate for that binding.

- not set / ``partner_signed``: as before — a partner-signed consent_jws or a
  stored consent (consent_id) is accepted;
- ``otp``: only a consent the subject confirmed with an OTP at the CM,
  presented by its consent_id; a consent_jws (or an assisted consent) is
  denied with ``consent_method_not_allowed``;
- ``internal``: validated like partner_signed (the label says why).

Clients that do not send the field change nothing.
"""
import pytest
from test_assisted_consent import (  # noqa: F401 - fixtures
    approved_consent,
    as_partner,
    bank,
    portal,
    realm_key,
    upload,
)
from test_grants import CSR, CSR_SCOPES, FR, GRANTS, validate
from test_otp_consent import OTP, codes, otp_config, raise_request  # noqa: F401

POLICY = "/consent/v1/partners/{}/policy"
SUBJECT = {"subject_id": {"type": "FAYDA_FAN", "value": "123456789012"}}


def put_policy(call, binding, scopes, **extra):
    status, body = call("PUT", POLICY.format(binding), json={
        "allowed_data_scopes": scopes, "allowed_purposes": ["credit-assessment"],
        "allowed_subject_id_types": ["FAYDA_FAN"], "allowed_signing_algs": ["EdDSA"],
        "max_validity_duration": "P1Y", **extra,
    })
    assert status == 200, body
    return body


def make_otp_partner(call, partner):
    put_policy(call, partner.fr_binding, ["farmer_personal_details"], consent_method="otp")
    put_policy(call, partner.csr_binding, CSR_SCOPES, consent_method="otp")


def otp_consent(pc, codes):  # noqa: F811
    req = raise_request(pc)
    status, consent = pc("POST", f"{OTP}/{req['id']}/verify-otp", json={"otp": codes[-1]})
    assert status == 201, consent
    return consent["consent_id"]


def by_id(call, consent_id, audience):
    status, body = call("POST", "/consent/v1/validate", json={
        "consent_id": consent_id, "consent_partner_id": audience, "request_context": SUBJECT})
    assert status == 200, body
    return body


def test_existing_partner_unchanged(call, bank):
    """No consent_method anywhere: the policy reads null and a partner-signed
    consent still permits, exactly as before."""
    status, policy = call("GET", POLICY.format(bank.fr_binding))
    assert status == 200 and policy["consent_method"] is None
    jws = bank.sign(bank.claims(grants=GRANTS))
    assert validate(call, jws, FR)["decision"] == "permit"


def test_otp_partner_gets_data_only_through_otp(call, portal, as_partner, bank, codes):
    make_otp_partner(call, bank)

    # Its own signed consent is refused...
    dec = validate(call, bank.sign(bank.claims(grants=GRANTS)), FR)
    assert dec["decision"] == "deny"
    assert dec["reason_code"] == "consent_method_not_allowed"

    # ...the consent the farmer confirmed with the OTP is honoured.
    consent_id = otp_consent(as_partner(bank.audience), codes)
    dec = by_id(call, consent_id, bank.audience)
    assert dec["decision"] == "permit", dec
    assert set(dec["grants"]) == {FR, CSR}


def test_otp_partner_assisted_consent_refused(call, portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    consent_id = approved_consent(call, pc, upload)
    assert by_id(call, consent_id, bank.audience)["decision"] == "permit"

    make_otp_partner(call, bank)
    dec = by_id(call, consent_id, bank.audience)
    assert dec["decision"] == "deny" and dec["reason_code"] == "consent_method_not_allowed"


def test_jws_permitted_before_switch_to_otp_is_refused_after(call, bank):
    jws = bank.sign(bank.claims(grants=GRANTS))
    assert validate(call, jws, FR)["decision"] == "permit"
    make_otp_partner(call, bank)
    # The same jti again: no stored decision is handed back.
    dec = validate(call, jws, FR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "consent_method_not_allowed"


@pytest.mark.parametrize("method", ["partner_signed", "internal"])
def test_partner_signed_and_internal_accept_signed_consents(call, bank, method):
    put_policy(call, bank.fr_binding, ["farmer_personal_details"], consent_method=method)
    dec = validate(call, bank.sign(bank.claims(grants=GRANTS)), FR)
    assert dec["decision"] == "permit", dec


def test_otp_on_one_binding_only(call, bank):
    """The method is per binding: the CSR binding is still partner_signed."""
    put_policy(call, bank.fr_binding, ["farmer_personal_details"], consent_method="otp")
    jws = bank.sign(bank.claims(grants=GRANTS))
    assert validate(call, jws, FR)["reason_code"] == "consent_method_not_allowed"
    assert validate(call, jws, CSR)["decision"] == "permit"


def test_left_out_keeps_the_method_null_resets_it(call, bank):
    assert put_policy(call, bank.fr_binding, ["farmer_personal_details"],
                      consent_method="otp")["consent_method"] == "otp"
    # A client that does not know the field (e.g. the current console UI).
    assert put_policy(call, bank.fr_binding, ["farmer_personal_details"])[
        "consent_method"] == "otp"
    assert put_policy(call, bank.fr_binding, ["farmer_personal_details"],
                      consent_method=None)["consent_method"] is None
    assert put_policy(call, bank.fr_binding, ["farmer_personal_details"],
                      consent_method=" OTP ")["consent_method"] == "otp"


def test_unknown_consent_method_rejected(call, bank):
    status, body = call("PUT", POLICY.format(bank.fr_binding), json={
        "allowed_data_scopes": ["farmer_personal_details"], "allowed_signing_algs": ["EdDSA"],
        "consent_method": "sms"})
    assert status == 400 and "consent_method" in str(body)


def test_partner_portal_shows_the_method(call, portal, as_partner, bank):
    pc = as_partner(bank.audience)
    by = {b["data_controller"]: b for b in pc("GET", "/bindings")[1]}
    assert by[FR]["consent_method"] == "partner_signed"
    make_otp_partner(call, bank)
    by = {b["data_controller"]: b for b in pc("GET", "/bindings")[1]}
    assert by[FR]["consent_method"] == "otp" and by[CSR]["consent_method"] == "otp"


@pytest.mark.parametrize("old, new, widens", [
    ("otp", None, True),
    ("otp", "partner_signed", True),
    ("partner_signed", "internal", True),
    (None, "internal", True),
    (None, "otp", False),
    ("internal", "partner_signed", False),
    ("partner_signed", None, False),
])
def test_loosening_the_method_needs_approval(old, new, widens):
    """With AWE on, a less strict method is a widening (approval first)."""
    from types import SimpleNamespace

    from openg2p_consent_manager.services import PartnerService

    base = dict(
        allowed_data_scopes=["a"], allowed_purposes=["p"], allowed_subject_id_types=["t"],
        allowed_signing_algs=["EdDSA"], max_validity_duration="P1Y", data_life=None,
        fetch_type="oneshot", max_fetch_frequency=None,
    )
    active = SimpleNamespace(**base, consent_method=old)
    data = SimpleNamespace(**base, consent_method=new)
    assert PartnerService._is_widening(data, active) is widens
