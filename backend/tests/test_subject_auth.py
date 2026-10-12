# ruff: noqa: F811  (fixtures imported from test_assisted_consent are test parameters)
"""Consent scenario 1, phase 2: the subject authenticates with national ID
(eSignet) during an assisted request. eSignet is faked over httpx: discovery,
JWKS, the token endpoint (which checks the client assertion and PKCE) and a
signed userinfo answer."""
import base64
import hashlib
import json
import secrets
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_assisted_consent import (  # noqa: F401  (fixtures)
    PDF,
    STAFF,
    as_partner,
    bank,
    create,
    portal,
    realm_key,
    upload,
)

ESIGNET = "https://esignet.test"
CLIENT_ID = "cm-consent-partner"
REDIRECT = "https://consent-partner-portal.test/consent/v1/partner-portal/subject-auth/callback"
SUBJECT = "123456789012"  # the request's FAYDA_FAN in create()


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def keys():
    return SimpleNamespace(provider=_rsa(), client=_rsa(), other=_rsa())


def _jwk(public_key, kid):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    d.update(kid=kid, use="sig", alg="RS256")
    return d


class FakeEsignet:
    """Answers the CM's calls; ``logins`` maps an issued code to its login."""

    def __init__(self, keys):
        self.keys = keys
        self.logins = {}
        self.calls = []
        # What the next token / userinfo answer looks like (tests change it).
        self.individual_id = SUBJECT
        self.id_overrides = {}
        self.userinfo_overrides = {}
        self.signing_key = keys.provider

    def authorize(self, url):
        """What the subject's browser does at eSignet: returns the callback query."""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        code = secrets.token_urlsafe(16)
        self.logins[code] = q
        return {"state": q["state"], "code": code}, q

    def _sign(self, claims):
        return jwt.encode(claims, self.signing_key, algorithm="RS256", headers={"kid": "esignet-1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ESIGNET,
                "authorization_endpoint": f"{ESIGNET}/authorize",
                "token_endpoint": f"{ESIGNET}/v1/esignet/oauth/v2/token",
                "userinfo_endpoint": f"{ESIGNET}/v1/esignet/oidc/userinfo",
                "jwks_uri": f"{ESIGNET}/.well-known/jwks.json",
            })
        if path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [_jwk(self.keys.provider.public_key(), "esignet-1")]})
        if path == "/v1/esignet/oauth/v2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            login = self.logins.pop(form.get("code"), None)
            if login is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            # The client assertion: signed with the CM's registered key, for us.
            assertion = jwt.decode(
                form["client_assertion"], self.keys.client.public_key(), algorithms=["RS256"],
                audience=f"{ESIGNET}/v1/esignet/oauth/v2/token",
            )
            assert assertion["iss"] == assertion["sub"] == CLIENT_ID
            assert jwt.get_unverified_header(form["client_assertion"])["kid"] == CLIENT_ID
            # PKCE.
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == login["code_challenge"]
            assert form["redirect_uri"] == REDIRECT
            now = int(time.time())
            claims = {"iss": ESIGNET, "aud": CLIENT_ID, "sub": "pairwise-sub-1", "iat": now,
                      "exp": now + 300, "nonce": login["nonce"], "acr": "mosip:idp:acr:generated-code",
                      "amr": ["OTP"], "auth_time": now}
            claims.update(self.id_overrides)
            return httpx.Response(200, json={"id_token": self._sign(claims), "access_token": "at-1",
                                             "token_type": "Bearer"})
        if path == "/v1/esignet/oidc/userinfo":
            assert request.headers["Authorization"] == "Bearer at-1"
            info = {"iss": ESIGNET, "aud": CLIENT_ID, "sub": "pairwise-sub-1",
                    "individual_id": self.individual_id, "name": "Abebe Kebede", "exp": int(time.time()) + 300,
                    "phone_number": "+251900000000"}
            info.update(self.userinfo_overrides)
            return httpx.Response(200, content=self._sign(info), headers={"content-type": "application/jwt"})
        return httpx.Response(404)


@pytest.fixture
def esignet(portal, keys, monkeypatch):
    from openg2p_consent_manager.services import SubjectAuthService

    config = portal.config
    pem = keys.client.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    for name, value in {
        "subject_auth_enabled": True,
        "subject_auth_discovery_url": f"{ESIGNET}/.well-known/openid-configuration",
        "subject_auth_client_id": CLIENT_ID,
        "subject_auth_private_key_pem": pem,
        "subject_auth_redirect_uri": REDIRECT,
        "subject_auth_session_ttl_seconds": 300,
    }.items():
        monkeypatch.setattr(config, name, value)
    fake = FakeEsignet(keys)
    svc = SubjectAuthService.get_component()
    monkeypatch.setattr(svc, "http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    svc._metadata, svc._jwks, svc._key = None, {}, None
    yield fake
    svc._metadata, svc._jwks, svc._key = None, {}, None


@pytest.fixture
def callback(raw_html):
    def _cb(params):
        return raw_html("/consent/v1/partner-portal/subject-auth/callback", params)

    return _cb


@pytest.fixture
def raw_html(env):
    transport = httpx.ASGITransport(app=env["app"])

    def _get(path, params):
        async def go():
            async with httpx.AsyncClient(transport=transport, base_url="http://cm") as c:
                r = await c.get(path, params=params)
                return r.status_code, r.text

        return env["loop"].run_until_complete(go())

    return _get


def login(pc, rid, esignet, callback):
    status, started = pc("POST", f"/consent-requests/{rid}/subject-auth")
    assert status == 201, started
    query, _ = esignet.authorize(started["authorization_url"])
    status, page = callback(query)
    return started, status, page


def test_config_and_authorize_url(portal, esignet, as_partner, bank):
    pc = as_partner(bank.audience)
    status, cfg = pc("GET", "/subject-auth")
    assert status == 200 and cfg["enabled"] and "FAYDA_FAN" in cfg["id_types"]
    rid = create(pc)["id"]
    status, started = pc("POST", f"/consent-requests/{rid}/subject-auth")
    assert status == 201, started
    url = urlparse(started["authorization_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ESIGNET}/authorize"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == REDIRECT
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["acr_values"] == "mosip:idp:acr:generated-code"
    assert json.loads(q["claims"])["userinfo"]["individual_id"]["essential"] is True
    assert q["state"] and q["nonce"] and q["code_challenge"]
    status, s = pc("GET", f"/consent-requests/{rid}/subject-auth")
    assert status == 200 and s["status"] == "pending" and s["started_by"] == f"{bank.audience}-operator"


def test_authenticated_subject_then_submit_and_approve(call, portal, esignet, as_partner, bank, upload,
                                                        callback):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    assert upload(pc, rid)[0] == 201
    # Mandatory: the signed form alone is not enough.
    status, body = pc("POST", f"/consent-requests/{rid}/submit")
    assert status == 409 and "national ID" in body["error"]

    _, status, page = login(pc, rid, esignet, callback)
    assert status == 200 and "Authenticated" in page
    status, s = pc("GET", f"/consent-requests/{rid}/subject-auth")
    assert s["status"] == "success" and s["method"] == "OTP"
    assert s["acr"] == "mosip:idp:acr:generated-code" and s["authenticated_at"]

    status, req = pc("POST", f"/consent-requests/{rid}/submit")
    assert status == 200, req
    assert req["subject_authentication"]["status"] == "success"
    status, staff_view = call("GET", f"{STAFF}/{rid}")
    assert staff_view["subject_authentication"]["provider"] == "National ID (eSignet)"
    status, done = call("POST", f"{STAFF}/{rid}/approve", json={})
    assert status == 200, done
    status, consent = pc("GET", f"/consents/{done['consent_id']}")
    assurance = consent["assurance"]
    assert assurance["subject_authenticated"] is True
    assert assurance["evidence"] == ["signed_form"]
    sa = assurance["subject_authentication"]
    assert sa["provider"] == "National ID (eSignet)" and sa["method"] == "OTP"


def test_state_is_single_use_and_unknown_state_refused(portal, esignet, as_partner, bank, callback):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    status, started = pc("POST", f"/consent-requests/{rid}/subject-auth")
    query, _ = esignet.authorize(started["authorization_url"])
    assert callback(query)[0] == 200
    status, page = callback(query)
    assert status == 400 and "already used" in page
    assert callback({"state": "nope", "code": "x"})[0] == 400
    assert callback({"code": "x"})[0] == 400


def test_another_person_fails(portal, esignet, as_partner, bank, callback, upload):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    esignet.individual_id = "999999999999"
    _, status, page = login(pc, rid, esignet, callback)
    assert status == 200 and "not the request" in page
    s = pc("GET", f"/consent-requests/{rid}/subject-auth")[1]
    assert s["status"] == "failed" and "not the request" in s["error"]
    assert upload(pc, rid)[0] == 201
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 409
    # A new attempt by the right person succeeds; the failure stays in the history.
    esignet.individual_id = SUBJECT
    _, status, page = login(pc, rid, esignet, callback)
    assert "Authenticated" in page
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 200


@pytest.mark.parametrize("change, fragment", [
    ({"id": {"nonce": "other"}}, "nonce"),
    ({"id": {"aud": "someone-else"}}, "ID token is not valid"),
    ({"id": {"iss": "https://evil.test"}}, "ID token is not valid"),
    ({"id": {"exp": 1}}, "ID token is not valid"),
    ({"userinfo": {"sub": "another-pairwise"}}, "another person"),
    ({"signer": "other"}, "not valid"),
])
def test_token_checks(portal, esignet, keys, as_partner, bank, callback, change, fragment):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    esignet.id_overrides = change.get("id", {})
    esignet.userinfo_overrides = change.get("userinfo", {})
    if change.get("signer"):
        esignet.signing_key = keys.other
    _, status, page = login(pc, rid, esignet, callback)
    s = pc("GET", f"/consent-requests/{rid}/subject-auth")[1]
    assert s["status"] == "failed", s
    assert fragment in s["error"], s["error"]


def test_provider_refusal_and_expiry(portal, esignet, as_partner, bank, callback, monkeypatch):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    status, started = pc("POST", f"/consent-requests/{rid}/subject-auth")
    query, _ = esignet.authorize(started["authorization_url"])
    status, page = callback({"state": query["state"], "error": "access_denied",
                             "error_description": "User cancelled"})
    assert status == 200 and "User cancelled" in page
    assert pc("GET", f"/consent-requests/{rid}/subject-auth")[1]["status"] == "failed"

    monkeypatch.setattr(portal.config, "subject_auth_session_ttl_seconds", -1)
    status, started = pc("POST", f"/consent-requests/{rid}/subject-auth")
    assert pc("GET", f"/consent-requests/{rid}/subject-auth")[1]["status"] == "expired"
    query, _ = esignet.authorize(started["authorization_url"])
    assert callback(query)[0] == 400


def test_id_type_without_national_id_and_closed_requests(portal, esignet, as_partner, bank, callback,
                                                         monkeypatch):
    pc = as_partner(bank.audience)
    monkeypatch.setattr(portal.config, "subject_auth_id_types", ["NATIONAL_ID"])
    rid = create(pc)["id"]  # a FAYDA_FAN subject
    status, body = pc("POST", f"/consent-requests/{rid}/subject-auth")
    assert status == 422 and "FAYDA_FAN" in body["error"]
    monkeypatch.setattr(portal.config, "subject_auth_id_types", ["FAYDA_FAN"])
    rid = create(pc)["id"]
    status, started = pc("POST", f"/consent-requests/{rid}/subject-auth")
    pc("POST", f"/consent-requests/{rid}/cancel")
    query, _ = esignet.authorize(started["authorization_url"])
    assert callback(query)[0] == 200
    s = pc("GET", f"/consent-requests/{rid}/subject-auth")[1]
    assert s["status"] == "failed" and "no longer open" in s["error"]
    assert pc("POST", f"/consent-requests/{rid}/subject-auth")[0] == 409


def test_other_partner_cannot_start_or_see(portal, esignet, as_partner, bank, new_partner):
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    other = as_partner(new_partner().audience)
    assert other("POST", f"/consent-requests/{rid}/subject-auth")[0] == 404
    assert other("GET", f"/consent-requests/{rid}/subject-auth")[0] == 404


def test_disabled_by_default(portal, as_partner, bank, upload, callback):
    pc = as_partner(bank.audience)
    assert pc("GET", "/subject-auth")[1] == {"enabled": False, "provider_name": None, "id_types": []}
    rid = create(pc)["id"]
    assert pc("POST", f"/consent-requests/{rid}/subject-auth")[0] == 404
    assert callback({"state": "x", "code": "y"})[0] == 404
    # Form-only flow unchanged.
    assert upload(pc, rid)[0] == 201
    assert pc("POST", f"/consent-requests/{rid}/submit")[0] == 200


def test_client_key_missing_is_503(portal, esignet, as_partner, bank, monkeypatch):
    from openg2p_consent_manager.services import SubjectAuthService

    monkeypatch.setattr(portal.config, "subject_auth_private_key_pem", "")
    monkeypatch.setattr(portal.config, "subject_auth_private_key_path", "/nonexistent/client.pem")
    SubjectAuthService.get_component()._key = None
    pc = as_partner(bank.audience)
    rid = create(pc)["id"]
    status, body = pc("POST", f"/consent-requests/{rid}/subject-auth")
    assert status == 503 and "client key" in body["error"]
