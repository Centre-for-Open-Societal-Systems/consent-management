"""The partner's policy in Partner Management applied on /validate (G2P-5719):
each policy field the evaluation reads, the PM id convention, and that the
CM's own partner / policy / approval APIs are gone."""
import pytest
from test_grants import FR, FR_SCOPES, bind, validate


@pytest.fixture
def partner(pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    return p


def legacy(p, **extra):
    return p.sign(p.claims(data_controller=FR, data_scopes=FR_SCOPES, **extra))


def test_pm_partner_id_convention():
    from openg2p_consent_manager.services import pm_partner_id

    assert pm_partner_id("bank-a") == "PARTNER_BANK_A"
    assert pm_partner_id("agri-composite") == "PARTNER_AGRI_COMPOSITE"


def test_policy_is_read_from_pm_by_convention(call, pm, partner):
    pm.requests.clear()
    pm.client.invalidate(partner.audience)
    dec = validate(call, legacy(partner), FR)
    assert dec["decision"] == "permit", dec
    assert dec["policy_version"] == 1
    req = pm.requests[-1]
    assert req.url.path == f"/policies/{partner.pm_id}"
    assert req.url.params["data_controller"] == FR


@pytest.mark.parametrize("policy, claims, reason", [
    ({"allowed_purposes": ["marketing"]}, {}, "purpose_not_allowed"),
    ({"allowed_subject_id_types": ["PHONE"]}, {}, "subject_not_allowed"),
    ({"allowed_signing_algs": ["ES256"]}, {}, "signature_invalid"),
    ({"max_validity_duration": "P7D"}, {}, "validity_exceeds_policy"),
    ({"allowed_data_scopes": ["livestock"]}, {}, "scope_exceeds_policy"),
])
def test_policy_fields_are_applied(call, pm, new_partner, policy, claims, reason):
    p = new_partner()
    fields = dict(policy)
    scopes = fields.pop("allowed_data_scopes", FR_SCOPES)
    bind(pm, p, FR, scopes, **fields)
    dec = validate(call, legacy(p, **claims), FR)
    assert dec["decision"] == "deny" and dec["reason_code"] == reason, dec
    assert dec["data_controller"] == FR


def test_empty_lists_mean_any(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES, allowed_purposes=[], allowed_subject_id_types=[],
         max_validity_duration=None)
    dec = validate(call, legacy(p), FR)
    assert dec["decision"] == "permit", dec


def test_unknown_partner_when_pm_answers_404(call, new_partner):
    p = new_partner()  # keys, but no policy in PM
    dec = validate(call, legacy(p), FR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "unknown_partner"
    assert "Partner Management" in dec["detail"]


@pytest.mark.parametrize("method, path", [
    ("GET", "/consent/v1/partners"),
    ("POST", "/consent/v1/partners"),
    ("PUT", "/consent/v1/partners/x/policy"),
    ("GET", "/consent/v1/meta"),
    ("GET", "/consent/v1/awe/tasks"),
    ("POST", "/consent/v1/awe/webhooks/decision"),
])
def test_partner_policy_and_awe_apis_are_gone(call, method, path):
    status, _ = call(method, path, json={} if method != "GET" else None)
    assert status in (404, 405)


def test_decision_filter_is_validated(call):
    status, _ = call("GET", "/consent/v1/decisions", params={"decision": "maybe"})
    assert status == 400
    status, _ = call("GET", "/consent/v1/decisions", params={"decision": "deny"})
    assert status == 200
