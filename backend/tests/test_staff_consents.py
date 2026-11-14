"""Staff API: a consent by id, its revocation on the subject's request, and the
receipt's withdrawal method (receipt_withdrawal_method)."""
import jwt
import pytest
from test_assisted_consent import (  # noqa: F401 (fixtures)
    as_partner,
    portal,
    realm_key,
    upload,
)
from test_consent_lookup import lookup, portal_consent
from test_exchange import cfg, exchange_on  # noqa: F401 (fixtures)
from test_template_consent import bank, exchange, templates  # noqa: F401 (fixtures)

CONSENTS = "/consent/v1/consents"


def staff_token(*roles, username="officer1"):
    claims = {"sub": username, "preferred_username": username, "email": f"{username}@x",
              "realm_access": {"roles": list(roles)}}
    return {"Authorization": "Bearer " + jwt.encode(claims, "k" * 32, algorithm="HS256")}


@pytest.fixture
def staff_auth():
    """Call ``staff_auth.on()`` to turn Keycloak auth on (tokens decoded
    unverified: no issuer set); restored after the test."""
    from openg2p_consent_manager.config import Settings

    config = Settings.get_config()
    saved = (config.auth_enabled, config.auth_issuer)

    def on():
        config.auth_enabled, config.auth_issuer = True, ""

    config.on = on
    yield config
    del config.on
    config.auth_enabled, config.auth_issuer = saved


def revocation_records(run, consent_id):
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import RevocationRecord
    from sqlalchemy import select

    async def go():
        async with async_session()() as s:
            rows = await s.execute(
                select(RevocationRecord).where(RevocationRecord.consent_id == consent_id)
            )
            return list(rows.scalars().all())

    return run(go())


def test_staff_get_and_revoke(call, run, staff_auth, exchange, as_partner, bank,  # noqa: F811
                              upload):  # noqa: F811
    consent_id = portal_consent(call, as_partner, bank, upload)
    staff_auth.on()
    approver = staff_token(staff_auth.auth_approver_role)

    # Staff roles only.
    assert call("GET", f"{CONSENTS}/{consent_id}")[0] == 401
    assert call("GET", f"{CONSENTS}/{consent_id}", headers=staff_token("OTHER"))[0] == 403
    assert call("POST", f"{CONSENTS}/{consent_id}/revocations", json={"reason": "asked"},
                headers=staff_token("OTHER"))[0] == 403

    status, body = call("GET", f"{CONSENTS}/{consent_id}", headers=approver)
    assert status == 200, body
    assert body["consent_id"] == consent_id and body["status"] == "active"
    assert body["partner_id"] == bank.audience and body["source"]
    assert body["grants"] and all(g["policy_ref"] for g in body["grants"])
    assert body["receipt_id"] and body["evidence"] and body["revocations"] == []
    status, receipt = call("GET", f"{CONSENTS}/{consent_id}/receipt", headers=approver)
    assert status == 200 and receipt["consent_artefact"]["@id"] == consent_id
    assert receipt["withdrawal"] == {
        "method": "Ask the organisation operating this Consent Manager to revoke it "
                  f"(consent ID {consent_id})."
    }
    assert call("GET", f"{CONSENTS}/nope", headers=approver)[0] == 404

    assert lookup(call, bank.audience)["decision"] == "permit"

    # A reason is required.
    assert call("POST", f"{CONSENTS}/{consent_id}/revocations", json={"reason": " "},
                headers=approver)[0] == 400
    status, body = call("POST", f"{CONSENTS}/{consent_id}/revocations",
                        json={"reason": "subject asked at the office"}, headers=approver)
    assert status == 200 and body["status"] == "revoked", body

    # Revoked exactly as by the subject: /validate (by id) and lookup deny it.
    by_id = call("POST", "/consent/v1/validate",
                 json={"consent_id": consent_id, "consent_partner_id": bank.audience})[1]
    assert by_id["decision"] == "deny" and by_id["reason_code"] == "revoked"
    assert lookup(call, bank.audience)["reason_code"] == "consent_not_found"

    (record,) = revocation_records(run, consent_id)
    assert record.originated_by == "subject" and record.revoked_by == "officer1"
    assert record.reason == "subject asked at the office"
    body = call("GET", f"{CONSENTS}/{consent_id}", headers=approver)[1]
    assert body["status"] == "revoked" and body["revocations"][0]["revoked_by"] == "officer1"

    # Admins may revoke too; a revoked consent cannot be revoked again.
    assert call("POST", f"{CONSENTS}/{consent_id}/revocations", json={"reason": "again"},
                headers=staff_token(staff_auth.auth_admin_role))[0] == 409


def test_receipt_withdrawal_method_from_setting():
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import ReceiptService

    config = Settings.get_config()
    saved = (config.receipt_withdrawal_method, config.receipt_withdrawal_subject_api_url)
    try:
        assert ReceiptService.withdrawal("c1") == {
            "method": "Ask the organisation operating this Consent Manager to revoke it "
                      "(consent ID c1)."
        }
        config.receipt_withdrawal_method = "Call 1800-123 or visit https://x.example ({consent_id})"
        config.receipt_withdrawal_subject_api_url = "https://bene.example/api/cm/"
        assert ReceiptService.withdrawal("c1") == {
            "method": "Call 1800-123 or visit https://x.example (c1)",
            "additional_methods": ["POST https://bene.example/api/cm/consent/v1/my/consents/c1/revoke"],
        }
        config.receipt_withdrawal_method = "  "
        assert ReceiptService.withdrawal("c1")["method"].startswith("Ask the organisation")
    finally:
        config.receipt_withdrawal_method, config.receipt_withdrawal_subject_api_url = saved
