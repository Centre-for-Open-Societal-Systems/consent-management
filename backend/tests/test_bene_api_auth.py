# ruff: noqa: F811  (fixtures imported from test_subject_auth are test parameters)
"""Beneficiary API auth with national ID: the subject logs in at eSignet (faked
as in test_subject_auth) from a web page or as an API client, gets a beneficiary
API token, sees their own requests and consents and approves / denies / revokes
them; other subjects' requests stay hidden; on the beneficiary API only this
token is taken on the subject's routes."""
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import test_subject_auth
from cryptography.hazmat.primitives import serialization
from test_grants import FR, FR_SCOPES, bind
from test_subject_auth import CLIENT_ID, ESIGNET, FakeEsignet, keys  # noqa: F401  (fixture)

BENE_REDIRECT = "https://consent.test/consent/v1/beneficiary/auth/callback"
PAGE = "https://consent.test/my"
FIN = "123456789012"
OTHER_FIN = "999988887777"
AUTH = "/consent/v1/beneficiary/auth"


@pytest.fixture
def raw(env):
    """GET without following redirects → the httpx response."""
    transport = httpx.ASGITransport(app=env["app"])

    def _get(path, params=None):
        async def go():
            async with httpx.AsyncClient(transport=transport, base_url="http://cm") as c:
                return await c.get(path, params=params)

        return env["loop"].run_until_complete(go())

    return _get


@pytest.fixture
def esignet(env, keys, monkeypatch):
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import SubjectAuthService

    config = Settings.get_config()
    pem = keys.client.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    for name, value in {
        "subject_auth_discovery_url": f"{ESIGNET}/.well-known/openid-configuration",
        "subject_auth_client_id": CLIENT_ID,
        "subject_auth_private_key_pem": pem,
        "subject_auth_redirect_uri": "",  # a beneficiary API has no portal callback
        "bene_auth_enabled": True,
        "bene_auth_redirect_uri": BENE_REDIRECT,
        "bene_auth_allowed_return_urls": ["https://consent.test/"],
    }.items():
        monkeypatch.setattr(config, name, value)
    # The fake eSignet checks the token request's redirect_uri against this.
    monkeypatch.setattr(test_subject_auth, "REDIRECT", BENE_REDIRECT)
    fake = FakeEsignet(keys)
    svc = SubjectAuthService.get_component()
    monkeypatch.setattr(svc, "http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    svc._metadata, svc._jwks, svc._key = None, {}, None
    yield fake
    svc._metadata, svc._jwks, svc._key = None, {}, None


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def start(raw, return_url=PAGE):
    r = raw(f"{AUTH}/login", {"return_url": return_url} if return_url else None)
    assert r.status_code == 302, r.text
    return r.headers["location"]


def back(raw, esignet, fin=FIN):
    """Login → eSignet → callback; the page URL the browser is sent back to."""
    esignet.individual_id = fin
    query, _ = esignet.authorize(start(raw))
    r = raw(f"{AUTH}/callback", query)
    assert r.status_code == 302, r.text
    return r.headers["location"], query


def login(call, raw, esignet, fin=FIN):
    page, _ = back(raw, esignet, fin)
    code = _query(page)["login"]
    status, tok = call("POST", f"{AUTH}/token", json={"code": code})
    assert status == 200, tok
    return {"Authorization": f"Bearer {tok['access_token']}"}


@pytest.fixture
def farmer(call, raw, esignet):
    return login(call, raw, esignet)


@pytest.fixture
def binding(call, new_partner):
    return bind(call, new_partner(), FR, FR_SCOPES)


def raise_request(call, binding, fin=FIN, id_type="FAYDA_FAN"):
    status, req = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": {"type": id_type, "value": fin},
        "partner_id": binding,
        "purpose": {"code": "credit-assessment"},
        "requested_scopes": FR_SCOPES,
    })
    assert status == 201, req
    return req


# ── login ────────────────────────────────────────────────────────────────────


def test_login_redirects_to_esignet_with_pkce(raw, esignet):
    url = start(raw)
    assert url.startswith(f"{ESIGNET}/authorize?")
    q = _query(url)
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == BENE_REDIRECT
    assert q["code_challenge_method"] == "S256" and q["state"] and q["nonce"]


def test_login_token_and_me(call, raw, esignet):
    page, query = back(raw, esignet)
    assert page.startswith(PAGE + "?login=")
    code = _query(page)["login"]
    status, tok = call("POST", f"{AUTH}/token", json={"code": code})
    assert status == 200, tok
    assert tok["subject_id"] == {"type": "FAYDA_FAN", "value": FIN} and tok["name"] == "Abebe Kebede"
    claims = jwt.decode(tok["access_token"], options={"verify_signature": False})
    assert claims["sub"] == FIN and claims["exp"] - claims["iat"] == 1800
    status, me = call("GET", f"{AUTH}/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert status == 200 and me["subject_id"]["value"] == FIN and me["acr"]
    # The code works once; the state too.
    assert call("POST", f"{AUTH}/token", json={"code": code})[0] == 400
    assert raw(f"{AUTH}/callback", query).status_code == 400


def test_return_url_must_be_allowed(raw, esignet):
    r = raw(f"{AUTH}/login", {"return_url": "https://evil.test/steal"})
    assert r.status_code == 400


def test_disabled_login_is_404(raw, esignet, monkeypatch):
    from openg2p_consent_manager.config import Settings

    monkeypatch.setattr(Settings.get_config(), "bene_auth_enabled", False)
    assert raw(f"{AUTH}/login", {"return_url": PAGE}).status_code == 404


def test_provider_without_national_id_returns_error_to_page(raw, esignet):
    esignet.userinfo_overrides = {"individual_id": None}
    page, _ = back(raw, esignet)
    q = _query(page)
    assert "login" not in q and "individual_id" in q["login_error"]


def test_userinfo_signed_with_published_key_without_exp(call, raw, esignet):
    """eSignet 1.8: userinfo signed with a key it publishes, no exp, iss
    <issuer>/v1/esignet — accepted (aud still checked)."""
    sign = esignet._sign

    def no_exp(claims, key=None, kid="esignet-1"):
        if "individual_id" in claims:
            claims = {k: v for k, v in claims.items() if k != "exp"}
            claims["iss"] = f"{ESIGNET}/v1/esignet"
        return sign(claims, key, kid)

    esignet._sign = no_exp
    login(call, raw, esignet)
    esignet.userinfo_overrides = {"aud": "another-client"}
    page, _ = back(raw, esignet)
    assert "login_error" in _query(page)


def test_api_client_login_without_return_url(call, raw, esignet):
    """An API client (no page): the callback answers the one-time code as JSON."""
    query, _ = esignet.authorize(start(raw, return_url=None))
    r = raw(f"{AUTH}/callback", query)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] and body["expires_in"] == 120
    status, tok = call("POST", f"{AUTH}/token", json={"code": body["code"]})
    assert status == 200 and tok["subject_id"]["value"] == FIN
    assert raw(f"{AUTH}/callback", query).status_code == 400  # the state works once


def test_api_client_login_error_is_json(raw, esignet):
    esignet.userinfo_overrides = {"individual_id": None}
    query, _ = esignet.authorize(start(raw, return_url=None))
    r = raw(f"{AUTH}/callback", query)
    assert r.status_code == 400 and "individual_id" in r.json()["error"]


def test_bad_tokens(call, farmer):
    token = farmer["Authorization"].split()[1]
    head, body, sig = token.split(".")
    tampered = f"{head}.{body}.{sig[:-4]}AAAA"
    assert call("GET", f"{AUTH}/me", headers={"Authorization": f"Bearer {tampered}"})[0] == 401
    # A farmer token holds no staff role.
    assert call("GET", "/consent/v1/partners", headers=farmer)[0] == 403


# ── the farmer's own requests and consents ───────────────────────────────────


def test_farmer_approves_own_request(call, farmer, binding):
    req = raise_request(call, binding)
    status, page = call("GET", "/consent/v1/my/consent-requests", params={"status": "pending"},
                        headers=farmer)
    assert status == 200 and req["id"] in [r["id"] for r in page["items"]]
    status, got = call("GET", f"/consent/v1/consent-requests/{req['id']}", headers=farmer)
    assert status == 200 and got["status"] == "pending"

    # No separate /authenticate: the national-ID login is the authentication.
    status, art = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                       json={"granted_scopes": ["farm_details"]}, headers=farmer)
    assert status == 201, art
    assert art["effective_data_scopes"] == ["farm_details"]
    a = art["assurance"]
    assert a["method"] == "self_service" and a["subject_authenticated"] is True
    assert a["subject_authentication"]["provider"] == "National ID (eSignet)"
    assert a["subject_authentication"]["acr"] == "mosip:idp:acr:generated-code"

    status, mine = call("GET", "/consent/v1/my/consents", headers=farmer)
    assert art["consent_id"] in [c["consent_id"] for c in mine["items"]]
    status, rev = call("POST", f"/consent/v1/my/consents/{art['consent_id']}/revoke", json={},
                       headers=farmer)
    assert status == 200 and rev["status"] == "revoked"


def test_farmer_denies_own_request(call, farmer, binding):
    req = raise_request(call, binding)
    status, body = call("POST", f"/consent/v1/consent-requests/{req['id']}/deny", json={},
                        headers=farmer)
    assert status == 200 and body["status"] == "denied"
    status, _ = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                     json={"granted_scopes": FR_SCOPES}, headers=farmer)
    assert status == 409


def test_matched_across_national_id_types(call, farmer, binding):
    req = raise_request(call, binding, id_type="national_id")
    status, page = call("GET", "/consent/v1/my/consent-requests", headers=farmer)
    assert req["id"] in [r["id"] for r in page["items"]]


def test_other_farmers_request_is_hidden(call, farmer, binding):
    req = raise_request(call, binding, fin=OTHER_FIN)
    rid = req["id"]
    assert call("GET", f"/consent/v1/consent-requests/{rid}", headers=farmer)[0] == 404
    assert call("POST", f"/consent/v1/consent-requests/{rid}/approve",
                json={"granted_scopes": FR_SCOPES}, headers=farmer)[0] == 404
    assert call("POST", f"/consent/v1/consent-requests/{rid}/deny", json={}, headers=farmer)[0] == 404
    status, page = call("GET", "/consent/v1/my/consent-requests", headers=farmer)
    assert rid not in [r["id"] for r in page["items"]]
    # Another farmer's consent cannot be revoked either.
    status, art = call("POST", f"/consent/v1/consent-requests/{rid}/approve",
                       json={"granted_scopes": FR_SCOPES})  # the service caller (dev)
    assert status == 412  # no authentication recorded for that farmer
    # A farmer cannot raise requests.
    status, _ = call("POST", "/consent/v1/consent-requests", headers=farmer, json={
        "subject_id": {"type": "FAYDA_FAN", "value": FIN}, "partner_id": binding,
        "purpose": {"code": "credit-assessment"}, "requested_scopes": FR_SCOPES,
    })
    assert status == 403


def test_assisted_request_is_not_answered_by_the_farmer(call, run, farmer, binding):
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentRequest

    req = raise_request(call, binding)

    async def make_assisted():
        async with async_session()() as session:
            row = await session.get(ConsentRequest, req["id"])
            row.method = "assisted"
            await session.commit()

    run(make_assisted())
    status, body = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                        json={"granted_scopes": FR_SCOPES}, headers=farmer)
    assert status == 409 and "partner" in body["error"]
    status, page = call("GET", "/consent/v1/my/consent-requests", headers=farmer)
    assert req["id"] not in [r["id"] for r in page["items"]]


# ── the beneficiary API takes only the beneficiary API auth token ────────────


@pytest.fixture
def bene_api(monkeypatch):
    from openg2p_consent_manager.config import Settings

    monkeypatch.setattr(Settings.get_config(), "api_audience", "beneficiary")


def test_bene_api_refuses_other_tokens_on_subject_routes(call, esignet, bene_api, binding):
    """Another caller (here the dev identity, as a Keycloak token would be) gets
    403 on the subject's routes; raising a request stays a service call."""
    req = raise_request(call, binding)  # the service caller (dev)
    rid = req["id"]
    for method, path, body in [
        ("GET", "/consent/v1/my/consents", None),
        ("GET", "/consent/v1/my/consent-requests", None),
        ("GET", f"/consent/v1/consent-requests/{rid}", None),
        ("POST", f"/consent/v1/consent-requests/{rid}/approve", {"granted_scopes": FR_SCOPES}),
        ("POST", f"/consent/v1/consent-requests/{rid}/deny", {}),
        ("POST", "/consent/v1/consents/any-id/revoke", {}),
    ]:
        status, out = call(method, path, json=body) if body is not None else call(method, path)
        assert status == 403, (path, out)
        assert "beneficiary API login" in str(out)


def test_bene_api_works_with_the_bene_token(call, raw, esignet, bene_api, binding):
    farmer = login(call, raw, esignet)
    req = raise_request(call, binding)
    status, page = call("GET", "/consent/v1/my/consent-requests", headers=farmer)
    assert status == 200 and req["id"] in [r["id"] for r in page["items"]]
    status, art = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                       json={"granted_scopes": FR_SCOPES}, headers=farmer)
    assert status == 201, art
    status, rev = call("POST", f"/consent/v1/consents/{art['consent_id']}/revoke", json={},
                       headers=farmer)
    assert status == 200 and rev["status"] == "revoked"
