"""Caller-terms mode on /validate (G2P-5719, design decision #8): a configured
token presenter (the Composite) sends the terms of the request — the purpose,
the requested scopes and limits (and a template code, a label). They replace
the consenting partner's per-registry scope policies; the Consent Manager
reads no partner policy and knows no use case."""
import pytest
from test_assisted_consent import (  # noqa: F401 (fixtures)
    approved_consent,
    as_partner,
    portal,
    realm_key,
    upload,
)
from test_exchange import PRESENTER, cfg, exchange_on, post  # noqa: F401 (fixture)
from test_grants import CSR, CSR_SCOPES, FR, FR_SCOPES, GRANTS, bind

TEMPLATE = "loan-profile@1"
# The scopes the presenter asks each registry for.
SCOPES = ["farmer_personal_details", "crop_season"]
LIMITS = {"allowed_subject_id_types": ["FAYDA_FAN"], "max_validity_duration": "P1Y",
          "allow_partner_consents": True, "allowed_signing_algs": ["EdDSA"]}


def ctx(limits=None, **extra):
    base = {"purpose": "credit-assessment", "requested_scopes": SCOPES,
            "limits": {**LIMITS, **(limits or {})}, "template": TEMPLATE}
    base.update(extra)
    return {k: v for k, v in base.items() if v is not None}


@pytest.fixture
def bank(new_partner):
    """A partner with no policy in the Exchange's PM (only its keys)."""
    return new_partner()


def issue(call, bank, presenter=PRESENTER, claims=None, data_controller=None, **context):
    jws = bank.sign(claims or bank.claims(grants=GRANTS))
    payload = {"consent_jws": jws, "partner_id": presenter, "issue_receipts": True,
               "request_context": ctx(**context)}
    if data_controller:
        payload["data_controller"] = data_controller
    return post(call, payload)


def decisions(call, audience):
    status, rows = call("GET", "/consent/v1/decisions", params={"partner_id": audience})
    assert status == 200, rows
    return rows


def test_terms_permit_limits_scopes_to_consent_and_request(call, cfg, pm, bank):
    exchange_on(cfg)
    pm.requests.clear()
    body = issue(call, bank)
    assert body["decision"] == "permit", body
    assert set(body["receipts"]) == {FR, CSR}
    # consent grant ∩ requested scopes, per registry.
    assert body["grants"] == {FR: ["farmer_personal_details"], CSR: ["crop_season"]}
    # No partner policy was read from Partner Management.
    assert pm.requests == []


def test_template_recorded_as_the_reference(call, cfg, bank):
    exchange_on(cfg)
    body = issue(call, bank, data_controller=FR)
    assert body["decision"] == "permit", body
    assert body["policy_ref"] == f"template:{TEMPLATE}"
    assert body["policy_id"] is None and body["policy_version"] is None
    rows = [r for r in decisions(call, bank.audience) if r["decision"] == "permit"]
    assert rows and all(r["policy_ref"] == f"template:{TEMPLATE}" for r in rows)
    # Without a template: no reference.
    other = issue(call, bank, claims=bank.claims(grants=GRANTS), template=None)
    assert other["decision"] == "permit" and other["policy_ref"] is None


def test_terms_need_requested_scopes(call, cfg, bank):
    """Without the scopes the whole consent would pass: denied."""
    exchange_on(cfg)
    body = issue(call, bank, requested_scopes=[])
    assert body["decision"] == "deny" and body["reason_code"] == "scope_exceeds_policy"


def test_purpose_must_match_consent(call, cfg, bank):
    exchange_on(cfg)
    body = issue(call, bank, purpose="marketing")
    assert body["decision"] == "deny" and body["reason_code"] == "purpose_not_allowed"


@pytest.mark.parametrize("limits,reason", [
    ({"allowed_subject_id_types": ["PHONE"]}, "subject_not_allowed"),
    ({"max_validity_duration": "P7D"}, "validity_exceeds_policy"),
    ({"allowed_signing_algs": ["ES256"]}, "signature_invalid"),
    ({"allow_partner_consents": False}, "partner_consent_not_accepted"),
])
def test_limits_apply(limits, reason, call, cfg, bank):
    exchange_on(cfg)
    body = issue(call, bank, limits=limits)
    assert body["decision"] == "deny" and body["reason_code"] == reason, body


def test_absent_limits(call, cfg, bank):
    """Absent limits: none, except allow_partner_consents (false)."""
    exchange_on(cfg)
    body = post(call, {"consent_jws": bank.sign(bank.claims(grants=GRANTS)),
                       "partner_id": PRESENTER, "issue_receipts": True,
                       "request_context": {"purpose": "credit-assessment",
                                           "requested_scopes": SCOPES}})
    assert body["reason_code"] == "partner_consent_not_accepted", body
    body = post(call, {"consent_jws": bank.sign(bank.claims(grants=GRANTS)),
                       "partner_id": PRESENTER, "issue_receipts": True,
                       "request_context": {"purpose": "credit-assessment",
                                           "requested_scopes": SCOPES,
                                           "limits": {"allow_partner_consents": True}}})
    assert body["decision"] == "permit", body


def test_terms_from_a_caller_that_is_not_a_presenter(call, cfg, bank):
    """Only a configured presenter may send terms: limits or a template from
    anyone else are refused."""
    exchange_on(cfg)
    jws = bank.sign(bank.claims(grants=GRANTS))
    for context in (ctx(), {"limits": {"allow_partner_consents": True}},
                    {"template": TEMPLATE}):
        body = post(call, {"consent_jws": jws, "data_controller": FR,
                           "partner_id": bank.audience, "request_context": context})
        assert body["decision"] == "deny", body
        assert body["reason_code"] == "caller_terms_not_allowed", body


def test_purpose_alone_from_another_caller_is_ignored(call, cfg, bank):
    """purpose from a caller that is not a presenter is not a term: the
    partner's scope policies apply (none here)."""
    exchange_on(cfg)
    jws = bank.sign(bank.claims(grants=GRANTS))
    body = post(call, {"consent_jws": jws, "data_controller": FR, "partner_id": bank.audience,
                       "request_context": {"purpose": "credit-assessment"}})
    assert body["decision"] == "deny" and body["reason_code"] == "unknown_partner"


def test_limits_without_purpose_are_malformed(call, cfg, bank):
    exchange_on(cfg)
    body = issue(call, bank, purpose=None)
    assert body["decision"] == "deny" and body["reason_code"] == "malformed_object"


def test_use_case_is_not_a_term(call, cfg, bank):
    """request_context.use_case is no longer read: without a purpose the
    partner's scope policies apply."""
    exchange_on(cfg)
    jws = bank.sign(bank.claims(grants=GRANTS))
    body = post(call, {"consent_jws": jws, "partner_id": PRESENTER, "data_controller": FR,
                       "request_context": {"use_case": TEMPLATE,
                                           "requested_scopes": SCOPES}})
    assert body["decision"] == "deny" and body["reason_code"] == "unknown_partner"


def test_stored_decision_is_not_reused_for_another_purpose(call, cfg, bank):
    exchange_on(cfg)
    claims = bank.claims(grants=GRANTS)
    assert issue(call, bank, claims=claims)["decision"] == "permit"
    again = issue(call, bank, claims=claims, purpose="insurance")
    assert again["reason_code"] == "purpose_not_allowed"


def test_without_purpose_the_exchange_uses_scope_policies(call, cfg, pm, bank):
    exchange_on(cfg)
    bind(pm, bank, FR, ["farmer_personal_details"])
    bind(pm, bank, CSR, CSR_SCOPES)
    jws = bank.sign(bank.claims(grants=GRANTS))
    body = post(call, {"consent_jws": jws, "partner_id": PRESENTER, "issue_receipts": True})
    assert body["decision"] == "permit", body
    assert body["grants"] == {FR: ["farmer_personal_details"], CSR: sorted(CSR_SCOPES)}


def test_stored_consent_in_terms_mode(call, cfg, portal, as_partner, pm, upload,  # noqa: F811
                                      new_partner):
    p = new_partner()
    bind(pm, p, FR, ["farmer_personal_details"])
    bind(pm, p, CSR, CSR_SCOPES)
    # A scope-policy consent, made before the exchange role is on (at the
    # Exchange consents are made from a consent template).
    consent_id = approved_consent(call, as_partner(p.audience), upload)
    exchange_on(cfg)
    # The scope policies no longer matter in this mode.
    pm.revoke(p.audience, FR)
    ask = {"consent_id": consent_id, "consent_partner_id": p.audience,
           "partner_id": PRESENTER, "issue_receipts": True}

    body = post(call, {**ask, "request_context": ctx()})
    assert body["decision"] == "permit", body
    assert body["grants"] == {FR: ["farmer_personal_details"], CSR: ["crop_season"]}
    assert set(body["receipts"]) == {FR, CSR}
    assert post(call, {**ask, "request_context": ctx(purpose="marketing")})[
        "reason_code"] == "purpose_not_allowed"
    assert post(call, {**ask, "request_context": ctx(
        limits={"allowed_subject_id_types": ["PHONE"]})})["reason_code"] == "subject_not_allowed"
    assert post(call, {**ask, "request_context": ctx(
        limits={"max_validity_duration": "P1D"})})["reason_code"] == "validity_exceeds_policy"
    # The consent's own scopes are the ceiling.
    body = post(call, {**ask, "request_context": ctx(requested_scopes=FR_SCOPES)})
    assert body["grants"] == {FR: ["farmer_personal_details"]}
