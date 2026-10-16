# ruff: noqa: F401, F811  (fixtures imported from other test modules are test parameters)
"""Other identifiers of the subject on assisted requests (e.g. the Farmer ID in
Agri Stack): required when configured, confirmed by the approver in the
registry, carried into the consent and its record."""
import io
import json
import zipfile

import pytest
from test_assisted_consent import STAFF, as_partner, bank, portal, raw, realm_key, request_body, upload

FARMER = {"type": "FARMER_ID", "value": "FR-0007"}


@pytest.fixture
def farmer_id_required(portal, monkeypatch):
    monkeypatch.setattr(portal.config, "subject_required_identifiers", ["FARMER_ID"])


def _create(pc, **extra):
    return pc("POST", "/consent-requests", json=request_body(**extra))


def test_required_identifier(portal, farmer_id_required, as_partner, bank):
    pc = as_partner(bank.audience)
    status, cfg = pc("GET", "/request-settings")
    assert status == 200 and cfg["required_subject_identifiers"] == ["FARMER_ID"]
    assert cfg["subject_auth"]["enabled"] is False
    status, body = _create(pc)
    assert status == 422 and "FARMER_ID is required" in body["error"]
    status, req = _create(pc, subject_identifiers=[FARMER])
    assert status == 201, req
    assert req["subject_identifiers"] == [FARMER]
    assert pc("GET", f"/consent-requests/{req['id']}")[1]["subject_identifiers"] == [FARMER]


@pytest.mark.parametrize("ids", [
    [FARMER, {"type": "FARMER_ID", "value": "FR-0008"}],      # twice
    [{"type": "FAYDA_FAN", "value": "123"}],                  # the subject's own type
    [{"type": "FARMER_ID", "value": " "}],                     # empty
])
def test_identifier_shapes(portal, as_partner, bank, ids):
    status, _ = _create(as_partner(bank.audience), subject_identifiers=ids)
    assert status in (400, 422)


def test_approver_confirms_identifiers(call, portal, farmer_id_required, as_partner, bank, upload, raw):
    pc = as_partner(bank.audience)
    rid = _create(pc, subject_identifiers=[FARMER])[1]["id"]
    assert upload(pc, rid)[0] == 201
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 200
    status, view = call("GET", f"{STAFF}/{rid}")
    assert view["subject_identifiers"] == [FARMER]

    status, body = call("POST", f"{STAFF}/{rid}/approve", json={"note": "ok"})
    assert status == 422 and "FARMER_ID" in body["error"]
    status, done = call("POST", f"{STAFF}/{rid}/approve", json={"note": "ok", "identifiers_checked": True})
    assert status == 200, done

    status, consent = pc("GET", f"/consents/{done['consent_id']}")
    assert consent["subject_identifiers"] == [FARMER]
    checked = consent["assurance"]["subject_identifiers_checked"]
    assert checked["identifiers"] == ["FARMER_ID"] and checked["checked_by"] and checked["checked_at"]

    status, _, content = raw(f"/consent/v1/partner-portal/consents/{done['consent_id']}/record",
                             headers=pc.headers)
    z = zipfile.ZipFile(io.BytesIO(content))
    statement = json.loads(z.read("consent-record.json"))
    assert statement["subject_identifiers"] == [FARMER]
    assert "FR-0007" in z.read("summary.html").decode()


def test_request_without_identifiers_needs_no_check(call, portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    rid = _create(pc)[1]["id"]
    upload(pc, rid)
    pc("POST", f"/consent-requests/{rid}/submit")
    status, done = call("POST", f"{STAFF}/{rid}/approve", json={})
    assert status == 200, done
