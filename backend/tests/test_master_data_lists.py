"""Purposes and subject ID types from Master Data code lists (partner portal).

Master Data's staff API is replaced by a fake behind an httpx transport, with
a fake Keycloak token endpoint. A loaded list is offered in the request
settings and enforced on portal requests; a list absent from the country's
Master Data (G2P-CAT-404), Master Data down or not configured = free text.
"""
import httpx
import pytest
from test_assisted_consent import (  # noqa: F401 (fixtures)
    as_partner,
    bank,
    portal,
    realm_key,
    request_body,
)
from test_exchange import cfg  # noqa: F401 (fixture)
from test_template_consent import exchange, templates  # noqa: F401 (fixtures)
from test_template_consent import bank as exchange_bank  # noqa: F401
from test_template_consent import create as create_from_template

MDS_URL = "http://mds.test"
TOKEN_URL = "http://keycloak.test/realms/staff/protocol/openid-connect/token"
PURPOSES = [
    {"value_code": "credit-assessment", "display": "Credit assessment", "status": "ACTIVE"},
    {"value_code": "insurance", "display": "Insurance", "status": "ACTIVE"},
    {"value_code": "old-purpose", "display": "Old", "status": "RETIRED"},
]
ID_TYPES = [
    {"value_code": "FAYDA_FAN", "display": "Fayda FAN", "status": "ACTIVE"},
    {"value_code": "FARMER_ID", "display": "Farmer ID", "status": "ACTIVE"},
]


class FakeMasterData:
    def __init__(self):
        self.lists = {"CONSENT_PURPOSE": PURPOSES, "SUBJECT_ID_TYPE": ID_TYPES}
        self.down = False
        self.calls = []
        self.token_calls = 0

    def handler(self, request):
        if self.down:
            raise httpx.ConnectError("Master Data is down", request=request)
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "svc-token", "expires_in": 300})
        assert request.url.path == "/catalogue/get_list_values"
        assert request.headers.get("authorization") == "Bearer svc-token"
        import json

        code = json.loads(request.content)["request_body"]["request_payload"]["list_code"]
        self.calls.append(code)
        if code not in self.lists:
            # Master Data answers errors as HTTP 200 with an ERROR header.
            return httpx.Response(200, json={"response_header": {
                "response_status": "ERROR", "response_error_code": "G2P-CAT-404",
                "response_error_message": f"list {code} not found"}})
        return httpx.Response(200, json={
            "response_header": {"response_status": "SUCCESS"},
            "response_body": {"response_payload": {
                "list_code": code, "version": {"version_no": 1}, "values": self.lists[code]}},
        })


@pytest.fixture
def mds(env):
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import MasterDataClient

    config = Settings.get_config()
    names = ("mds_url", "mds_token_url", "mds_client_id", "mds_client_secret")
    saved = {n: getattr(config, n) for n in names}
    config.mds_url, config.mds_token_url = MDS_URL, TOKEN_URL
    config.mds_client_id, config.mds_client_secret = "consent-manager", "secret"
    fake = FakeMasterData()
    client = MasterDataClient.get_component()
    client.transport = httpx.MockTransport(fake.handler)
    client.invalidate()
    yield fake
    for k, v in saved.items():
        setattr(config, k, v)
    client.transport = None
    client.invalidate()


def test_settings_offer_the_lists(portal, mds, as_partner, bank):  # noqa: F811
    status, settings = as_partner(bank.audience)("GET", "/request-settings")
    assert status == 200, settings
    assert settings["purposes"] == {"list_code": "CONSENT_PURPOSE", "values": [
        {"code": "credit-assessment", "label": "Credit assessment"},
        {"code": "insurance", "label": "Insurance"}]}  # retired value left out
    assert [v["code"] for v in settings["subject_id_types"]["values"]] == ["FAYDA_FAN",
                                                                           "FARMER_ID"]
    # Cached: a second read does not call Master Data (nor Keycloak) again.
    as_partner(bank.audience)("GET", "/request-settings")
    assert mds.calls == ["CONSENT_PURPOSE", "SUBJECT_ID_TYPE"] and mds.token_calls == 1


def test_unknown_purpose_or_id_type_refused(portal, mds, as_partner, bank):  # noqa: F811
    pc = as_partner(bank.audience)
    status, body = pc("POST", "/consent-requests",
                      json=request_body(purpose={"code": "marketing"}))
    assert status == 400 and "unknown purpose 'marketing'" in body["error"]
    assert "CONSENT_PURPOSE" in body["error"]
    status, body = pc("POST", "/consent-requests",
                      json=request_body(subject_id={"type": "PASSPORT", "value": "P1"}))
    assert status == 400 and "unknown subject ID type 'PASSPORT'" in body["error"]
    status, body = pc("POST", "/consent-requests", json=request_body())
    assert status == 201, body


def test_absent_list_is_free_text(portal, mds, as_partner, bank):  # noqa: F811
    del mds.lists["CONSENT_PURPOSE"]
    pc = as_partner(bank.audience)
    settings = pc("GET", "/request-settings")[1]
    assert settings["purposes"] is None and settings["subject_id_types"] is not None
    # The purpose is then checked only against the partner's policy.
    status, body = pc("POST", "/consent-requests",
                      json=request_body(subject_id={"type": "PASSPORT", "value": "P1"}))
    assert status == 400 and "subject ID type" in body["error"]


def test_master_data_down_accepts(portal, mds, as_partner, bank):  # noqa: F811
    from openg2p_consent_manager.services import MasterDataClient

    mds.lists["SUBJECT_ID_TYPE"] = [{"value_code": "FARMER_ID", "status": "ACTIVE"}]
    pc = as_partner(bank.audience)
    status, body = pc("POST", "/consent-requests", json=request_body())
    assert status == 400 and "FAYDA_FAN" in body["error"]
    mds.down = True
    MasterDataClient.get_component().invalidate()
    settings = pc("GET", "/request-settings")[1]
    assert settings["purposes"] is None and settings["subject_id_types"] is None
    status, body = pc("POST", "/consent-requests", json=request_body())
    assert status == 201, body


def test_not_configured_is_free_text(portal, as_partner, bank):  # noqa: F811
    from openg2p_consent_manager.services import MasterDataClient

    MasterDataClient.get_component().invalidate()
    settings = as_partner(bank.audience)("GET", "/request-settings")[1]
    assert settings["purposes"] is None and settings["subject_id_types"] is None


def test_exchange_checks_id_type_not_template_purpose(  # noqa: F811
    exchange, mds, as_partner, exchange_bank,  # noqa: F811
):
    mds.lists["CONSENT_PURPOSE"] = [{"value_code": "something-else", "status": "ACTIVE"}]
    pc = as_partner(exchange_bank.audience)
    # The template's purpose (credit-assessment) is the template source's: not checked.
    status, body = create_from_template(pc)
    assert status == 201, body
    status, body = create_from_template(pc, subject_id={"type": "PASSPORT", "value": "P1"})
    assert status == 400 and "unknown subject ID type 'PASSPORT'" in body["error"]
