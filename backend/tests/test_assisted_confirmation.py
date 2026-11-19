# ruff: noqa: F401, F811  (fixtures imported from other test modules are test parameters)
"""Consent scenario 0, assisted confirmation: after the subject's national-ID
login, the partner portal shows the consent's terms and one "I agree" makes
the consent active at once (no signed form, no Consent Approver). Off unless
the install enables it and the partner's policy (department: every scope
policy; Exchange: the consent template, from the use-case policy) allows it.
The signed-form flow (scenario 1) is unchanged."""
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from test_assisted_consent import (
    STAFF,
    as_partner,
    create,
    portal,
    raw,
    realm_key,
    upload,
)
from test_consent_record import _record
from test_exchange import cfg, exchange_on
from test_grants import CSR, CSR_SCOPES, FR, bind
from test_subject_auth import callback, esignet, keys, login, raw_html
from test_template_consent import TEMPLATE, describe, invalidate, templates
from test_template_consent import create as create_from_template


@pytest.fixture
def assisted_on(portal, monkeypatch):
    config = portal.config
    monkeypatch.setattr(config, "assisted_confirmation_enabled", True)
    monkeypatch.setattr(config, "assisted_confirmation_validity_days", 30)
    monkeypatch.setattr(config, "assisted_confirmation_login_max_age_seconds", 600)
    return config


@pytest.fixture
def branch(pm, new_partner):
    """A partner whose scope policies (FR and CSR) allow assisted confirmation."""
    p = new_partner()
    bind(pm, p, FR, ["farmer_personal_details"], allow_assisted_confirmation=True)
    bind(pm, p, CSR, CSR_SCOPES, allow_assisted_confirmation=True)
    return p


def confirm(pc, rid):
    status, terms = pc("GET", f"/consent-requests/{rid}/confirmation")
    assert status == 200, terms
    return pc("POST", f"/consent-requests/{rid}/confirm",
              json={"statement": terms["statement"], "agreed": True}), terms


def test_off_by_default(portal, esignet, as_partner, branch, callback):
    pc = as_partner(branch.audience)
    status, settings = pc("GET", "/request-settings")
    assert status == 200 and settings["assisted_confirmation"] is False
    rid = create(pc)["id"]
    login(pc, rid, esignet, callback)
    status, body = pc("GET", f"/consent-requests/{rid}/confirmation")
    assert status == 403 and "not enabled" in body["error"]


def test_needs_national_id_login_enabled(assisted_on, as_partner, branch):
    pc = as_partner(branch.audience)
    assert pc("GET", "/request-settings")[1]["assisted_confirmation"] is False
    rid = create(pc)["id"]
    status, body = pc("GET", f"/consent-requests/{rid}/confirmation")
    assert status == 409 and "national-ID" in body["error"]


def test_policy_must_allow_it(assisted_on, esignet, as_partner, pm, new_partner, callback):
    p = new_partner()
    bind(pm, p, FR, ["farmer_personal_details"], allow_assisted_confirmation=True)
    bind(pm, p, CSR, CSR_SCOPES)  # not on this one
    pc = as_partner(p.audience)
    rid = create(pc)["id"]
    login(pc, rid, esignet, callback)
    status, body = pc("GET", f"/consent-requests/{rid}/confirmation")
    assert status == 403 and "does not allow" in body["error"]
    status, access = pc("GET", "/access")
    flags = {s["data_controller"]: s["allow_assisted_confirmation"] for s in access["scope_policies"]}
    assert flags == {FR: True, CSR: False}


def test_confirmed_consent_is_active_at_once(call, assisted_on, esignet, as_partner, branch,
                                             callback, raw):
    pc = as_partner(branch.audience)
    assert pc("GET", "/request-settings")[1]["assisted_confirmation"] is True
    rid = create(pc)["id"]
    # The login comes first.
    status, body = pc("POST", f"/consent-requests/{rid}/confirm",
                      json={"statement": "x", "agreed": True})
    assert status == 409
    login(pc, rid, esignet, callback)

    status, terms = pc("GET", f"/consent-requests/{rid}/confirmation")
    assert status == 200, terms
    assert terms["statement"].startswith(f"I agree to share my data with {branch.audience}")
    assert "credit-assessment" in terms["statement"] and "crop_season" in terms["statement"]
    validity = (datetime.fromisoformat(terms["valid_until"])
                - datetime.fromisoformat(terms["valid_from"]))
    assert timedelta(days=29) < validity <= timedelta(days=30)  # the install default (< P1Y)

    status, req = pc("POST", f"/consent-requests/{rid}/confirm",
                     json={"statement": terms["statement"], "agreed": True})
    assert status == 200, req
    assert req["status"] == "approved" and req["consent_id"]
    assert req["verified_by"] is None and "Assisted confirmation" in req["verification_note"]
    # No staff step: the request is not waiting for an approver.
    assert call("POST", f"{STAFF}/{rid}/approve", json={})[0] == 409

    status, consent = pc("GET", f"/consents/{req['consent_id']}")
    assert status == 200 and consent["status"] == "active"
    a = consent["assurance"]
    assert a["method"] == "assisted_confirmation" and a["evidence"] == []
    assert a["confirmed_by"] == f"{branch.audience}-operator"
    assert a["statement"] == terms["statement"] and len(a["statement_sha256"]) == 64
    assert a["subject_authenticated"] is True and a["subject_authentication"]["method"] == "OTP"
    scopes = {g["data_controller"]: sorted(g["effective_data_scopes"]) for g in consent["grants"]}
    assert scopes == {FR: ["farmer_personal_details"], CSR: ["crop_season", "measures"]}

    # The partner's copy says how it was given, with the statement.
    status, _, content = _record(raw, pc, req["consent_id"])
    statement = json.loads(zipfile.ZipFile(io.BytesIO(content)).read("consent-record.json"))
    assert statement["how_given"] == "assisted_confirmation"
    assert statement["confirmation"]["statement"] == terms["statement"]
    assert statement["evidence"] == []
    # Done once only.
    assert confirm_again(pc, rid, terms)[0] == 409


def confirm_again(pc, rid, terms):
    return pc("POST", f"/consent-requests/{rid}/confirm",
              json={"statement": terms["statement"], "agreed": True})


def test_wrong_statement_refused(assisted_on, esignet, as_partner, branch, callback):
    pc = as_partner(branch.audience)
    rid = create(pc)["id"]
    login(pc, rid, esignet, callback)
    status, body = pc("POST", f"/consent-requests/{rid}/confirm",
                      json={"statement": "I agree to anything", "agreed": True})
    assert status == 409 and "terms have changed" in body["error"]
    status, body = pc("POST", f"/consent-requests/{rid}/confirm",
                      json={"statement": "x", "agreed": False})
    assert status in (400, 422)  # agreed must be true


def test_login_too_old(assisted_on, esignet, as_partner, branch, callback, monkeypatch):
    pc = as_partner(branch.audience)
    rid = create(pc)["id"]
    login(pc, rid, esignet, callback)
    monkeypatch.setattr(assisted_on, "assisted_confirmation_login_max_age_seconds", -1)
    status, body = confirm(pc, rid)[0]
    assert status == 409 and "too old" in body["error"]


def test_validity_capped_by_a_shorter_request(assisted_on, esignet, as_partner, branch, callback):
    pc = as_partner(branch.audience)
    until = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    rid = create(pc, validity={"valid_until": until})["id"]
    login(pc, rid, esignet, callback)
    (status, req), terms = confirm(pc, rid)
    assert status == 200, req
    assert datetime.fromisoformat(terms["valid_until"]) <= datetime.fromisoformat(until)


def test_signed_form_flow_unchanged(call, assisted_on, esignet, as_partner, branch, upload,
                                    callback):
    pc = as_partner(branch.audience)
    rid = create(pc)["id"]
    login(pc, rid, esignet, callback)
    assert upload(pc, rid)[0] == 201
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 200
    # Submitted for an approver: no longer open to a confirmation.
    assert pc("GET", f"/consent-requests/{rid}/confirmation")[0] == 409
    status, done = call("POST", f"{STAFF}/{rid}/approve", json={})
    assert status == 200, done
    assurance = pc("GET", f"/consents/{done['consent_id']}")[1]["assurance"]
    assert assurance["method"] == "assisted" and assurance["evidence"] == ["signed_form"]


# ── At the Exchange: allowed by the consent template (the use-case policy) ──


@pytest.fixture
def exchange_branch(cfg, assisted_on, templates, new_partner):
    exchange_on(cfg)
    p = new_partner()
    templates.offer(p.audience, [TEMPLATE])
    return p


def _template_limits(templates, allow):
    templates.templates[TEMPLATE] = describe(TEMPLATE, limits={
        "allowed_subject_id_types": ["FAYDA_FAN"], "max_validity_duration": "P10D",
        "allow_partner_consents": False, "allow_assisted_confirmation": allow,
    })
    invalidate()


def test_exchange_template_must_allow_it(exchange_branch, templates, esignet, as_partner, callback):
    _template_limits(templates, False)
    pc = as_partner(exchange_branch.audience)
    status, req = create_from_template(pc)
    assert status == 201, req
    login(pc, req["id"], esignet, callback)
    status, body = pc("GET", f"/consent-requests/{req['id']}/confirmation")
    assert status == 403 and TEMPLATE in body["error"]
    access = pc("GET", "/access")[1]
    assert access["templates"][0]["allow_assisted_confirmation"] is False


def test_exchange_confirmed_from_template(exchange_branch, templates, esignet, as_partner, callback):
    _template_limits(templates, True)
    pc = as_partner(exchange_branch.audience)
    assert pc("GET", "/access")[1]["templates"][0]["allow_assisted_confirmation"] is True
    status, req = create_from_template(pc)
    assert status == 201, req
    login(pc, req["id"], esignet, callback)
    (status, done), terms = confirm(pc, req["id"])
    assert status == 200, done
    # The template's longest validity (P10D) caps the install's 30 days.
    validity = (datetime.fromisoformat(terms["valid_until"])
                - datetime.fromisoformat(terms["valid_from"]))
    assert validity <= timedelta(days=10)
    consent = pc("GET", f"/consents/{done['consent_id']}")[1]
    assert consent["assurance"]["method"] == "assisted_confirmation"
    assert consent["template"] == TEMPLATE
