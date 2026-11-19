"""The full reference of the PM policy applied (G2P-5719): PM's id of that
policy version and its key (kind:key) are recorded on decision logs,
artefacts and grants, and returned by /validate, the decisions API and the
artefact responses — not just the version."""
from test_assisted_consent import (  # noqa: F401 (fixtures)
    approved_consent,
    as_partner,
    portal,
    realm_key,
    upload,
)
from test_exchange import cfg, exchange_on  # noqa: F401 (fixture)
from test_grants import CSR, CSR_SCOPES, FR, FR_SCOPES, GRANTS, bind, validate
from test_caller_terms_mode import TEMPLATE, issue


def policy_of(pm, audience, *, data_controller=None):
    from openg2p_consent_manager.services import pm_partner_id

    return pm.policies[pm_partner_id(audience)][("data_controller", data_controller)]


def decisions(call, audience):
    status, rows = call("GET", "/consent/v1/decisions", params={"partner_id": audience})
    assert status == 200, rows
    return rows


def test_scope_policy_reference_on_decision_and_log(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    policy = policy_of(pm, p.audience, data_controller=FR)
    body = validate(call, p.sign(p.claims(data_controller=FR, data_scopes=FR_SCOPES)), FR)
    assert body["decision"] == "permit", body
    assert body["policy_id"] == policy["id"]
    assert body["policy_ref"] == f"data_controller:{FR}" and body["policy_version"] == 1
    row = decisions(call, p.audience)[0]
    assert (row["policy_id"], row["policy_ref"], row["policy_version"]) == (
        policy["id"], f"data_controller:{FR}", 1)


def test_deny_after_policy_read_records_the_policy(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES, allowed_purposes=["insurance"])
    body = validate(call, p.sign(p.claims(data_controller=FR, data_scopes=FR_SCOPES)), FR)
    assert body["reason_code"] == "purpose_not_allowed"
    row = decisions(call, p.audience)[0]
    assert row["policy_ref"] == f"data_controller:{FR}"
    assert row["policy_id"] == policy_of(pm, p.audience, data_controller=FR)["id"]


def test_caller_terms_reference(call, cfg, pm, new_partner):  # noqa: F811
    """Caller-terms mode reads no PM policy: the template the presenter named
    is the reference; nothing else."""
    exchange_on(cfg)
    p = new_partner()
    body = issue(call, p, claims=p.claims(grants=GRANTS))
    assert body["decision"] == "permit", body
    rows = [r for r in decisions(call, p.audience) if r["decision"] == "permit"]
    assert rows and all(
        (r["policy_id"], r["policy_ref"], r["policy_version"])
        == (None, f"template:{TEMPLATE}", None)
        for r in rows
    )
    body = issue(call, p, purpose="insurance")
    assert body["reason_code"] == "purpose_not_allowed"
    row = next(r for r in decisions(call, p.audience) if r["reason_code"] == "purpose_not_allowed")
    assert row["policy_id"] is None and row["policy_ref"] == f"template:{TEMPLATE}"


def test_originated_consent_records_the_policy(call, portal, as_partner, pm, upload,  # noqa: F811
                                               new_partner):
    p = new_partner()
    bind(pm, p, FR, ["farmer_personal_details"])
    bind(pm, p, CSR, CSR_SCOPES)
    pc = as_partner(p.audience)
    consent_id = approved_consent(call, pc, upload)
    status, consent = pc("GET", f"/consents/{consent_id}")
    assert status == 200, consent
    fr = next(g for g in consent["grants"] if g["data_controller"] == FR)
    assert fr["policy_ref"] == f"data_controller:{FR}"
    assert fr["policy_id"] == policy_of(pm, p.audience, data_controller=FR)["id"]


def test_common_policy_fields():
    from openg2p_consent_manager.services.pm_policy_client import common_policy_fields

    one = {"policy_version": 1, "policy_id": "a", "policy_ref": "data_controller:x"}
    assert common_policy_fields([one, dict(one)]) == one
    assert common_policy_fields([one, {**one, "policy_id": "b"}]) == {
        "policy_version": None, "policy_id": None, "policy_ref": None}
