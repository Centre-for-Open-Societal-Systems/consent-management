"""Consent scenario 1 (assisted, in person), Phase 1.

Partner users (partner realm tokens, verified here against an in-process key)
create requests in the partner portal API, upload the signed form (object
storage replaced by an in-memory store) and submit; staff approve or reject;
the stored consent is then presented at /validate by its ID — with
issue_receipts, receipts in the exchange format.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import jwt
import pytest
from test_grants import CSR, CSR_SCOPES, FR, FR_SCOPES, bind

ISSUER = "http://keycloak/realms/partner"
PORTAL = "/consent/v1/partner-portal"
STAFF = "/consent/v1/verifications"
PDF = b"%PDF-1.7\n" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 50
PRESENTER = "agri-composite"


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, content_type, sha256):
        self.objects[key] = data

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)


class FakeJwks:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        jwt.get_unverified_header(token)  # malformed → PyJWTError, as PyJWKClient
        return SimpleNamespace(key=self.public_key)


@pytest.fixture(scope="session")
def realm_key():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture
def portal(env, realm_key):
    """Partner realm + evidence store on for one test; restored after."""
    from openg2p_consent_manager import auth
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import EvidenceService

    config = Settings.get_config()
    names = ("partner_auth_issuer", "evidence_max_bytes", "receipt_issuer",
             "receipt_presenters", "evidence_max_files_per_request")
    saved = {n: getattr(config, n) for n in names}
    config.partner_auth_issuer = ISSUER
    auth._partner_jwk_client = FakeJwks(realm_key.public_key())
    evidence = EvidenceService.get_component()
    store = MemoryStore()
    evidence.store_override = store
    yield SimpleNamespace(config=config, store=store, evidence=evidence)
    for k, v in saved.items():
        setattr(config, k, v)
    auth._partner_jwk_client = None
    evidence.store_override = None


def token(realm_key, partner_id, roles=("PARTNER_OPERATOR",), username=None, **extra):
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "iss": ISSUER, "sub": str(uuid.uuid4()), "iat": now, "exp": now + 300,
        "preferred_username": username or f"{partner_id}-operator",
        "realm_access": {"roles": list(roles)},
    }
    if partner_id is not None:
        claims["partner_id"] = partner_id
    claims.update(extra)
    return jwt.encode(claims, realm_key, algorithm="EdDSA", headers={"kid": "k1"})


@pytest.fixture
def as_partner(call, realm_key):
    """``as_partner(audience)(method, path, json=...)`` with that partner's token."""

    def make(audience, **kw):
        headers = {"Authorization": f"Bearer {token(realm_key, audience, **kw)}"}

        def _call(method, path, json=None, params=None):
            return call(method, PORTAL + path, json=json, params=params, headers=headers)

        _call.headers = headers
        return _call

    return make


@pytest.fixture
def upload(env):
    transport = httpx.ASGITransport(app=env["app"])

    def _upload(pc, request_id, data=PDF, content_type="application/pdf",
                filename="form.pdf", kind=None):
        async def go():
            async with httpx.AsyncClient(transport=transport, base_url="http://cm") as c:
                r = await c.post(
                    f"{PORTAL}/consent-requests/{request_id}/evidence",
                    files={"file": (filename, data, content_type)},
                    data={"kind": kind} if kind else None, headers=pc.headers,
                )
                try:
                    return r.status_code, r.json()
                except ValueError:
                    return r.status_code, r.text

        return env["loop"].run_until_complete(go())

    return _upload


@pytest.fixture
def raw(env):
    """(status, headers, bytes) for a GET — for file downloads."""
    transport = httpx.ASGITransport(app=env["app"])

    def _get(path, headers=None):
        async def go():
            async with httpx.AsyncClient(transport=transport, base_url="http://cm") as c:
                r = await c.get(path, headers=headers)
                return r.status_code, r.headers, r.content

        return env["loop"].run_until_complete(go())

    return _get


@pytest.fixture
def bank(call, new_partner):
    """A partner bound to FR (personal details only) and CSR."""
    p = new_partner()
    p.fr_binding = bind(call, p, FR, ["farmer_personal_details"])
    p.csr_binding = bind(call, p, CSR, CSR_SCOPES)
    return p


def request_body(**extra):
    body = {
        "subject_id": {"type": "FAYDA_FAN", "value": "123456789012"},
        "purpose": {"code": "credit-assessment"},
        "grants": [
            {"data_controller": FR, "data_scopes": ["farmer_personal_details"]},
            {"data_controller": CSR, "data_scopes": ["crop_season", "measures"]},
        ],
        "use_case": "loan-profile@1",
    }
    body.update(extra)
    return body


def create(pc, **extra):
    status, body = pc("POST", "/consent-requests", json=request_body(**extra))
    assert status == 201, body
    return body


def submitted(pc, upload, **extra):
    req = create(pc, **extra)
    status, body = upload(pc, req["id"])
    assert status == 201, body
    status, body = pc("POST", f"/consent-requests/{req['id']}/submit")
    assert status == 200, body
    return body


def approved_consent(call, pc, upload, **extra):
    req = submitted(pc, upload, **extra)
    status, body = call("POST", f"{STAFF}/{req['id']}/approve", json={"note": "form ok"})
    assert status == 200, body
    return body["consent_id"]


def validate(call, **payload):
    status, body = call("POST", "/consent/v1/validate", json=payload)
    assert status == 200, body
    return body


# ── partner realm auth ────────────────────────────────────────────────────────


def test_portal_disabled_without_partner_realm(call, realm_key, bank):
    headers = {"Authorization": f"Bearer {token(realm_key, bank.audience)}"}
    assert call("GET", f"{PORTAL}/me", headers=headers)[0] == 404
    assert call("POST", f"{PORTAL}/consent-requests", json={}, headers=headers)[0] == 404


def test_partner_token_checks(call, portal, realm_key, bank):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    def me(tok):
        return call("GET", f"{PORTAL}/me", headers={"Authorization": f"Bearer {tok}"})[0]

    assert call("GET", f"{PORTAL}/me")[0] == 401
    assert me("not-a-jwt") == 401
    other_key = ed25519.Ed25519PrivateKey.generate()
    assert me(token(other_key, bank.audience)) == 401  # wrong signature
    assert me(token(realm_key, bank.audience, iss="http://keycloak/realms/staff")) == 401
    assert me(token(realm_key, bank.audience, exp=1)) == 401
    assert me(token(realm_key, None)) == 403  # no partner_id
    assert me(token(realm_key, bank.audience, roles=["SOMETHING"])) == 403
    assert me(token(realm_key, bank.audience, roles=["PARTNER_ADMIN"])) == 200


def test_me_and_bindings(portal, as_partner, bank):
    pc = as_partner(bank.audience, username="op1", name="Op One")
    status, me = pc("GET", "/me")
    assert status == 200
    assert me == {"username": "op1", "name": "Op One", "partner_id": bank.audience,
                  "roles": ["PARTNER_OPERATOR"]}
    status, bindings = pc("GET", "/bindings")
    assert status == 200
    by = {b["data_controller"]: b for b in bindings}
    assert set(by) == {FR, CSR}
    assert by[FR]["allowed_data_scopes"] == ["farmer_personal_details"]
    assert by[CSR]["allowed_purposes"] == ["credit-assessment"]
    assert by[CSR]["allowed_subject_id_types"] == ["FAYDA_FAN"]


# ── requests ─────────────────────────────────────────────────────────────────


def test_create_request(portal, as_partner, bank):
    pc = as_partner(bank.audience, username="op1")
    req = create(pc)
    assert req["status"] == "pending" and req["method"] == "assisted"
    assert req["use_case"] == "loan-profile@1" and req["created_by"] == "op1"
    assert req["subject_id"] == {"type": "FAYDA_FAN", "value": "123456789012"}
    assert req["grants"] == request_body()["grants"]
    assert req["consent_id"] is None and req["submitted_at"] is None

    status, detail = pc("GET", f"/consent-requests/{req['id']}")
    assert status == 200 and detail["evidence"] == []
    status, listing = pc("GET", "/consent-requests", params={"status": "pending"})
    assert status == 200 and listing["total"] == 1
    assert [i["id"] for i in listing["items"]] == [req["id"]]


@pytest.mark.parametrize("change, fragment", [
    ({"grants": [{"data_controller": FR, "data_scopes": FR_SCOPES}]}, "scope_exceeds_policy"),
    ({"grants": [{"data_controller": "nsr", "data_scopes": ["x"]}]}, "not bound"),
    ({"purpose": {"code": "marketing"}}, "purpose_not_allowed"),
    ({"subject_id": {"type": "PHONE", "value": "1"}}, "subject_not_allowed"),
    ({"validity": {"valid_until": (datetime.now(timezone.utc) + timedelta(days=800)).isoformat()}},
     "validity_exceeds_policy"),
])
def test_create_request_policy_checks(portal, as_partner, bank, change, fragment):
    pc = as_partner(bank.audience)
    status, body = pc("POST", "/consent-requests", json=request_body(**change))
    assert status == 422, body
    assert fragment in body["error"]


def test_create_request_needs_a_policy(call, portal, as_partner, new_partner):
    p = new_partner()
    status, _ = call("POST", "/consent/v1/partners", json={
        "audience": p.audience, "controller_id": FR, "partner_mgmt_id": p.pm_id})
    assert status == 201
    status, body = as_partner(p.audience)("POST", "/consent-requests", json=request_body(
        grants=[{"data_controller": FR, "data_scopes": ["farmer_personal_details"]}]))
    assert status == 422 and "no active policy" in body["error"]


def test_other_partner_sees_nothing(portal, as_partner, bank, new_partner, upload, call):
    pc = as_partner(bank.audience)
    req = create(pc)
    status, ev = upload(pc, req["id"])
    assert status == 201
    other = as_partner(new_partner().audience)
    rid = req["id"]
    assert other("GET", f"/consent-requests/{rid}")[0] == 404
    assert other("GET", f"/consent-requests/{rid}/evidence/{ev['id']}")[0] == 404
    assert other("DELETE", f"/consent-requests/{rid}/evidence/{ev['id']}")[0] == 404
    assert upload(other, rid)[0] == 404
    assert other("POST", f"/consent-requests/{rid}/submit")[0] == 404
    assert other("POST", f"/consent-requests/{rid}/cancel")[0] == 404
    assert other("GET", "/consent-requests")[1]["total"] == 0

    consent_id = approved_consent(call, pc, upload)
    assert other("GET", f"/consents/{consent_id}")[0] == 404
    assert other("GET", f"/consents/{consent_id}/receipt")[0] == 404
    assert other("GET", "/consents")[1]["total"] == 0


# ── evidence ─────────────────────────────────────────────────────────────────


def test_evidence_upload_download_delete(portal, as_partner, bank, upload, raw):
    pc = as_partner(bank.audience, username="op1")
    req = create(pc)
    status, ev = upload(pc, req["id"], filename="../../signed form.pdf")
    assert status == 201, ev
    assert ev["kind"] == "signed_form" and ev["filename"] == "signed form.pdf"
    assert ev["content_type"] == "application/pdf" and ev["size_bytes"] == len(PDF)
    assert ev["sha256"] == hashlib.sha256(PDF).hexdigest() and ev["uploaded_by"] == "op1"
    assert len(portal.store.objects) == 1

    status, headers, content = raw(
        f"{PORTAL}/consent-requests/{req['id']}/evidence/{ev['id']}", pc.headers
    )
    assert status == 200 and content == PDF
    assert headers["content-type"] == "application/pdf"
    assert headers["content-disposition"].startswith("attachment;")

    status, png = upload(pc, req["id"], data=PNG, content_type="image/png",
                         filename="photo.png", kind="other")
    assert status == 201 and png["kind"] == "other"
    detail = pc("GET", f"/consent-requests/{req['id']}")[1]
    assert [e["id"] for e in detail["evidence"]] == [ev["id"], png["id"]]

    assert pc("DELETE", f"/consent-requests/{req['id']}/evidence/{png['id']}")[0] == 204
    assert len(portal.store.objects) == 1
    assert pc("DELETE", f"/consent-requests/{req['id']}/evidence/{png['id']}")[0] == 404


def test_evidence_type_size_and_kind(portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    assert upload(pc, rid, data=b"hello", content_type="text/plain")[0] == 415
    # Declared PDF, but not a PDF.
    assert upload(pc, rid, data=b"MZ\x90\x00binary", content_type="application/pdf")[0] == 415
    assert upload(pc, rid, data=b"", content_type="application/pdf")[0] == 422
    assert upload(pc, rid, kind="selfie")[0] == 422
    portal.config.evidence_max_bytes = 100
    status, body = upload(pc, rid)
    assert status == 413, body
    portal.config.evidence_max_bytes = 10 * 1024 * 1024
    portal.config.evidence_max_files_per_request = 1
    assert upload(pc, rid)[0] == 201
    assert upload(pc, rid)[0] == 409
    assert len(portal.store.objects) == 1  # the refused upload's object is removed


def test_evidence_storage_not_configured(portal, as_partner, bank, upload):
    portal.evidence.store_override = None  # and no S3 endpoint configured
    pc = as_partner(bank.audience)
    status, body = upload(pc, create(pc)["id"])
    assert status == 503 and "not configured" in body["error"]


# ── submit / cancel ──────────────────────────────────────────────────────────


def test_submit_needs_a_signed_form(portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    status, body = pc("POST", f"/consent-requests/{rid}/submit")
    assert status == 409 and "signed" in body["error"]
    assert upload(pc, rid, data=PNG, content_type="image/png", kind="other")[0] == 201
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 409
    status, ev = upload(pc, rid)
    assert status == 201
    status, req = pc("POST", f"/consent-requests/{rid}/submit")
    assert status == 200 and req["status"] == "pending_verification"
    assert req["submitted_at"]
    # Evidence is frozen once submitted.
    assert upload(pc, rid)[0] == 409
    assert pc("DELETE", f"/consent-requests/{rid}/evidence/{ev['id']}")[0] == 409
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 409


def test_cancel(portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    status, req = pc("POST", f"/consent-requests/{rid}/cancel")
    assert status == 200 and req["status"] == "cancelled"
    assert pc("POST", f"/consent-requests/{rid}/cancel")[0] == 409
    sub = submitted(pc, upload)
    status, req = pc("POST", f"/consent-requests/{sub['id']}/cancel")
    assert status == 200 and req["status"] == "cancelled"


# ── staff verification ──────────────────────────────────────────────────────


def test_staff_approve_creates_consent(call, portal, as_partner, bank, upload, raw):
    pc = as_partner(bank.audience, username="op1")
    req = submitted(pc, upload)
    rid = req["id"]

    status, listing = call("GET", STAFF, params={"status": "pending_verification"})
    assert status == 200
    item = next(i for i in listing["items"] if i["id"] == rid)
    assert item["partner_audience"] == bank.audience
    status, detail = call("GET", f"{STAFF}/{rid}")
    assert status == 200 and len(detail["evidence"]) == 1
    ev = detail["evidence"][0]
    status, _, content = raw(f"{STAFF}/{rid}/evidence/{ev['id']}")
    assert status == 200 and content == PDF

    status, done = call("POST", f"{STAFF}/{rid}/approve", json={"note": "form ok"})
    assert status == 200, done
    assert done["status"] == "approved" and done["verified_by"] == "dev"
    assert done["verification_note"] == "form ok" and done["verified_at"]
    consent_id = done["consent_id"]
    assert consent_id
    assert call("POST", f"{STAFF}/{rid}/approve", json={})[0] == 409
    assert call("POST", f"{STAFF}/{rid}/reject", json={"note": "x"})[0] == 409

    # The partner sees it.
    status, mine = pc("GET", f"/consent-requests/{rid}")
    assert mine["status"] == "approved" and mine["consent_id"] == consent_id
    status, consent = pc("GET", f"/consents/{consent_id}")
    assert status == 200, consent
    assert consent["status"] == "active" and consent["use_case"] == "loan-profile@1"
    assert consent["consent_request_id"] == rid
    assurance = consent["assurance"]
    assert assurance["method"] == "assisted" and assurance["evidence"] == ["signed_form"]
    assert assurance["verified_by"] == "dev" and assurance["subject_authenticated"] is False
    grants = {g["data_controller"]: g for g in consent["grants"]}
    assert grants[FR]["effective_data_scopes"] == ["farmer_personal_details"]
    assert grants[CSR]["effective_data_scopes"] == ["crop_season", "measures"]
    status, receipt = pc("GET", f"/consents/{consent_id}/receipt")
    assert status == 200 and receipt["consent_artefact"]["@id"] == consent_id
    status, listing = pc("GET", "/consents", params={"status": "active"})
    assert [c["consent_id"] for c in listing["items"]] == [consent_id]
    assert pc("GET", "/consents", params={"status": "revoked"})[1]["total"] == 0


def test_staff_approve_narrows_to_current_policy(call, portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    req = submitted(pc, upload)
    # CSR policy narrowed after the request was made.
    status, _ = call("PUT", f"/consent/v1/partners/{bank.csr_binding}/policy", json={
        "allowed_data_scopes": ["crop_season"], "allowed_purposes": ["credit-assessment"],
        "allowed_subject_id_types": ["FAYDA_FAN"], "allowed_signing_algs": ["EdDSA"],
        "max_validity_duration": "P1Y",
    })
    assert status == 200
    status, done = call("POST", f"{STAFF}/{req['id']}/approve", json={})
    assert status == 200, done
    consent = pc("GET", f"/consents/{done['consent_id']}")[1]
    grants = {g["data_controller"]: g["effective_data_scopes"] for g in consent["grants"]}
    assert grants == {FR: ["farmer_personal_details"], CSR: ["crop_season"]}


def test_staff_reject(call, portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    draft = create(pc)
    assert call("POST", f"{STAFF}/{draft['id']}/reject", json={"note": "x"})[0] == 409
    assert call("POST", f"{STAFF}/{draft['id']}/approve", json={})[0] == 409
    req = submitted(pc, upload)
    # A reason is required (request validation: 400 in openg2p-fastapi-common).
    assert call("POST", f"{STAFF}/{req['id']}/reject", json={})[0] == 400
    assert call("POST", f"{STAFF}/{req['id']}/reject", json={"note": "  "})[0] == 400
    status, done = call("POST", f"{STAFF}/{req['id']}/reject",
                        json={"note": "signature does not match"})
    assert status == 200 and done["status"] == "rejected"
    assert done["verification_note"] == "signature does not match" and done["consent_id"] is None
    assert call("GET", f"{STAFF}/{uuid.uuid4()}")[0] == 404
    # Drafts are not in the staff queue.
    ids = [i["id"] for i in call("GET", STAFF, params={"limit": 200})[1]["items"]]
    assert req["id"] in ids and draft["id"] not in ids


# ── /validate with consent_id ────────────────────────────────────────────────


def test_validate_consent_id_permit(call, portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    consent_id = approved_consent(call, pc, upload)
    body = validate(call, consent_id=consent_id, consent_partner_id=bank.audience,
                    request_context={"subject_id": {"type": "FAYDA_FAN",
                                                    "value": "123456789012"}})
    assert body["decision"] == "permit", body
    assert body["consent_id"] == consent_id and body["receipt_id"]
    assert body["grants"] == {FR: ["farmer_personal_details"], CSR: ["crop_season", "measures"]}
    assert body["subject_id"] == {"type": "FAYDA_FAN", "value": "123456789012"}
    assert body["receipts"] is None

    one = validate(call, consent_id=consent_id, consent_partner_id=bank.audience,
                   data_controller=CSR)
    assert one["data_controller"] == CSR and one["grants"] == {CSR: ["crop_season", "measures"]}
    assert one["effective_data_scopes"] == ["crop_season", "measures"]
    deny = validate(call, consent_id=consent_id, consent_partner_id=bank.audience,
                    data_controller="nsr")
    assert deny["reason_code"] == "controller_not_granted"


def test_validate_needs_exactly_one_consent(call):
    # A malformed request is a clean 200 deny (the partner api's contract).
    for body in ({}, {"consent_jws": "a.b.c", "consent_id": "x"}):
        status, decision = call("POST", "/consent/v1/validate", json=body)
        assert status == 200, decision
        assert decision["decision"] == "deny" and decision["reason_code"] == "malformed_object"
        assert "exactly one" in decision["detail"]


def test_validate_consent_id_denials(call, portal, as_partner, bank, upload, run, new_partner):
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentArtefact

    pc = as_partner(bank.audience)
    assert validate(call, consent_id=str(uuid.uuid4()),
                    consent_partner_id=bank.audience)["reason_code"] == "unknown_consent"

    consent_id = approved_consent(call, pc, upload)
    ok = {"consent_id": consent_id, "consent_partner_id": bank.audience}
    assert validate(call, consent_id=consent_id,
                    consent_partner_id=new_partner().audience)["reason_code"] == "partner_mismatch"
    assert validate(call, consent_id=consent_id)["reason_code"] == "partner_mismatch"
    assert validate(call, **ok, request_context={
        "subject_id": {"type": "FAYDA_FAN", "value": "999"}})["reason_code"] == "subject_mismatch"
    # Another id type is for the registry to resolve.
    assert validate(call, **ok, request_context={
        "subject_id": {"type": "FARMER_ID", "value": "F1"}})["decision"] == "permit"

    async def shift(**values):
        async with async_session()() as session:
            artefact = await session.get(ConsentArtefact, consent_id)
            for k, v in values.items():
                setattr(artefact, k, v)
            await session.commit()

    now = datetime.now(timezone.utc)
    run(shift(valid_from=now + timedelta(days=1)))
    assert validate(call, **ok)["reason_code"] == "not_yet_valid"
    run(shift(valid_from=now - timedelta(days=2), valid_until=now - timedelta(days=1)))
    assert validate(call, **ok)["reason_code"] == "expired"
    run(shift(valid_until=now + timedelta(days=30)))
    assert validate(call, **ok)["decision"] == "permit"

    assert call("POST", f"/consent/v1/consents/{consent_id}/revoke", json={})[0] == 200
    deny = validate(call, **ok)
    assert deny["decision"] == "deny" and deny["reason_code"] == "revoked"
    assert pc("GET", "/consents", params={"status": "revoked"})[1]["total"] == 1

    # A partner-signed (embedded) consent is not presentable by ID.
    jws = bank.sign(bank.claims(grants=[{"data_controller": CSR, "data_scopes": CSR_SCOPES}]))
    embedded = validate(call, consent_jws=jws, data_controller=CSR)
    assert embedded["decision"] == "permit", embedded
    assert validate(call, consent_id=embedded["consent_id"],
                    consent_partner_id=bank.audience)["reason_code"] == "unknown_consent"


def test_validate_consent_id_scopes(call, portal, as_partner, bank, upload):
    pc = as_partner(bank.audience)
    consent_id = approved_consent(call, pc, upload)
    ok = {"consent_id": consent_id, "consent_partner_id": bank.audience}

    body = validate(call, **ok, request_context={"requested_scopes": ["measures", "x"]})
    assert body["grants"] == {CSR: ["measures"]}
    assert validate(call, **ok, request_context={"requested_scopes": ["x"]})[
        "reason_code"] == "scope_exceeds_policy"

    # The partner's CURRENT policy narrows a stored consent.
    status, _ = call("PUT", f"/consent/v1/partners/{bank.csr_binding}/policy", json={
        "allowed_data_scopes": ["farmer_reference"], "allowed_purposes": ["credit-assessment"],
        "allowed_subject_id_types": ["FAYDA_FAN"], "allowed_signing_algs": ["EdDSA"],
    })
    assert status == 200
    body = validate(call, **ok)
    assert body["grants"] == {FR: ["farmer_personal_details"]}
    assert body["data_controller"] == FR


def _claims(receipt):
    from openg2p_consent_manager.utils.canonical import b64url_decode

    header, payload, _ = receipt.split(".")
    return json.loads(b64url_decode(header)), json.loads(b64url_decode(payload))


def test_validate_consent_id_issues_receipts(call, portal, as_partner, bank, upload):
    portal.config.receipt_issuer = "agri-stack-exchange-cm"
    portal.config.receipt_presenters = [PRESENTER]
    pc = as_partner(bank.audience)
    consent_id = approved_consent(call, pc, upload)
    consent = pc("GET", f"/consents/{consent_id}")[1]

    body = validate(call, consent_id=consent_id, consent_partner_id=bank.audience,
                    partner_id=PRESENTER, issue_receipts=True,
                    request_context={"subject_id": {"type": "FAYDA_FAN",
                                                    "value": "123456789012"},
                                     "requested_scopes": ["farmer_personal_details",
                                                          "crop_season"]})
    assert body["decision"] == "permit", body
    assert set(body["receipts"]) == {FR, CSR}
    assert body["grants"] == {FR: ["farmer_personal_details"], CSR: ["crop_season"]}
    header, claims = _claims(body["receipts"][CSR])
    assert header["typ"] == "consent-receipt+jwt"
    assert claims["iss"] == "agri-stack-exchange-cm" and claims["aud"] == CSR
    assert claims["partner"] == bank.audience and claims["presenter"] == PRESENTER
    assert claims["consent_id"] == consent_id and claims["scopes"] == ["crop_season"]
    assert claims["purpose"] == "credit-assessment"
    assert datetime.fromisoformat(claims["consent_issued_at"]) == datetime.fromisoformat(
        consent["created_at"])
    status, st = call("GET", f"/consent/v1/receipts/{claims['jti']}/status")
    assert status == 200 and st["status"] == "active"

    deny = validate(call, consent_id=consent_id, consent_partner_id=bank.audience,
                    partner_id="someone-else", issue_receipts=True)
    assert deny["reason_code"] == "receipt_presenter_not_allowed"

    call("POST", f"/consent/v1/consents/{consent_id}/revoke", json={})
    status, st = call("GET", f"/consent/v1/receipts/{claims['jti']}/status")
    assert st["status"] == "revoked"


def test_issue_receipts_from_jws_returns_grants(call, portal, bank):
    portal.config.receipt_issuer = "agri-stack-exchange-cm"
    portal.config.receipt_presenters = [PRESENTER]
    jws = bank.sign(bank.claims(grants=[
        {"data_controller": FR, "data_scopes": FR_SCOPES},
        {"data_controller": CSR, "data_scopes": CSR_SCOPES},
    ]))
    body = validate(call, consent_jws=jws, partner_id=PRESENTER, issue_receipts=True)
    assert body["decision"] == "permit", body
    assert body["grants"] == {FR: ["farmer_personal_details"], CSR: sorted(CSR_SCOPES)}


# ── migration + S3 store ─────────────────────────────────────────────────────


def test_migration_added_columns(run):
    from openg2p_fastapi_common.context import dbengine
    from sqlalchemy import text

    async def columns(table):
        async with dbengine.get().connect() as conn:
            rows = await conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
            ), {"t": table})
            return {r[0] for r in rows}

    assert {"method", "use_case", "created_by", "partner_audience", "submitted_at",
            "verified_by", "verified_at", "verification_note"} <= run(columns("consent_requests"))
    assert {"assurance", "consent_request_id"} <= run(columns("consent_artefacts"))
    assert {"sha256", "storage_key", "consent_request_id"} <= run(columns("consent_evidence"))


def test_s3_store_creates_bucket_and_round_trips(env):
    moto = pytest.importorskip("moto")
    import boto3

    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services.evidence_service import S3EvidenceStore

    config = Settings.get_config()
    saved = (config.evidence_s3_endpoint, config.evidence_s3_region)
    config.evidence_s3_endpoint, config.evidence_s3_region = None, "us-east-1"
    try:
        with moto.mock_aws():
            store = S3EvidenceStore()
            store.put("consent-requests/r1/e1", PDF, "application/pdf", "abc")
            assert store.get("consent-requests/r1/e1") == PDF
            head = boto3.client("s3", region_name="us-east-1").head_object(
                Bucket=config.evidence_s3_bucket, Key="consent-requests/r1/e1")
            assert head["Metadata"] == {"sha256": "abc"}
            store.delete("consent-requests/r1/e1")
            with pytest.raises(Exception):
                store.get("consent-requests/r1/e1")
    finally:
        config.evidence_s3_endpoint, config.evidence_s3_region = saved
