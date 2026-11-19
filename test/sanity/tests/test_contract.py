import pytest

# Contract: the audience split enforces the right auth model —
#   partner api (/validate): NO Keycloak (trust = the signed consent object);
#   staff api (/decisions):  Keycloak required.


@pytest.mark.contract
def test_validate_is_open(partner_client):
    # The partner api has no Keycloak wall — an unauthenticated /validate is NOT
    # rejected with 401; a malformed body is a clean 200 "deny" decision.
    r = partner_client.post("/consent/v1/validate", json={})
    assert r.status_code == 200, f"expected 200 deny, got {r.status_code}: {r.text}"
    assert r.json().get("decision") == "deny", r.text


@pytest.mark.contract
def test_admin_requires_auth(client, cfg):
    if not cfg.auth_enabled:
        pytest.skip("auth disabled")
    r = client.get("/consent/v1/decisions")
    assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}: {r.text}"
