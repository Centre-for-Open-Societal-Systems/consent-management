"""Single consent, one grant per registry (G2P-5719).

Covers /validate with `grants` and with legacy single-controller consents,
per-(partner, controller) policies in Partner Management, replay per (jti,
controller), subject mismatch, and originated consents with grants.
"""
import jwt
import pytest

FR = "farmer-registry"
CSR = "crop-sown-registry"
FR_SCOPES = ["farmer_personal_details", "farm_details"]
CSR_SCOPES = ["farmer_reference", "crop_season", "measures"]


def bind(pm, partner, controller, scopes, **policy):
    """Give ``partner`` an active scope policy for ``controller`` in (the fake)
    Partner Management. Returns its version."""
    return pm.grant(partner.audience, controller, scopes, **policy)


def validate(call, jws, data_controller=None, **ctx):
    payload = {"consent_jws": jws}
    if data_controller is not None:
        payload["data_controller"] = data_controller
    if ctx:
        payload["request_context"] = ctx
    status, body = call("POST", "/consent/v1/validate", json=payload)
    assert status == 200, body
    return body


GRANTS = [
    {"data_controller": FR, "data_scopes": FR_SCOPES},
    {"data_controller": CSR, "data_scopes": CSR_SCOPES},
]


@pytest.fixture
def two_registry_partner(pm, new_partner):
    """A partner with a policy for FR and one for CSR in Partner Management. The
    FR policy allows only personal details (not farm details)."""
    p = new_partner()
    bind(pm, p, FR, ["farmer_personal_details"])
    bind(pm, p, CSR, CSR_SCOPES)
    return p


# ── grants consent ────────────────────────────────────────────────────────────


def test_grants_consent_validated_for_two_controllers(call, two_registry_partner):
    p = two_registry_partner
    jws = p.sign(p.claims(grants=GRANTS))

    fr = validate(call, jws, FR)
    assert fr["decision"] == "permit", fr
    assert fr["data_controller"] == FR
    # grant ∩ FR policy: farm_details is granted but not in FR's policy.
    assert fr["effective_data_scopes"] == ["farmer_personal_details"]
    assert fr["subject_id"] == {"type": "FAYDA_FAN", "value": "123456789012"}
    assert fr["policy_version"] == 1

    csr = validate(call, jws, CSR, requested_scopes=["crop_season", "measures", "x"])
    assert csr["decision"] == "permit", csr
    assert csr["data_controller"] == CSR
    # grant ∩ CSR policy ∩ requested
    assert csr["effective_data_scopes"] == ["crop_season", "measures"]

    # Two decisions, two artefacts, two receipts.
    assert fr["consent_id"] != csr["consent_id"]
    assert fr["receipt_id"] and csr["receipt_id"] and fr["receipt_id"] != csr["receipt_id"]
    for dec, ctl in ((fr, FR), (csr, CSR)):
        status, receipt = call("GET", f"/consent/v1/receipts/{dec['receipt_id']}")
        assert status == 200
        assert receipt["data_controller"] == {"id": ctl}
        assert receipt["data_controllers"] == [{"id": ctl}]


def test_grants_consent_without_data_controller_is_denied(call, two_registry_partner):
    p = two_registry_partner
    dec = validate(call, p.sign(p.claims(grants=GRANTS)))
    assert dec["decision"] == "deny"
    assert dec["reason_code"] == "malformed_object"
    assert "data_controller is required" in dec["detail"]


def test_controller_not_granted(call, two_registry_partner):
    p = two_registry_partner
    jws = p.sign(p.claims(grants=[{"data_controller": FR, "data_scopes": FR_SCOPES}]))
    dec = validate(call, jws, CSR)
    assert dec["decision"] == "deny"
    assert dec["reason_code"] == "controller_not_granted"
    assert dec["data_controller"] == CSR


def test_grant_for_controller_without_policy_is_unknown_partner(call, pm, new_partner):
    """PM answers 404 for a controller the partner has no policy for."""
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    jws = p.sign(p.claims(grants=GRANTS))
    assert validate(call, jws, FR)["decision"] == "permit"
    dec = validate(call, jws, CSR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "unknown_partner"


def test_grants_and_legacy_fields_together_is_malformed(call, two_registry_partner):
    p = two_registry_partner
    jws = p.sign(p.claims(grants=GRANTS, data_controller=FR, data_scopes=FR_SCOPES))
    dec = validate(call, jws, FR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "malformed_object"


def test_duplicate_controller_in_grants_is_malformed(call, two_registry_partner):
    p = two_registry_partner
    jws = p.sign(p.claims(grants=[GRANTS[0], GRANTS[0]]))
    dec = validate(call, jws, FR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "malformed_object"


def test_tampered_grants_fail_signature(call, two_registry_partner):
    import base64
    import json

    p = two_registry_partner
    claims = p.claims(grants=[{"data_controller": FR, "data_scopes": ["farmer_personal_details"]}])
    header, _, sig = p.sign(claims).split(".")
    claims["grants"] = GRANTS
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    dec = validate(call, f"{header}.{payload}.{sig}", CSR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "signature_invalid"


def test_known_jti_with_forged_signature_gets_no_stored_permit(call, two_registry_partner):
    """A stored decision is returned only after the signature is verified: an
    altered object reusing a jti that already has a permit is denied."""
    import base64
    import json

    p = two_registry_partner
    claims = p.claims(grants=GRANTS)
    jws = p.sign(claims)
    assert validate(call, jws, CSR)["decision"] == "permit"

    header, _, sig = jws.split(".")
    forged = dict(claims, subject_id={"type": "FAYDA_FAN", "value": "999999999999"})
    payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    dec = validate(call, f"{header}.{payload}.{sig}", CSR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "signature_invalid"


def test_known_jti_reused_for_a_different_consent_is_denied(call, two_registry_partner):
    """The partner signs a new consent but reuses a jti: it is not the stored consent."""
    p = two_registry_partner
    claims = p.claims(grants=GRANTS)
    assert validate(call, p.sign(claims), CSR)["decision"] == "permit"

    reused = dict(claims, subject_id={"type": "FAYDA_FAN", "value": "999999999999"})
    dec = validate(call, p.sign(reused), CSR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "replay"


# ── legacy consent ────────────────────────────────────────────────────────────


def test_legacy_consent_still_permits(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    claims = p.claims(data_controller=FR, data_scopes=FR_SCOPES)
    dec = validate(call, p.sign(claims))  # no data_controller in the request
    assert dec["decision"] == "permit", dec
    assert dec["data_controller"] == FR
    assert dec["effective_data_scopes"] == sorted(FR_SCOPES)

    # Naming the matching controller is fine too (fresh jti).
    claims = p.claims(data_controller=FR, data_scopes=FR_SCOPES)
    assert validate(call, p.sign(claims), FR)["decision"] == "permit"


def test_legacy_consent_with_mismatched_data_controller(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    bind(pm, p, CSR, CSR_SCOPES)
    jws = p.sign(p.claims(data_controller=FR, data_scopes=FR_SCOPES))
    dec = validate(call, jws, CSR)
    assert dec["decision"] == "deny"
    assert dec["reason_code"] == "controller_not_granted"


# ── replay / idempotency per (jti, controller) ───────────────────────────────


def test_replay_is_per_controller(call, run, two_registry_partner):
    from sqlalchemy import func, select

    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import (
        ConsentArtefact,
        ConsentReceipt,
        DecisionLog,
    )

    p = two_registry_partner
    claims = p.claims(grants=GRANTS)
    jws = p.sign(claims)

    fr1 = validate(call, jws, FR)
    fr2 = validate(call, jws, FR)  # same controller → stored decision
    csr1 = validate(call, jws, CSR)  # other controller → its own decision
    csr2 = validate(call, jws, CSR)

    assert fr1["decision"] == fr2["decision"] == csr1["decision"] == csr2["decision"] == "permit"
    assert fr1["consent_id"] == fr2["consent_id"]
    assert csr1["consent_id"] == csr2["consent_id"]
    assert fr1["consent_id"] != csr1["consent_id"]
    assert fr2["data_controller"] == FR and csr2["data_controller"] == CSR
    assert fr2["effective_data_scopes"] == fr1["effective_data_scopes"]

    async def counts():
        async with async_session()() as s:
            arts = (await s.execute(
                select(ConsentArtefact).where(ConsentArtefact.object_jti == claims["jti"])
            )).scalars().all()
            receipts = (await s.execute(
                select(func.count(ConsentReceipt.id)).where(
                    ConsentReceipt.consent_id.in_([a.id for a in arts])
                )
            )).scalar()
            permits = (await s.execute(
                select(DecisionLog.data_controller).where(
                    DecisionLog.object_jti == claims["jti"], DecisionLog.decision == "permit"
                )
            )).scalars().all()
            return sorted(a.controller_id for a in arts), receipts, sorted(permits)

    controllers, receipts, permit_logs = run(counts())
    assert controllers == [CSR, FR]
    assert receipts == 2
    assert permit_logs == [CSR, FR]


def test_revoking_one_controllers_artefact_leaves_the_other(call, two_registry_partner):
    p = two_registry_partner
    jws = p.sign(p.claims(grants=GRANTS))
    fr = validate(call, jws, FR)
    validate(call, jws, CSR)
    status, _ = call("POST", f"/consent/v1/consents/{fr['consent_id']}/revoke", json={})
    assert status == 200
    assert validate(call, jws, FR)["reason_code"] == "revoked"
    assert validate(call, jws, CSR)["decision"] == "permit"


# ── subject ───────────────────────────────────────────────────────────────────


def test_subject_mismatch(call, two_registry_partner):
    p = two_registry_partner
    jws = p.sign(p.claims(grants=GRANTS))

    dec = validate(call, jws, FR, subject_id={"type": "FAYDA_FAN", "value": "999999999999"})
    assert dec["decision"] == "deny" and dec["reason_code"] == "subject_mismatch"

    # A different id type is for the registry to resolve — not a CM deny.
    dec = validate(call, jws, FR, subject_id={"type": "FARMER_ID", "value": "FR-0007"})
    assert dec["decision"] == "permit", dec

    # Same subject passes; a mismatch is still caught on the stored decision.
    dec = validate(call, jws, FR, subject_id={"type": "FAYDA_FAN", "value": "123456789012"})
    assert dec["decision"] == "permit"
    dec = validate(call, jws, FR, subject_id={"type": "FAYDA_FAN", "value": "000000000000"})
    assert dec["decision"] == "deny" and dec["reason_code"] == "subject_mismatch"


# ── policies: one partner, several controllers ───────────────────────────────


def test_partner_with_two_controller_policies(call, pm, two_registry_partner):
    p = two_registry_partner
    jws = p.sign(p.claims(grants=GRANTS))
    fr = validate(call, jws, FR)
    assert fr["effective_data_scopes"] == ["farmer_personal_details"]
    assert fr["policy_version"] == 1

    # A new FR policy version in PM changes FR only.
    assert bind(pm, p, FR, FR_SCOPES) == 2
    jws = p.sign(p.claims(grants=GRANTS))
    fr = validate(call, jws, FR)
    assert fr["effective_data_scopes"] == sorted(FR_SCOPES)
    assert fr["policy_version"] == 2
    assert validate(call, jws, CSR)["effective_data_scopes"] == sorted(CSR_SCOPES)

    # Revoking the CSR policy in PM denies CSR only.
    pm.revoke(p.audience, CSR)
    jws = p.sign(p.claims(grants=GRANTS))
    assert validate(call, jws, CSR)["reason_code"] == "unknown_partner"
    assert validate(call, jws, FR)["decision"] == "permit"


def test_records_store_audience_and_policy_version(call, run, two_registry_partner):
    from sqlalchemy import select

    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentArtefact, DecisionLog

    p = two_registry_partner
    claims = p.claims(grants=GRANTS)
    dec = validate(call, p.sign(claims), FR)
    assert dec["decision"] == "permit"

    async def rows():
        async with async_session()() as s:
            art = await s.get(ConsentArtefact, dec["consent_id"])
            log = (await s.execute(
                select(DecisionLog).where(DecisionLog.object_jti == claims["jti"])
            )).scalars().first()
            return art, log

    art, log = run(rows())
    assert art.partner_id == p.audience and art.policy_version == 1
    assert log.partner_id == p.audience and log.policy_version == 1
    status, receipt = call("GET", f"/consent/v1/receipts/{dec['receipt_id']}")
    assert status == 200 and receipt["third_parties"] == [p.audience]


def test_migration_is_idempotent(env, run):
    from unittest import mock

    init = env["init"]
    with mock.patch("asyncio.run", run):
        init.migrate_database(None)
        init.migrate_database(None)


# ── originated consent with grants ────────────────────────────────────────────


def test_originated_consent_with_grants(call, two_registry_partner):
    p = two_registry_partner
    status, req = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": {"type": "FAYDA_FAN", "value": "123456789012"},
        "partner_id": p.audience,
        "purpose": {"code": "credit-assessment"},
        "grants": [
            {"data_controller": FR, "data_scopes": ["farmer_personal_details"]},
            {"data_controller": CSR, "data_scopes": ["crop_season", "measures"]},
        ],
    })
    assert status == 201, req
    assert req["controller_id"] is None
    assert [g["data_controller"] for g in req["grants"]] == [FR, CSR]
    assert req["requested_scopes"] == ["crop_season", "farmer_personal_details", "measures"]

    token = jwt.encode({"sub": "123456789012", "iss": "test"}, "k" * 32, algorithm="HS256")
    status, _ = call("POST", f"/consent/v1/consent-requests/{req['id']}/authenticate",
                     json={"id_token": token})
    assert status == 200

    # The farmer approves crop_season only from CSR and all of FR.
    status, art = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve", json={
        "grants": [
            {"data_controller": FR, "data_scopes": ["farmer_personal_details"]},
            {"data_controller": CSR, "data_scopes": ["crop_season"]},
        ],
    })
    assert status == 201, art
    assert art["controller_id"] is None
    grants = {g["data_controller"]: g for g in art["grants"]}
    assert grants[FR]["effective_data_scopes"] == ["farmer_personal_details"]
    assert grants[CSR]["effective_data_scopes"] == ["crop_season"]
    assert grants[FR]["policy_version"] == 1 and "partner_binding_id" not in grants[FR]
    assert art["effective_data_scopes"] == ["crop_season", "farmer_personal_details"]
    assert art["partner_id"] == p.audience


def test_originated_grants_rejects_controller_without_policy(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    status, body = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": {"type": "FAYDA_FAN", "value": "1"},
        "partner_id": p.audience,
        "purpose": {"code": "credit-assessment"},
        "grants": GRANTS,
    })
    assert status == 422, body


def test_originated_single_controller_request(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    status, body = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": {"type": "FAYDA_FAN", "value": "42"},
        "partner_id": p.audience,
        "purpose": {"code": "credit-assessment"},
        "requested_scopes": FR_SCOPES,
    })
    assert status == 400, body  # requested_scopes needs data_controller
    status, req = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": {"type": "FAYDA_FAN", "value": "42"},
        "partner_id": p.audience,
        "purpose": {"code": "credit-assessment"},
        "data_controller": FR,
        "requested_scopes": FR_SCOPES,
    })
    assert status == 201, req
    assert req["controller_id"] == FR and req["grants"] is None
    token = jwt.encode({"sub": "42"}, "k" * 32, algorithm="HS256")
    call("POST", f"/consent/v1/consent-requests/{req['id']}/authenticate", json={"id_token": token})
    status, art = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                       json={"granted_scopes": ["farm_details"]})
    assert status == 201, art
    assert art["controller_id"] == FR and art["grants"] is None
    assert art["effective_data_scopes"] == ["farm_details"]
