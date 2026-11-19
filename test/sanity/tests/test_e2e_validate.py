import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sanity.pm_seed import SANITY_PURPOSE, SANITY_SCOPES, SANITY_SUBJECT_ID_TYPE
from sanity.signing import (
    load_private_key_pem,
    sign_jws,
    tamper_jws_payload,
    verify_receipt_signature,
)

# Full PDP round-trip. Partner keys and policies live in Partner Management
# (PM); CM reads them at validate time. The pm_partner fixture ensures the
# persistent sanity partner (audience cm-sanity = PM PARTNER_CM_SANITY) has the
# key and a scope policy for the sanity controller. The e2e signs the consent
# object as a compact JWS with the matching private key, exercises permit and
# the denials, then verifies the CM receipt against its JWKS. Gated by
# SANITY_RUN_E2E and by the presence of PM.

SCOPES = SANITY_SCOPES


@pytest.mark.e2e
def test_validate_roundtrip(partner_client, cfg, pm_partner):
    # partner_client = PARTNER api (/validate, receipts, JWKS — no Keycloak).
    if not cfg.run_e2e:
        pytest.skip("SANITY_RUN_E2E not enabled")

    # pm_partner fixture has ensured PM serves the key and the policy.
    aud = cfg.partner_audience
    kid = cfg.pm_kid
    priv = load_private_key_pem(cfg.pm_private_key_pem)
    now = datetime.now(timezone.utc)
    alg = _alg_for(priv)  # inferred from the PM-registered key

    def make_claims(scopes, controller=cfg.controller_id):
        return {
            "@context": "https://openg2p.org/contexts/consent_object.jsonld",
            "@type": "ConsentObject",
            "jti": uuid.uuid4().hex,
            "subject_id": {"type": SANITY_SUBJECT_ID_TYPE, "value": "FARMER_1234"},
            "data_controller": controller, "aud": aud,
            "purpose": {"code": SANITY_PURPOSE, "text": "sanity"},
            "data_scopes": scopes, "fetch_type": "oneshot",
            "validity": {
                "valid_from": now.isoformat(),
                "valid_until": (now + timedelta(days=30)).isoformat(),
            },
            "issued_at": now.isoformat(),
        }

    def validate(jws, **extra):
        r = partner_client.post("/consent/v1/validate", json={
            "consent_jws": jws, "partner_id": aud, **extra,
        })
        assert r.status_code == 200, r.text
        return r.json()

    # 1. permit — request a subset; effective = consented ∩ policy ∩ requested
    jws = sign_jws(make_claims(SCOPES + ["farmer_profile.landholdings"]), priv, kid, algorithm=alg)
    dec = validate(jws, request_context={"requested_scopes": SCOPES})
    assert dec["decision"] == "permit", dec
    assert set(dec["effective_data_scopes"]) == set(SCOPES), dec
    receipt_id = dec["receipt_id"]

    # 2. the CM receipt is signed by the CM key — verify against its JWKS
    receipt = partner_client.get(f"/consent/v1/receipts/{receipt_id}").json()
    jwks = partner_client.get("/.well-known/jwks.json").json()
    sig = receipt["signature"]
    msg = receipt["consent_artefact"]["hash"].encode("utf-8")
    assert verify_receipt_signature(
        jwks, sig["kid"], sig["algorithm"], msg, sig["value"]
    ), "CM receipt signature did not verify against the published JWKS"

    # 3. denial — tampered JWS (payload swapped, original signature kept)
    good_claims = make_claims(["farmer_profile.basic"])
    good_jws = sign_jws(good_claims, priv, kid, algorithm=alg)
    tampered = tamper_jws_payload(good_jws, {**good_claims, "data_scopes": ["farmer_profile.crops"]})
    d = validate(tampered)
    assert d["decision"] == "deny" and d["reason_code"] == "signature_invalid", d

    # 4. denial — scope outside policy
    over_jws = sign_jws(make_claims(["farmer_profile.landholdings"]), priv, kid, algorithm=alg)
    d = validate(over_jws)
    assert d["decision"] == "deny" and d["reason_code"] == "scope_exceeds_policy", d

    # 5. denial — a controller the partner has no PM policy for
    no_policy_jws = sign_jws(
        make_claims(SCOPES, controller=f"{cfg.controller_id}-no-policy"), priv, kid, algorithm=alg
    )
    d = validate(no_policy_jws)
    assert d["decision"] == "deny" and d["reason_code"] == "unknown_partner", d


def _alg_for(priv) -> str:
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    if isinstance(priv, ed25519.Ed25519PrivateKey):
        return "EdDSA"
    if isinstance(priv, ec.EllipticCurvePrivateKey):
        return "ES256"
    if isinstance(priv, rsa.RSAPrivateKey):
        return "RS256"
    raise ValueError("unsupported private key type for sanity signing")
