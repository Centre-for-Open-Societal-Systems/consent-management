"""Consent by lookup by one of the subject's other identifiers (G2P-5719): a
registry may search by an identifier other than the consent's subject (the
Crop Sown Registry by FARMER_ID for a FAYDA_FAN consent). Both lookups match
the consent's subject or one of its ``subject_identifiers``; the stored-consent
subject check accepts the same. Types are matched as spelled."""
import pytest
from test_assisted_consent import (  # noqa: F401 (fixtures)
    as_partner,
    portal,
    realm_key,
    upload,
)
from test_consent_lookup import lookup as terms_lookup
from test_consent_lookup import portal_consent, set_artefact
from test_exchange import cfg, post  # noqa: F401 (fixture)
from test_grants import CSR, FR
from test_registry_lookup import lookup, originate, partner  # noqa: F401 (fixture)
from test_template_consent import (  # noqa: F401 (fixtures)
    SUBJECT,
    bank,
    exchange,
    templates,
)

FARMER = {"type": "FARMER_ID", "value": "FR-0007"}


@pytest.fixture
def held(call, run, partner):  # noqa: F811
    """A registry consent (FR and CSR) whose subject also has a Farmer ID."""
    consent_id = originate(call, partner)
    set_artefact(run, consent_id, subject_identifiers=[FARMER])
    return consent_id


def test_registry_lookup_by_another_identifier(call, partner, held):  # noqa: F811
    body = lookup(call, partner.audience, CSR, FARMER)
    assert body["decision"] == "permit" and body["consent_id"] == held, body
    # The decision names the consent's subject.
    assert body["subject_id"] == SUBJECT


@pytest.mark.parametrize("subject", [
    {**FARMER, "value": "FR-0008"},            # wrong value
    {"type": "farmer_id", "value": "FR-0007"},  # type spelled differently: no aliasing
])
def test_registry_lookup_by_another_identifier_not_found(subject, call, partner, held):  # noqa: F811
    body = lookup(call, partner.audience, CSR, subject)
    assert body["decision"] == "deny" and body["reason_code"] == "consent_not_found", body


def test_terms_lookup_by_another_identifier(call, run, exchange, as_partner, bank,  # noqa: F811
                                               upload):
    consent_id = portal_consent(call, as_partner, bank, upload)
    set_artefact(run, consent_id, subject_identifiers=[FARMER])
    body = terms_lookup(call, bank.audience, subject=FARMER)
    assert body["decision"] == "permit" and body["consent_id"] == consent_id, body
    body = terms_lookup(call, bank.audience, subject={**FARMER, "value": "FR-0008"})
    assert body["reason_code"] == "consent_not_found", body


@pytest.mark.parametrize("subject,reason", [
    (FARMER, "ok"),                                  # one of the consent's identifiers
    ({**FARMER, "value": "FR-0008"}, "subject_mismatch"),  # its type, another value
    ({**SUBJECT, "value": "999"}, "subject_mismatch"),     # the subject's type, another value
    ({"type": "LAND_ID", "value": "L-1"}, "ok"),     # a type the consent lacks: the registry's
])
def test_stored_consent_subject_check(subject, reason, call, partner, held):  # noqa: F811
    body = post(call, {"consent_id": held, "consent_partner_id": partner.audience,
                       "data_controller": FR, "request_context": {"subject_id": subject}})
    assert body["reason_code"] == reason, body
