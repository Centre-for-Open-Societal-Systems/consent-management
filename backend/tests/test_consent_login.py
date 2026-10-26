# ruff: noqa: F811  (fixtures imported from test_subject_auth are test parameters)
"""Consent login with national ID, separate from the beneficiary API auth: the
subject logs in at eSignet (faked as in test_subject_auth) for ONE consent
request; the national ID must be the request's subject; the token reads,
grants or declines only that request (not /my/*, not other requests)."""
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import test_subject_auth
from cryptography.hazmat.primitives import serialization
from test_grants import FR, FR_SCOPES, bind
from test_subject_auth import CLIENT_ID, ESIGNET, FakeEsignet, keys  # noqa: F401  (fixture)

CONSENT_REDIRECT = "https://consent.test/consent/v1/consent-login/callback"
PAGE = "https://consent.test/consent/"
FIN = "123456789012"
OTHER_FIN = "999988887777"
LOGIN = "/consent/v1/consent-login"


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
        "subject_auth_redirect_uri": "",
        "consent_login_enabled": True,
        "consent_login_redirect_uri": CONSENT_REDIRECT,
        "consent_login_allowed_return_urls": ["https://consent.test/"],
    }.items():
        monkeypatch.setattr(config, name, value)
    # The fake eSignet checks the token request's redirect_uri against this.
    monkeypatch.setattr(test_subject_auth, "REDIRECT", CONSENT_REDIRECT)
    fake = FakeEsignet(keys)
    svc = SubjectAuthService.get_component()
    monkeypatch.setattr(svc, "http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    svc._metadata, svc._jwks, svc._key = None, {}, None
    yield fake
    svc._metadata, svc._jwks, svc._key = None, {}, None


@pytest.fixture
def binding(call, new_partner):
    return bind(call, new_partner(), FR, FR_SCOPES)


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def raise_request(call, binding, fin=FIN, id_type="FAYDA_FAN"):
    status, req = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": {"type": id_type, "value": fin},
        "partner_id": binding,
        "purpose": {"code": "credit-assessment"},
        "requested_scopes": FR_SCOPES,
    })
    assert status == 201, req
    return req


def start(raw, request_id, return_url=None):
    params = {"request_id": request_id}
    if return_url:
        params["return_url"] = return_url
    r = raw(f"{LOGIN}/start", params)
    assert r.status_code == 302, r.text
    return r.headers["location"]


def callback(raw, esignet, request_id, fin=FIN, return_url=None):
    esignet.individual_id = fin
    query, _ = esignet.authorize(start(raw, request_id, return_url))
    return raw(f"{LOGIN}/callback", query), query


def consent_login(call, raw, esignet, request_id, fin=FIN):
    r, _ = callback(raw, esignet, request_id, fin)
    assert r.status_code == 200, r.text
    status, tok = call("POST", f"{LOGIN}/token", json={"code": r.json()["code"]})
    assert status == 200, tok
    return {"Authorization": f"Bearer {tok['access_token']}"}, tok


# ── login ────────────────────────────────────────────────────────────────────


def test_start_redirects_to_esignet_with_its_own_callback(call, raw, esignet, binding):
    req = raise_request(call, binding)
    q = _query(start(raw, req["id"]))
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == CONSENT_REDIRECT
    assert q["code_challenge_method"] == "S256" and q["state"] and q["nonce"]


def test_start_only_for_an_answerable_request(call, raw, run, esignet, binding, monkeypatch):
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.db import async_session
    from openg2p_consent_manager.models import ConsentRequest

    assert raw(f"{LOGIN}/start", {"request_id": "no-such-request"}).status_code == 404
    req = raise_request(call, binding)

    async def set_method(value):
        async with async_session()() as session:
            row = await session.get(ConsentRequest, req["id"])
            row.method = value
            await session.commit()

    run(set_method("assisted"))
    assert raw(f"{LOGIN}/start", {"request_id": req["id"]}).status_code == 409
    run(set_method(None))
    assert call("POST", f"/consent/v1/consent-requests/{req['id']}/deny", json={})[0] == 200
    assert raw(f"{LOGIN}/start", {"request_id": req["id"]}).status_code == 409  # not pending
    other = raise_request(call, binding)
    assert raw(f"{LOGIN}/start", {"request_id": other["id"],
                                  "return_url": "https://evil.test/x"}).status_code == 400
    # From a page: back to the page with the reason, not a JSON error.
    r = raw(f"{LOGIN}/start", {"request_id": req["id"], "return_url": PAGE + req["id"]})
    assert r.status_code == 302 and "login_error=" in r.headers["location"]
    assert r.headers["location"].startswith(PAGE + req["id"])
    monkeypatch.setattr(Settings.get_config(), "consent_login_enabled", False)
    assert raw(f"{LOGIN}/start", {"request_id": other["id"]}).status_code == 404


def test_api_client_login_token_for_one_request(call, raw, esignet, binding):
    req = raise_request(call, binding)
    r, query = callback(raw, esignet, req["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["consent_request_id"] == req["id"] and body["expires_in"] == 120
    status, tok = call("POST", f"{LOGIN}/token", json={"code": body["code"]})
    assert status == 200, tok
    assert tok["consent_request_id"] == req["id"] and tok["subject_id"]["value"] == FIN
    head = jwt.get_unverified_header(tok["access_token"])
    claims = jwt.decode(tok["access_token"], options={"verify_signature": False})
    assert head["typ"] == "consent-login+jwt" and claims["crid"] == req["id"]
    assert claims["exp"] - claims["iat"] == 600
    # Code and state work once.
    assert call("POST", f"{LOGIN}/token", json={"code": body["code"]})[0] == 400
    assert raw(f"{LOGIN}/callback", query).status_code == 400


def test_page_login_returns_to_the_page(call, raw, esignet, binding):
    req = raise_request(call, binding)
    page = PAGE + req["id"]
    r, _ = callback(raw, esignet, req["id"], return_url=page)
    assert r.status_code == 302 and r.headers["location"].startswith(page + "?login=")


def test_another_national_id_is_refused(call, raw, esignet, binding):
    req = raise_request(call, binding, fin=OTHER_FIN)
    r, _ = callback(raw, esignet, req["id"], fin=FIN)
    assert r.status_code == 400 and "not the request's subject" in r.json()["error"]


# ── what the consent login token may do ──────────────────────────────────────


def test_consent_login_grants_its_request(call, raw, esignet, binding):
    req = raise_request(call, binding, id_type="national_id")  # matched across ID types
    headers, _ = consent_login(call, raw, esignet, req["id"])
    status, got = call("GET", f"/consent/v1/consent-requests/{req['id']}", headers=headers)
    assert status == 200 and got["status"] == "pending"
    status, art = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                       json={"granted_scopes": ["farm_details"]}, headers=headers)
    assert status == 201, art
    a = art["assurance"]
    assert a["method"] == "self_service" and a["subject_authenticated"] is True
    assert a["subject_authentication"]["login"] == "consent_login"
    assert a["subject_authentication"]["provider"] == "National ID (eSignet)"


def test_consent_login_declines_its_request(call, raw, esignet, binding):
    req = raise_request(call, binding)
    headers, _ = consent_login(call, raw, esignet, req["id"])
    status, body = call("POST", f"/consent/v1/consent-requests/{req['id']}/deny", json={}, headers=headers)
    assert status == 200 and body["status"] == "denied"


def test_consent_login_is_only_for_its_request(call, raw, esignet, binding):
    req = raise_request(call, binding)
    mine_too = raise_request(call, binding)  # same farmer, another request
    headers, _ = consent_login(call, raw, esignet, req["id"])
    for method, path, body in [
        ("GET", f"/consent/v1/consent-requests/{mine_too['id']}", None),
        ("POST", f"/consent/v1/consent-requests/{mine_too['id']}/approve", {"granted_scopes": FR_SCOPES}),
        ("POST", f"/consent/v1/consent-requests/{mine_too['id']}/deny", {}),
        ("GET", "/consent/v1/my/consents", None),
        ("GET", "/consent/v1/my/consent-requests", None),
        ("POST", "/consent/v1/consents/any-id/revoke", {}),
        ("GET", "/consent/v1/beneficiary/auth/me", None),
    ]:
        status, out = call(method, path, json=body, headers=headers)
        assert status == 403, (path, out)
    # A subject cannot raise requests; a consent login holds no staff role.
    status, _ = call("POST", "/consent/v1/consent-requests", headers=headers, json={
        "subject_id": {"type": "FAYDA_FAN", "value": FIN}, "partner_id": binding,
        "purpose": {"code": "credit-assessment"}, "requested_scopes": FR_SCOPES,
    })
    assert status == 403
    assert call("GET", "/consent/v1/partners", headers=headers)[0] == 403


def test_bad_consent_login_token(call, raw, esignet, binding):
    req = raise_request(call, binding)
    headers, _ = consent_login(call, raw, esignet, req["id"])
    head, body, sig = headers["Authorization"].split()[1].split(".")
    tampered = {"Authorization": f"Bearer {head}.{body}.{sig[:-4]}AAAA"}
    assert call("GET", f"/consent/v1/consent-requests/{req['id']}", headers=tampered)[0] == 401


def test_bene_api_takes_the_consent_login_token(call, raw, esignet, binding, monkeypatch):
    """On the beneficiary API (consent login on, bene API auth off) the request
    routes take only a national-ID login token."""
    from openg2p_consent_manager.config import Settings

    monkeypatch.setattr(Settings.get_config(), "api_audience", "beneficiary")
    req = raise_request(call, binding)  # the service caller (dev)
    assert call("GET", f"/consent/v1/consent-requests/{req['id']}")[0] == 403  # not a subject
    headers, _ = consent_login(call, raw, esignet, req["id"])
    assert call("GET", f"/consent/v1/consent-requests/{req['id']}", headers=headers)[0] == 200
