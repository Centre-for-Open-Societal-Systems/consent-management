"""Agri Stack exchange (G2P-5719): consent receipts between Consent Managers.

One CM app plays both roles here. As the exchange CM it validates the
partner's consent with ``issue_receipts`` and signs receipts; as the department
CM it trusts that issuer, fetching the JWKS and receipt status over HTTP — from
itself, through an ASGI transport — exactly as it would from another install.
With the exchange settings empty (the default), nothing changes.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from jwt.api_jws import PyJWS
from test_grants import CSR, CSR_SCOPES, FR, FR_SCOPES, GRANTS, bind

ISSUER = "agri-stack-exchange-cm"
PRESENTER = "agri-composite"


def _b64json(segment):
    from openg2p_consent_manager.utils.canonical import b64url_decode

    return json.loads(b64url_decode(segment))


@pytest.fixture
def cfg(env):
    """Set exchange settings for one test; restore the defaults after."""
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import ExchangeService

    config = Settings.get_config()
    names = ("receipt_issuer", "receipt_presenters", "receipt_ttl_seconds",
             "trusted_receipt_issuers", "receipt_status_check",
             "receipt_jwks_refresh_cooldown_seconds")
    saved = {n: getattr(config, n) for n in names}
    exchange = ExchangeService.get_component()
    transport = httpx.ASGITransport(app=env["app"])
    exchange.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, base_url="http://cm"
    )
    exchange.clear_caches()

    def set_(**kw):
        for k, v in kw.items():
            setattr(config, k, v)

    yield set_
    set_(**saved)
    exchange.clear_caches()


def exchange_on(cfg):
    cfg(receipt_issuer=ISSUER, receipt_presenters=[PRESENTER])


def department_on(cfg, issuer=ISSUER, presenter=PRESENTER, **extra):
    from openg2p_consent_manager.config import TrustedReceiptIssuer

    cfg(trusted_receipt_issuers=[TrustedReceiptIssuer(
        issuer=issuer, jwks_url="http://cm/.well-known/jwks.json", presenter=presenter,
        **extra,
    )])


def post(call, payload):
    status, body = call("POST", "/consent/v1/validate", json=payload)
    assert status == 200, body
    return body


@pytest.fixture
def bank(call, new_partner):
    """A partner bound at the exchange to FR and CSR (full scopes)."""
    p = new_partner()
    bind(call, p, FR, FR_SCOPES)
    bind(call, p, CSR, CSR_SCOPES)
    return p


@pytest.fixture(scope="session")
def presenter_binding(env, call):
    """The department's standing policy for the exchange presenter at FR: only
    farmer_personal_details (narrower than the exchange grant)."""
    from conftest import Partner

    presenter = Partner(env, audience=PRESENTER)
    bind(call, presenter, FR, ["farmer_personal_details"])
    return presenter


def issue(call, bank, presenter=PRESENTER, **extra):
    jws = bank.sign(bank.claims(grants=GRANTS))
    return post(call, {"consent_jws": jws, "partner_id": presenter,
                       "issue_receipts": True, **extra})


# ── exchange role: issuing ───────────────────────────────────────────────────


def test_permit_issues_one_receipt_per_controller(call, cfg, bank):
    exchange_on(cfg)
    body = issue(call, bank)
    assert body["decision"] == "permit", body
    assert set(body["receipts"]) == {FR, CSR}
    assert body["subject_id"] == {"type": "FAYDA_FAN", "value": "123456789012"}


def test_issue_for_one_named_controller(call, cfg, bank):
    exchange_on(cfg)
    body = issue(call, bank, data_controller=CSR)
    assert body["decision"] == "permit", body
    assert list(body["receipts"]) == [CSR]
    assert body["data_controller"] == CSR
    assert body["effective_data_scopes"] == sorted(CSR_SCOPES)


def test_receipt_claims_and_signature(call, cfg, bank):
    from openg2p_consent_manager.services import CryptoService

    exchange_on(cfg)
    before = int(datetime.now(timezone.utc).timestamp())
    body = issue(call, bank)
    receipt = body["receipts"][FR]
    header = _b64json(receipt.split(".")[0])
    crypto = CryptoService.get_component()
    assert header == {"alg": crypto.algorithm, "kid": crypto.kid, "typ": "consent-receipt+jwt"}

    _, jwks = call("GET", "/.well-known/jwks.json")
    key = jwt.PyJWK(jwks["keys"][0])
    # JWS-level verify: `sub` is an object ({type, value}, as the consent's
    # subject_id), which RFC 7519 JWT validators reject.
    claims = json.loads(PyJWS().decode(receipt, key.key, algorithms=[header["alg"]]))
    assert claims["iss"] == ISSUER
    assert claims["aud"] == FR
    assert claims["presenter"] == PRESENTER
    assert claims["partner"] == bank.audience
    assert claims["sub"] == {"type": "FAYDA_FAN", "value": "123456789012"}
    assert claims["purpose"] == "credit-assessment"
    assert sorted(claims["scopes"]) == sorted(FR_SCOPES)
    assert claims["consent_id"]
    uuid.UUID(claims["jti"])
    assert before <= claims["iat"] == claims["nbf"]
    # exp = min(consent expiry (30 days), now + 900 s).
    assert claims["exp"] - claims["iat"] == 900
    assert claims["consent_exp"] > claims["exp"]
    datetime.fromisoformat(claims["consent_issued_at"])
    # consent_id is this CM's artefact for that controller.
    status, st = call("GET", f"/consent/v1/consents/{claims['consent_id']}/status")
    assert status == 200 and st["status"] == "active"


def test_receipt_ttl_capped_by_consent_expiry(call, cfg, bank):
    exchange_on(cfg)
    now = datetime.now(timezone.utc)
    claims = bank.claims(grants=GRANTS, validity={
        "valid_from": (now - timedelta(minutes=1)).isoformat(),
        "valid_until": (now + timedelta(minutes=5)).isoformat(),
    })
    body = post(call, {"consent_jws": bank.sign(claims), "partner_id": PRESENTER,
                       "issue_receipts": True})
    receipt = _b64json(body["receipts"][FR].split(".")[1])
    assert receipt["exp"] == receipt["consent_exp"]
    assert receipt["exp"] - receipt["iat"] <= 300


def test_caller_not_a_presenter_is_refused(call, cfg, bank):
    exchange_on(cfg)
    body = issue(call, bank, presenter="someone-else")
    assert body["decision"] == "deny"
    assert body["reason_code"] == "receipt_presenter_not_allowed"
    assert body["receipts"] is None


def test_issue_receipts_refused_when_not_configured(call, cfg, bank):
    body = issue(call, bank)
    assert body["decision"] == "deny"
    assert body["reason_code"] == "receipt_presenter_not_allowed"
    assert "not enabled" in body["detail"]


def test_deny_issues_no_receipts(call, cfg, new_partner):
    exchange_on(cfg)
    p = new_partner()
    bind(call, p, FR, FR_SCOPES)  # not bound to CSR
    body = post(call, {"consent_jws": p.sign(p.claims(grants=GRANTS)),
                       "partner_id": PRESENTER, "issue_receipts": True})
    assert body["decision"] == "deny"
    assert body["reason_code"] == "unknown_partner"
    assert body["data_controller"] == CSR
    assert body["receipts"] is None


def test_validate_without_issue_receipts_is_unchanged(call, cfg, bank):
    exchange_on(cfg)
    jws = bank.sign(bank.claims(grants=GRANTS))
    body = post(call, {"consent_jws": jws, "data_controller": FR, "partner_id": PRESENTER})
    assert body["decision"] == "permit"
    assert body["receipts"] is None


# ── exchange role: receipt status ────────────────────────────────────────────


def _jti(receipt):
    return _b64json(receipt.split(".")[1])["jti"]


def receipt_status(call, receipt):
    status, body = call("GET", f"/consent/v1/receipts/{_jti(receipt)}/status")
    assert status == 200, body
    return body["status"]


def test_receipt_status_active_revoked_expired(call, run, cfg, bank):
    from sqlalchemy import update

    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import IssuedReceipt

    exchange_on(cfg)
    receipts = issue(call, bank)["receipts"]
    assert receipt_status(call, receipts[FR]) == "active"

    # Revoking the FR consent revokes the FR receipt only.
    consent_id = _b64json(receipts[FR].split(".")[1])["consent_id"]
    status, _ = call("POST", f"/consent/v1/consents/{consent_id}/revoke", json={})
    assert status == 200
    assert receipt_status(call, receipts[FR]) == "revoked"
    assert receipt_status(call, receipts[CSR]) == "active"

    async def expire():
        async with async_session()() as session:
            await session.execute(
                update(IssuedReceipt).where(IssuedReceipt.id == _jti(receipts[CSR]))
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
            await session.commit()

    run(expire())
    assert receipt_status(call, receipts[CSR]) == "expired"


def test_unknown_receipt_status_is_404(call):
    status, _ = call("GET", f"/consent/v1/receipts/{uuid.uuid4()}/status")
    assert status == 404


# ── department role: accepting receipts ──────────────────────────────────────


def present(call, receipt, data_controller=FR, partner_id=PRESENTER, **ctx):
    payload = {"consent_jws": receipt, "data_controller": data_controller,
               "partner_id": partner_id}
    if ctx:
        payload["request_context"] = ctx
    return post(call, payload)


def last_decision_log(run, jti):
    from sqlalchemy import select

    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import DecisionLog

    async def go():
        async with async_session()() as session:
            result = await session.execute(
                select(DecisionLog).where(DecisionLog.receipt_jti == jti)
                .order_by(DecisionLog.created_at.desc())
            )
            return result.scalars().first()

    return run(go())


def test_trusted_receipt_permits_with_intersected_scopes(
    call, run, cfg, bank, presenter_binding
):
    exchange_on(cfg)
    department_on(cfg)
    receipt = issue(call, bank)["receipts"][FR]
    body = present(call, receipt)
    assert body["decision"] == "permit", body
    # receipt scopes (both FR scopes) ∩ the department's policy for the presenter.
    assert body["effective_data_scopes"] == ["farmer_personal_details"]
    assert body["subject_id"] == {"type": "FAYDA_FAN", "value": "123456789012"}
    assert body["data_controller"] == FR
    assert body["policy_version"] == 1
    log = last_decision_log(run, _jti(receipt))
    assert log.decision == "permit" and log.receipt_issuer == ISSUER
    # Presented again: the stored decision.
    again = present(call, receipt)
    assert again["decision"] == "permit" and again["consent_id"] == body["consent_id"]


def test_requested_scopes_narrow_further(call, cfg, bank, presenter_binding):
    exchange_on(cfg)
    department_on(cfg)
    receipt = issue(call, bank)["receipts"][FR]
    body = present(call, receipt, requested_scopes=["farm_details"])
    assert body["decision"] == "deny"
    assert body["reason_code"] == "scope_exceeds_policy"


def test_wrong_audience_is_denied(call, cfg, bank, presenter_binding):
    exchange_on(cfg)
    department_on(cfg)
    receipt = issue(call, bank)["receipts"][CSR]
    body = present(call, receipt, data_controller=FR)
    assert body["reason_code"] == "audience_mismatch"


def test_wrong_presenter_is_denied(call, run, cfg, bank, presenter_binding):
    exchange_on(cfg)
    department_on(cfg)
    receipt = issue(call, bank)["receipts"][FR]
    body = present(call, receipt, partner_id="bank-direct")
    assert body["reason_code"] == "presenter_mismatch"
    assert last_decision_log(run, _jti(receipt)).reason_code == "presenter_mismatch"
    # A trusted issuer pinned to another presenter.
    department_on(cfg, presenter="other-composite")
    assert present(call, receipt)["reason_code"] == "presenter_mismatch"


def _sign_receipt(**overrides):
    from openg2p_consent_manager.services import CryptoService

    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "iss": ISSUER, "jti": str(uuid.uuid4()), "consent_id": str(uuid.uuid4()),
        "aud": FR, "presenter": PRESENTER, "partner": "bank-x",
        "sub": {"type": "FAYDA_FAN", "value": "123456789012"},
        "purpose": "credit-assessment", "scopes": ["farmer_personal_details"],
        "consent_issued_at": datetime.now(timezone.utc).isoformat(),
        "consent_exp": now + 86400, "iat": now, "nbf": now, "exp": now + 900,
    }
    claims.update(overrides)
    return CryptoService.get_component().sign_jws(claims, "consent-receipt+jwt")


def test_expired_receipt_is_denied(call, cfg, presenter_binding):
    department_on(cfg)
    now = int(datetime.now(timezone.utc).timestamp())
    receipt = _sign_receipt(iat=now - 1000, nbf=now - 1000, exp=now - 100)
    body = present(call, receipt)
    assert body["reason_code"] == "expired"


def test_revoked_receipt_is_denied(call, cfg, bank, presenter_binding):
    exchange_on(cfg)
    department_on(cfg)
    receipt = issue(call, bank)["receipts"][FR]
    consent_id = _b64json(receipt.split(".")[1])["consent_id"]
    call("POST", f"/consent/v1/consents/{consent_id}/revoke", json={})
    body = present(call, receipt)
    assert body["decision"] == "deny"
    assert body["reason_code"] == "revoked"


def test_status_check_never_skips_the_issuer(call, cfg, presenter_binding):
    department_on(cfg)
    receipt = _sign_receipt()  # not recorded at the issuer → status "unknown"
    assert present(call, receipt)["reason_code"] == "receipt_invalid"
    cfg(receipt_status_check="never")
    assert present(call, receipt)["decision"] == "permit"


def test_status_unreachable_fails_closed(call, cfg, presenter_binding):
    # The status URL answers 405 (GET on a POST route): not a status → deny.
    department_on(cfg, status_url="http://cm/consent/v1/validate?jti={jti}")
    assert present(call, _sign_receipt())["reason_code"] == "receipt_status_unavailable"


def test_untrusted_issuer_is_denied(call, run, cfg, bank, presenter_binding):
    exchange_on(cfg)
    department_on(cfg, issuer="some-other-exchange")
    receipt = issue(call, bank)["receipts"][FR]
    body = present(call, receipt)
    assert body["reason_code"] == "receipt_issuer_not_trusted"
    log = last_decision_log(run, _jti(receipt))
    assert log.receipt_issuer == ISSUER and log.decision == "deny"


def test_forged_receipt_is_denied(call, cfg, presenter_binding):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from jwt.api_jws import PyJWS

    from openg2p_consent_manager.services import CryptoService

    department_on(cfg)
    genuine = _sign_receipt()
    claims = _b64json(genuine.split(".")[1])
    forged = PyJWS().encode(
        json.dumps(claims).encode(), ed25519.Ed25519PrivateKey.generate(),
        algorithm="EdDSA",
        headers={"kid": CryptoService.get_component().kid, "typ": "consent-receipt+jwt"},
    )
    assert present(call, forged)["reason_code"] == "receipt_invalid"


def test_unknown_kid_refetches_jwks(call, cfg, presenter_binding):
    import time

    from openg2p_consent_manager.services import ExchangeService

    department_on(cfg)
    cfg(receipt_status_check="never", receipt_jwks_refresh_cooldown_seconds=0)
    # A cached JWKS without the current key (as after an issuer key rotation).
    ExchangeService.get_component()._jwks[ISSUER] = (time.monotonic() - 1, {})
    assert present(call, _sign_receipt())["decision"] == "permit"


def test_presenter_not_bound_at_department(call, cfg):
    department_on(cfg, presenter="")
    cfg(receipt_status_check="never")
    receipt = _sign_receipt(presenter="unbound-composite")
    body = present(call, receipt, partner_id="unbound-composite")
    assert body["reason_code"] == "unknown_partner"


def test_no_issuers_configured_receipt_denied_and_consents_unchanged(call, cfg, bank):
    receipt = _sign_receipt()
    body = present(call, receipt)
    assert body["decision"] == "deny"
    assert body["reason_code"] == "receipt_issuer_not_trusted"
    # A partner's own consent is validated exactly as before.
    jws = bank.sign(bank.claims(grants=GRANTS))
    body = post(call, {"consent_jws": jws, "data_controller": FR, "partner_id": bank.audience})
    assert body["decision"] == "permit"
    assert body["effective_data_scopes"] == sorted(FR_SCOPES)
