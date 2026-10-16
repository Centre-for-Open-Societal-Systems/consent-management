"""Server-side validation of the binding / policy fields the admin UI offers as
fixed choices (G2P-5719), the /meta endpoint that serves those choices, and that
a policy stored before these rules still loads (flagged in ``issues``)."""
import uuid

import pytest

CONTROLLER = "farmer-registry"


@pytest.fixture
def binding(call):
    status, body = call("POST", "/consent/v1/partners", json={
        "audience": f"val-{uuid.uuid4().hex[:8]}", "controller_id": CONTROLLER,
    })
    assert status == 201, body
    return body["id"]


def put_policy(call, binding_id, **fields):
    payload = {"allowed_data_scopes": ["farm_details"], "allowed_signing_algs": ["EdDSA"]}
    payload.update(fields)
    return call("PUT", f"/consent/v1/partners/{binding_id}/policy", json=payload)


def error_text(body):
    # The platform's handler: 400 {"errors": [{"code", "message"}]}.
    return " ".join(e["message"] for e in body["errors"])


def test_meta_lists_allowed_values(call, binding):
    status, body = call("GET", "/consent/v1/meta")
    assert status == 200, body
    assert body["signing_algorithms"] == ["EdDSA", "ES256", "RS256"]
    assert body["fetch_types"] == ["oneshot", "periodic"]
    assert body["partner_statuses"] == ["active", "suspended"]
    assert CONTROLLER in body["known_controller_ids"]
    # The configured default subject id type is always suggested.
    assert "national_id" in body["known_subject_id_types"]


def test_valid_policy_is_accepted_and_normalised(call, binding):
    status, body = put_policy(
        call, binding,
        allowed_signing_algs=["EdDSA", " ES256 ", "EdDSA"],
        allowed_purposes=["credit-assessment", "", "credit-assessment"],
        fetch_type="periodic", max_validity_duration=" p90d ",
        max_fetch_frequency="P1D", data_life="P1DT6H",
    )
    assert status == 200, body
    assert body["allowed_signing_algs"] == ["EdDSA", "ES256"]
    assert body["allowed_purposes"] == ["credit-assessment"]
    assert body["max_validity_duration"] == "P90D"
    assert body["fetch_type"] == "periodic"
    assert body["issues"] == []


@pytest.mark.parametrize("fields, bad", [
    ({"allowed_signing_algs": ["HS256"]}, "allowed_signing_algs"),
    ({"allowed_signing_algs": ["EdDSA", "none"]}, "allowed_signing_algs"),
    ({"allowed_signing_algs": []}, "allowed_signing_algs"),
    ({"allowed_signing_algs": ["  "]}, "allowed_signing_algs"),
    ({"fetch_type": "streaming"}, "fetch_type"),
    ({"max_validity_duration": "30 days"}, "max_validity_duration"),
    ({"max_validity_duration": "P"}, "max_validity_duration"),
    ({"max_validity_duration": "P0D"}, "max_validity_duration"),
    ({"max_fetch_frequency": "daily"}, "max_fetch_frequency"),
    ({"data_life": "P" + "1" * 40 + "D"}, "data_life"),
])
def test_invalid_policy_is_rejected(call, binding, fields, bad):
    status, body = put_policy(call, binding, **fields)
    assert status == 400, body
    assert bad in error_text(body)


def test_null_durations_mean_no_cap(call, binding):
    status, body = put_policy(call, binding, max_validity_duration="", data_life=None)
    assert status == 200, body
    assert body["max_validity_duration"] is None and body["data_life"] is None


def test_partner_status_is_validated(call, binding):
    status, body = call("PATCH", f"/consent/v1/partners/{binding}", json={"status": "deleted"})
    assert status == 400 and "status" in error_text(body), body
    status, body = call("PATCH", f"/consent/v1/partners/{binding}", json={"status": "suspended"})
    assert status == 200 and body["status"] == "suspended", body
    status, body = call("PATCH", f"/consent/v1/partners/{binding}", json={"status": "active"})
    assert status == 200 and body["status"] == "active", body


def test_decision_filter_is_validated(call):
    status, _ = call("GET", "/consent/v1/decisions", params={"decision": "maybe"})
    assert status == 400
    status, _ = call("GET", "/consent/v1/decisions", params={"decision": "deny"})
    assert status == 200


def test_stored_policy_with_now_invalid_values_still_loads(call, run, binding):
    """A version saved before these rules (written straight to the DB) loads,
    with its problems listed in ``issues`` rather than failing the page."""
    from datetime import datetime, timezone

    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import PartnerPolicy

    async def store():
        async with async_session()() as session:
            session.add(PartnerPolicy(
                partner_id=binding, version=1, status="active",
                allowed_data_scopes=["farm_details"], allowed_purposes=[],
                allowed_subject_id_types=[], allowed_signing_algs=["HS256"],
                max_validity_duration="30 days", fetch_type="streaming",
                effective_from=datetime.now(timezone.utc),
            ))
            await session.commit()

    run(store())
    status, body = call("GET", f"/consent/v1/partners/{binding}/policy")
    assert status == 200, body
    assert body["allowed_signing_algs"] == ["HS256"]
    issues = " ".join(body["issues"])
    assert "allowed_signing_algs" in issues
    assert "fetch_type" in issues
    assert "max_validity_duration" in issues

    status, body = call("GET", f"/consent/v1/partners/{binding}/policies")
    assert status == 200 and len(body[0]["issues"]) == 3, body

    # Re-saving it unchanged is rejected until the values are fixed.
    status, _ = put_policy(call, binding, allowed_signing_algs=["HS256"])
    assert status == 400
    status, body = put_policy(call, binding)
    assert status == 200 and body["version"] == 2 and body["issues"] == [], body
