"""Consent by lookup at a registry (G2P-5719, contract §13) and partner-signed
consents allowed per policy (§14).

§13: a registry's partner API calls its CM's /validate with no consent:
``partner_id`` + ``data_controller`` + ``request_context.subject_id``. The CM
finds the consent it holds for (partner, subject, controller), active and
valid now (latest ``valid_until``, then newest), and validates it as a stored
consent under the partner's scope policy for that registry.

§14: a partner-signed consent (``consent_jws``) is accepted only when the
applicable policy sets ``allow_partner_consents``; consents the CM holds and
consent tokens are unaffected.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from test_consent_lookup import set_artefact
from test_exchange import (  # noqa: F401 (fixture)
    PRESENTER,
    _sign_receipt,
    cfg,
    department_on,
    exchange_on,
    post,
)
from test_grants import CSR, FR, GRANTS, bind, validate

SUBJECT = {"type": "FAYDA_FAN", "value": "123456789012"}
FR_GRANT = {"data_controller": FR, "data_scopes": ["farmer_personal_details"]}
CSR_GRANT = {"data_controller": CSR, "data_scopes": ["crop_season", "measures"]}


@pytest.fixture
def partner(pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, ["farmer_personal_details", "farm_details"])
    bind(pm, p, CSR, ["crop_season", "measures"])
    return p


def originate(call, p, grants=(FR_GRANT, CSR_GRANT), subject=SUBJECT):
    """An originated consent (consent request, authenticated, approved)."""
    status, req = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": subject, "partner_id": p.audience,
        "purpose": {"code": "credit-assessment"}, "grants": list(grants),
    })
    assert status == 201, req
    token = jwt.encode({"sub": subject["value"], "iss": "test"}, "k" * 32, algorithm="HS256")
    status, _ = call("POST", f"/consent/v1/consent-requests/{req['id']}/authenticate",
                     json={"id_token": token})
    assert status == 200
    status, art = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                       json={"grants": list(grants)})
    assert status == 201, art
    return art["id"]


def lookup(call, partner_id, controller=FR, subject=SUBJECT, **ctx):
    payload = {"partner_id": partner_id, "request_context": {"subject_id": subject, **ctx}}
    if controller:
        payload["data_controller"] = controller
    return post(call, payload)


# ── §13 consent by lookup at a registry ─────────────────────────────────────


def test_finds_an_originated_consent_with_several_grants(call, partner):
    consent_id = originate(call, partner)
    fr = lookup(call, partner.audience, FR)
    assert fr["decision"] == "permit", fr
    assert fr["consent_id"] == consent_id
    assert fr["data_controller"] == FR
    assert fr["effective_data_scopes"] == ["farmer_personal_details"]
    assert fr["policy_ref"] == f"data_controller:{FR}"
    csr = lookup(call, partner.audience, CSR, requested_scopes=["crop_season"])
    assert csr["decision"] == "permit", csr
    assert csr["consent_id"] == consent_id
    assert csr["effective_data_scopes"] == ["crop_season"]


def test_finds_a_single_controller_consent(call, partner):
    consent_id = originate(call, partner, grants=[FR_GRANT])
    body = lookup(call, partner.audience, FR)
    assert body["decision"] == "permit" and body["consent_id"] == consent_id, body
    assert lookup(call, partner.audience, CSR)["reason_code"] == "consent_not_found"


def test_finds_a_partner_signed_consent_after_first_use(call, partner):
    first = validate(call, partner.sign(partner.claims(grants=GRANTS)), FR)
    assert first["decision"] == "permit", first
    body = lookup(call, partner.audience, FR)
    assert body["decision"] == "permit", body
    assert body["consent_id"] == first["consent_id"]
    assert body["effective_data_scopes"] == first["effective_data_scopes"]
    # Stored for FR only (CSR never validated it).
    assert lookup(call, partner.audience, CSR)["reason_code"] == "consent_not_found"


def test_template_consent_not_found_at_a_registry(call, run, partner):
    """A consent made from a consent template (exchange role) is presented
    through the token presenter, not by a direct call to the registry."""
    consent_id = originate(call, partner)
    set_artefact(run, consent_id, template="loan-profile@1")
    assert lookup(call, partner.audience, FR)["reason_code"] == "consent_not_found"


def test_picks_latest_valid_until_then_newest(call, run, partner):
    now = datetime.now(timezone.utc)
    first, second, third = (originate(call, partner) for _ in range(3))
    set_artefact(run, first, valid_until=now + timedelta(days=90))
    set_artefact(run, second, valid_until=now + timedelta(days=10))
    set_artefact(run, third, valid_until=now + timedelta(days=30))
    assert lookup(call, partner.audience)["consent_id"] == first
    set_artefact(run, third, valid_until=now + timedelta(days=90))
    assert lookup(call, partner.audience)["consent_id"] == third


@pytest.mark.parametrize("case", ["other_controller", "other_partner", "other_subject",
                                  "other_subject_type", "revoked", "expired"])
def test_not_found(case, call, run, pm, new_partner, partner):
    consent_id = originate(call, partner, grants=[FR_GRANT])
    partner_id, controller, subject = partner.audience, FR, SUBJECT
    if case == "other_controller":
        controller = CSR
    elif case == "other_partner":
        other = new_partner()
        bind(pm, other, FR, ["farmer_personal_details"])
        partner_id = other.audience
    elif case == "other_subject":
        subject = {**SUBJECT, "value": "999999999999"}
    elif case == "other_subject_type":
        # Subject id types are matched exactly (no equivalence).
        subject = {**SUBJECT, "type": "UIN"}
    elif case == "revoked":
        set_artefact(run, consent_id, status="revoked")
    elif case == "expired":
        set_artefact(run, consent_id,
                     valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    body = lookup(call, partner_id, controller, subject)
    assert body["decision"] == "deny" and body["reason_code"] == "consent_not_found", body
    assert body["consent_id"] is None


def test_scope_policy_applied(call, pm, partner):
    originate(call, partner)
    # The registry's scope policy now excludes the consented scope.
    bind(pm, partner, FR, ["farm_details"])
    body = lookup(call, partner.audience, FR)
    assert body["decision"] == "deny" and body["reason_code"] == "scope_exceeds_policy", body
    # No scope policy for the registry: nothing granted.
    pm.revoke(partner.audience, CSR)
    assert lookup(call, partner.audience, CSR)["reason_code"] == "scope_exceeds_policy"
    # requested_scopes narrow further.
    bind(pm, partner, CSR, ["crop_season", "measures"])
    body = lookup(call, partner.audience, CSR, requested_scopes=["measures", "other"])
    assert body["decision"] == "permit" and body["effective_data_scopes"] == ["measures"], body


def test_lookup_needs_partner_subject_and_controller(call, partner):
    originate(call, partner)
    for body in (
        lookup(call, partner.audience, controller=None),
        post(call, {"partner_id": partner.audience, "data_controller": FR}),
        post(call, {"data_controller": FR, "request_context": {"subject_id": SUBJECT}}),
    ):
        assert body["decision"] == "deny" and body["reason_code"] == "malformed_object", body


def test_terms_mode_lookup_is_by_purpose(call, cfg, partner):
    """The presenter's lookup is by (partner, subject, purpose): a registry
    consent for that purpose is found, under the terms sent (no scope
    policy). A purpose sent by a registry is not a term."""
    consent_id = originate(call, partner)
    exchange_on(cfg)
    body = post(call, {
        "partner_id": PRESENTER, "consent_partner_id": partner.audience,
        "request_context": {"subject_id": SUBJECT, "purpose": "credit-assessment",
                            "requested_scopes": ["farmer_personal_details"]},
    })
    assert body["decision"] == "permit" and body["consent_id"] == consent_id, body
    assert body["grants"] == {FR: ["farmer_personal_details"]}
    body = post(call, {
        "partner_id": PRESENTER, "consent_partner_id": partner.audience,
        "request_context": {"subject_id": SUBJECT, "purpose": "insurance",
                            "requested_scopes": ["farmer_personal_details"]},
    })
    assert body["reason_code"] == "consent_not_found", body
    body = lookup(call, partner.audience, FR, purpose="insurance")
    assert body["decision"] == "permit", body


# ── §14 partner-signed consents allowed per policy ──────────────────────────


def test_partner_signed_consent_denied_without_the_flag(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, ["farmer_personal_details"], allow_partner_consents=False)
    body = validate(call, p.sign(p.claims(grants=[FR_GRANT])), FR)
    assert body["decision"] == "deny", body
    assert body["reason_code"] == "partner_consent_not_accepted"
    # Absent from PM's answer: false.
    from openg2p_consent_manager.services import pm_partner_id

    bind(pm, p, FR, ["farmer_personal_details"])
    pm.policies[pm_partner_id(p.audience)][("data_controller", FR)].pop(
        "allow_partner_consents"
    )
    pm._changed(p.audience)
    body = validate(call, p.sign(p.claims(grants=[FR_GRANT])), FR)
    assert body["reason_code"] == "partner_consent_not_accepted", body
    # Set: accepted.
    bind(pm, p, FR, ["farmer_personal_details"], allow_partner_consents=True)
    assert validate(call, p.sign(p.claims(grants=[FR_GRANT])), FR)["decision"] == "permit"


def test_partner_signed_consent_denied_in_terms_mode(call, cfg, new_partner):
    exchange_on(cfg)
    p = new_partner()
    body = post(call, {
        "consent_jws": p.sign(p.claims(grants=GRANTS)), "partner_id": PRESENTER,
        "data_controller": FR,
        "request_context": {"subject_id": SUBJECT, "purpose": "credit-assessment",
                            "requested_scopes": ["farmer_personal_details"],
                            "limits": {"allow_partner_consents": False}},
    })
    assert body["decision"] == "deny", body
    assert body["reason_code"] == "partner_consent_not_accepted"


def test_held_consents_unaffected_by_the_flag(call, pm, partner):
    consent_id = originate(call, partner, grants=[FR_GRANT])
    bind(pm, partner, FR, ["farmer_personal_details"], allow_partner_consents=False)
    by_id = post(call, {"consent_id": consent_id, "consent_partner_id": partner.audience,
                        "data_controller": FR})
    assert by_id["decision"] == "permit", by_id
    by_lookup = lookup(call, partner.audience, FR)
    assert by_lookup["decision"] == "permit" and by_lookup["consent_id"] == consent_id


def test_consent_tokens_unaffected_by_the_flag(call, cfg, env, pm):
    from conftest import Partner

    presenter = Partner(env, audience="flag-composite")
    bind(pm, presenter, FR, ["farmer_personal_details"], allow_partner_consents=False)
    department_on(cfg, presenter=presenter.audience)
    body = post(call, {"consent_jws": _sign_receipt(presenter=presenter.audience),
                       "partner_id": presenter.audience, "data_controller": FR})
    assert body["decision"] == "permit", body
