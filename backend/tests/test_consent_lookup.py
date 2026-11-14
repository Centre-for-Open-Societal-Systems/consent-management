"""Consent by lookup (G2P-5719, contract §12): the Composite calls /validate in
caller-terms mode with neither consent_jws nor consent_id; the Exchange CM finds
the consent it holds for (consent_partner_id, subject, purpose) and validates
it as a stored consent. Also: the department's token status check is off by
default."""
from datetime import datetime, timedelta, timezone

import pytest
from test_assisted_consent import (  # noqa: F401 (fixtures)
    as_partner,
    portal,
    realm_key,
    upload,
)
from test_exchange import (  # noqa: F401 (fixture)
    PRESENTER,
    _sign_receipt,
    cfg,
    department_on,
    exchange_on,
    post,
)
from test_grants import CSR, FR, GRANTS, bind
from test_template_consent import (  # noqa: F401 (fixtures)
    SUBJECT,
    TEMPLATE,
    approve_assisted,
    bank,
    exchange,
    templates,
)

SCOPES = ["farmer_personal_details", "crop_season", "measures"]


def lookup(call, audience, subject=SUBJECT, purpose="credit-assessment", partner_id=PRESENTER,
           **extra):
    context = {"subject_id": subject, "purpose": purpose, "requested_scopes": SCOPES,
               "template": TEMPLATE}
    if partner_id != PRESENTER:
        context.pop("template")
    return post(call, {
        "consent_partner_id": audience, "partner_id": partner_id, "issue_receipts": True,
        "request_context": {k: v for k, v in context.items() if v is not None},
        **extra,
    })


def portal_consent(call, as_partner, bank, upload, **extra):  # noqa: F811
    _, (status, verified) = approve_assisted(call, as_partner(bank.audience), upload, **extra)
    assert status == 200, verified
    return verified["consent_id"]


def set_artefact(run, consent_id, **fields):
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentArtefact

    async def go():
        async with async_session()() as s:
            art = await s.get(ConsentArtefact, consent_id)
            for k, v in fields.items():
                setattr(art, k, v)
            await s.commit()

    run(go())


def decision_log(run, consent_id):
    from sqlalchemy import select

    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import DecisionLog

    async def go():
        async with async_session()() as s:
            rows = await s.execute(
                select(DecisionLog).where(DecisionLog.consent_id == consent_id)
            )
            return list(rows.scalars().all())

    return run(go())


def test_lookup_finds_a_portal_consent(call, run, exchange, as_partner, bank, upload):  # noqa: F811
    consent_id = portal_consent(call, as_partner, bank, upload)
    body = lookup(call, bank.audience)
    assert body["decision"] == "permit", body
    assert body["consent_id"] == consent_id
    assert body["grants"] == {FR: ["farmer_personal_details"], CSR: ["crop_season", "measures"]}
    assert set(body["receipts"]) == {FR, CSR}
    logs = [r for r in decision_log(run, consent_id) if r.decision == "permit"]
    assert {r.data_controller for r in logs} == {FR, CSR}
    assert all(r.policy_ref == f"template:{TEMPLATE}" and r.policy_id is None for r in logs)


def test_lookup_picks_latest_valid_until(call, run, exchange, as_partner, bank, upload):  # noqa: F811
    now = datetime.now(timezone.utc)
    first = portal_consent(call, as_partner, bank, upload)
    second = portal_consent(call, as_partner, bank, upload)
    third = portal_consent(call, as_partner, bank, upload)
    set_artefact(run, first, valid_until=now + timedelta(days=90))
    set_artefact(run, second, valid_until=now + timedelta(days=10))
    set_artefact(run, third, valid_until=now + timedelta(days=30))
    assert lookup(call, bank.audience)["consent_id"] == first
    # Tie on valid_until: the newest.
    set_artefact(run, third, valid_until=now + timedelta(days=90))
    assert lookup(call, bank.audience)["consent_id"] == third


@pytest.mark.parametrize("case", ["revoked", "expired", "other_purpose", "other_subject",
                                  "other_partner"])
def test_lookup_not_found(case, call, run, exchange, as_partner, new_partner, bank,  # noqa: F811
                          upload):
    consent_id = portal_consent(call, as_partner, bank, upload)
    ask = {}
    if case == "revoked":
        set_artefact(run, consent_id, status="revoked")
    elif case == "expired":
        set_artefact(run, consent_id, valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    elif case == "other_purpose":
        ask["purpose"] = "insurance"
    elif case == "other_subject":
        ask["subject"] = {**SUBJECT, "value": "999999999999"}
    audience = bank.audience
    if case == "other_partner":
        other = new_partner()
        exchange.offer(other.audience, [TEMPLATE])
        audience = other.audience
    body = lookup(call, audience, **ask)
    assert body["decision"] == "deny" and body["reason_code"] == "consent_not_found", body
    assert body["receipts"] is None and body["consent_id"] is None


def test_lookup_only_for_a_presenter_in_terms_mode(call, exchange, as_partner, bank,  # noqa: F811
                                                   upload):
    portal_consent(call, as_partner, bank, upload)
    for body in (
        lookup(call, bank.audience, partner_id=bank.audience),  # not a presenter
        lookup(call, bank.audience, purpose=None),  # no purpose
        lookup(call, None),  # no consent_partner_id
        post(call, {"consent_partner_id": bank.audience, "partner_id": PRESENTER,
                    "request_context": {"purpose": "credit-assessment"}}),  # no subject
    ):
        assert body["decision"] == "deny" and body["reason_code"] == "malformed_object", body
    # Both consent_jws and consent_id: still refused.
    both = post(call, {"consent_jws": "a.b.c", "consent_id": "x", "partner_id": PRESENTER})
    assert both["reason_code"] == "malformed_object"


def test_partner_signed_consent_is_found_after_first_use(call, cfg, new_partner):
    exchange_on(cfg)
    p = new_partner()
    jws = p.sign(p.claims(grants=GRANTS))
    first = post(call, {"consent_jws": jws, "partner_id": PRESENTER, "issue_receipts": True,
                        "request_context": {"subject_id": SUBJECT, "template": TEMPLATE,
                                            "purpose": "credit-assessment",
                                            "requested_scopes": SCOPES,
                                            "limits": {"allow_partner_consents": True}}})
    assert first["decision"] == "permit", first
    body = lookup(call, p.audience)
    assert body["decision"] == "permit", body
    assert body["grants"] == first["grants"]
    assert set(body["receipts"]) == {FR, CSR}
    assert body["consent_id"]
    # Found for its purpose only.
    assert lookup(call, p.audience, purpose="insurance")["reason_code"] == "consent_not_found"


def test_status_check_is_off_by_default(call, cfg, env, pm):
    """The department checks tokens offline: the status URL (here one that
    would fail closed) is not called."""
    from conftest import Partner

    from openg2p_consent_manager.config import Settings

    assert Settings.model_fields["receipt_status_check"].default == "never"
    assert Settings.model_fields["receipt_ttl_seconds"].default == 300
    presenter = Partner(env, audience="lookup-composite")
    bind(pm, presenter, FR, ["farmer_personal_details"])
    department_on(cfg, presenter=presenter.audience,
                  status_url="http://cm/consent/v1/validate?jti={jti}")
    body = post(call, {"consent_jws": _sign_receipt(presenter=presenter.audience),
                       "partner_id": presenter.audience, "data_controller": FR})
    assert body["decision"] == "permit", body
