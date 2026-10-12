# ruff: noqa: F401, F811  (fixtures imported from other test modules are test parameters)
"""The partner's copy of a consent (GET …/consents/{id}/record): a ZIP with the
unchanged receipt, the evidence and a CM-signed statement tying them together."""
import hashlib
import io
import json
import zipfile

import jwt
from test_assisted_consent import (
    PDF,
    PNG,
    STAFF,
    approved_consent,
    as_partner,
    bank,
    create,
    portal,
    raw,
    realm_key,
    upload,
)
from test_subject_auth import callback, esignet, keys, login, raw_html


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(raw, pc, consent_id):
    status, headers, content = raw(f"/consent/v1/partner-portal/consents/{consent_id}/record",
                                   headers=pc.headers)
    return status, headers, content


def _verify(call, jws):
    status, jwks = call("GET", "/.well-known/jwks.json")
    assert status == 200, jwks
    header = jwt.get_unverified_header(jws)
    assert header["typ"] == "consent-record+jwt"
    key = next(k for k in jwt.PyJWKSet.from_dict(jwks).keys if k.key_id == header["kid"])
    return jwt.decode(jws, key.key, algorithms=[header["alg"]])


def test_record_ties_receipt_and_evidence(call, portal, as_partner, bank, upload, raw):
    pc = as_partner(bank.audience)
    req = create(pc)
    assert upload(pc, req["id"])[0] == 201
    assert upload(pc, req["id"], data=PNG, content_type="image/png", filename="id card.png",
                  kind="other")[0] == 201
    assert pc("POST", f"/consent-requests/{req['id']}/submit")[0] == 200
    status, done = call("POST", f"{STAFF}/{req['id']}/approve", json={"note": "ok"})
    consent_id = done["consent_id"]
    _, receipt_before = pc("GET", f"/consents/{consent_id}/receipt")

    status, headers, content = _record(raw, pc, consent_id)
    assert status == 200 and headers["content-type"] == "application/zip"
    assert consent_id in headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(content))
    names = set(z.namelist())
    assert {"consent-receipt.json", "consent-record.jws", "consent-record.json", "cm-jwks.json",
            "summary.html", "README.txt"} <= names
    assert {"evidence/01-form.pdf", "evidence/02-id_card.png"} <= names

    # The receipt is the issued one, unchanged.
    receipt_bytes = z.read("consent-receipt.json")
    assert json.loads(receipt_bytes) == receipt_before
    assert pc("GET", f"/consents/{consent_id}/receipt")[1] == receipt_before

    statement = _verify(call, z.read("consent-record.jws").decode())
    assert statement == json.loads(z.read("consent-record.json"))
    assert statement["consent_id"] == consent_id and statement["partner"] == bank.audience
    assert statement["receipt"]["sha256"] == _sha(receipt_bytes)
    by_file = {e["file"]: e for e in statement["evidence"]}
    assert by_file["evidence/01-form.pdf"]["sha256"] == _sha(PDF) == _sha(z.read("evidence/01-form.pdf"))
    assert by_file["evidence/01-form.pdf"]["kind"] == "signed_form"
    assert by_file["evidence/02-id_card.png"]["sha256"] == _sha(z.read("evidence/02-id_card.png"))
    assert statement["subject_authenticated"] is False and statement["subject_authentication"] is None
    # When the CM verified, not which staff member.
    assert statement["verified_by"] == "Consent Manager" and statement["verified_at"]
    assert "dev" not in json.dumps(statement["verified_by"])
    assert statement["status"] == "active" and statement["use_case"] == "loan-profile@1"
    assert consent_id in z.read("summary.html").decode()


def test_record_with_national_id_authentication(call, portal, esignet, as_partner, bank, upload, raw,
                                                callback):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    login(pc, rid, esignet, callback)
    assert upload(pc, rid)[0] == 201
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 200
    consent_id = call("POST", f"{STAFF}/{rid}/approve", json={})[1]["consent_id"]
    status, _, content = _record(raw, pc, consent_id)
    assert status == 200
    statement = json.loads(zipfile.ZipFile(io.BytesIO(content)).read("consent-record.json"))
    assert statement["subject_authenticated"] is True
    sa = statement["subject_authentication"]
    assert sa["provider"] == "National ID (eSignet)" and sa["method"] == "OTP" and sa["authenticated_at"]


def test_record_is_the_partners_only(call, portal, as_partner, bank, upload, raw, new_partner):
    pc = as_partner(bank.audience)
    consent_id = approved_consent(call, pc, upload)
    other = as_partner(new_partner().audience)
    assert _record(raw, other, consent_id)[0] == 404
    assert _record(raw, pc, "no-such-consent")[0] == 404


def test_record_refuses_altered_evidence(call, portal, as_partner, bank, upload, raw):
    pc = as_partner(bank.audience)
    consent_id = approved_consent(call, pc, upload)
    key = next(iter(portal.store.objects))
    portal.store.objects[key] = b"%PDF-1.7 something else"
    status, _, content = _record(raw, pc, consent_id)
    assert status == 500 and b"SHA-256" in content

