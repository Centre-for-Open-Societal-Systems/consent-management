"""Idempotent seeding of the sanity test partner into Partner Management (PM).

CM stores no partner keys or policies, so the signed e2e needs a real partner
in PM whose public key matches the sanity's private key and which has an active
scope policy for the sanity controller. This module ensures both — creating
them once via PM's staff API and then leaving them in place (persistent test
fixture, never deleted).

Flow (all idempotent):
  1. If PM's partner API already serves the (partner_id, kid) key and a policy
     for the controller that matches SANITY_POLICY, we're done.
  2. If the key is not served: submit an onboarding request with the derived
     public key and the policy, and approve it → partner, key, policy active.
     If the partner already exists (409): enable it, then submit a key-update
     request for the kid and approve it.
  3. If the policy is still missing or differs: submit a policy request and
     approve it.
"""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

import httpx


def public_pem_and_alg(private_pem: str):
    """Derive the SPKI public PEM + JWS algorithm from a PEM private key."""
    priv = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    if isinstance(priv, ed25519.Ed25519PrivateKey):
        alg = "EdDSA"
    elif isinstance(priv, ec.EllipticCurvePrivateKey):
        alg = "ES256"
    elif isinstance(priv, rsa.RSAPrivateKey):
        alg = "RS256"
    else:
        raise ValueError("unsupported private key type for PM seeding")
    return pub_pem, alg


# Scopes the e2e asks for; its consents add one more scope outside the policy.
SANITY_SCOPES = ["farmer_profile.basic", "farmer_profile.crops"]
SANITY_PURPOSE = "share_farm_profile"
SANITY_SUBJECT_ID_TYPE = "national_id"


def desired_policy(cfg) -> dict:
    """The scope policy the e2e needs (a PM PolicyInput)."""
    return {
        "data_controller": cfg.controller_id,
        "allowed_data_scopes": SANITY_SCOPES,
        "allowed_purposes": [SANITY_PURPOSE],
        "allowed_subject_id_types": [SANITY_SUBJECT_ID_TYPE],
        "allowed_signing_algs": ["EdDSA", "ES256", "RS256"],
        "max_validity_duration": "P1Y",
        "fetch_type": "oneshot",
        "max_fetch_frequency": None,
        "data_life": None,
        # The e2e signs its own consents (consent_jws).
        "allow_partner_consents": True,
    }


def key_servable(cfg) -> bool:
    """True if PM's fetch API already serves the sanity (partner_id, kid)."""
    if not cfg.pm_partner_api_url:
        return False
    url = f"{cfg.pm_partner_api_url}/keys/{cfg.pm_partner_id}/{cfg.pm_kid}"
    r = httpx.get(url, verify=cfg.verify_tls, timeout=20)
    return r.status_code == 200


def _policy_matches(have: dict, want: dict) -> bool:
    for field in ("allowed_data_scopes", "allowed_purposes", "allowed_subject_id_types"):
        if set(have.get(field) or []) != set(want[field]):
            return False
    if not set(want["allowed_signing_algs"]) <= set(have.get("allowed_signing_algs") or []):
        return False
    if have.get("fetch_type") != want["fetch_type"]:
        return False
    if have.get("allow_partner_consents") is not True:
        return False
    return have.get("max_validity_duration") in (None, want["max_validity_duration"])


def policy_servable(cfg) -> bool:
    """True if PM serves an active policy for the controller that matches
    desired_policy (what CM will read at /validate time)."""
    if not cfg.pm_partner_api_url:
        return False
    r = httpx.get(
        f"{cfg.pm_partner_api_url}/policies/{cfg.pm_partner_id}",
        params={"data_controller": cfg.controller_id},
        verify=cfg.verify_tls,
        timeout=20,
    )
    if r.status_code != 200:
        return False
    want = desired_policy(cfg)
    return any(
        p.get("data_controller") == cfg.controller_id and _policy_matches(p, want)
        for p in r.json().get("policies", [])
    )


def _admin_token(cfg):
    """Obtain a client-credentials token for PM's admin API.

    Prefers dedicated PM admin creds (SANITY_PM_ADMIN_*); otherwise falls back to
    the sanity Keycloak client (SANITY_TOKEN_URL/CLIENT_ID/CLIENT_SECRET) — that
    client must then be granted the `partner_manager` role in the staff realm.
    Returns None only when no credentials are configured at all (PM auth off).
    """
    if cfg.pm_admin_token_url and cfg.pm_admin_client_secret:
        token_url, client_id, client_secret = (
            cfg.pm_admin_token_url, cfg.pm_admin_client_id, cfg.pm_admin_client_secret,
        )
    elif cfg.token_url and cfg.client_secret:
        token_url, client_id, client_secret = (
            cfg.token_url, cfg.client_id, cfg.client_secret,
        )
    else:
        return None
    r = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        verify=cfg.verify_tls,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _headers(token):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _check_auth(r, cfg, token):
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"PM admin API rejected the seed ({r.status_code}) at {r.request.url}. "
            f"The seed needs a Keycloak client with the 'partner_manager' role "
            f"(token_sent={token is not None}). Either set SANITY_PM_ADMIN_* to a "
            f"dedicated partner_manager client, or grant the sanity client "
            f"'{cfg.client_id}' the partner_manager role in the staff realm."
        )
    r.raise_for_status()


def _approve(cfg, token, request_id):
    a = httpx.post(
        f"{cfg.pm_admin_url}/partners/requests/{request_id}/approve",
        headers=_headers(token),
        json={"notes": "auto-approved by CM sanity seed"},
        verify=cfg.verify_tls,
        timeout=30,
    )
    _check_auth(a, cfg, token)


def ensure_seeded(cfg) -> str:
    """Ensure the sanity partner, key and policy exist and are servable in PM.

    Returns "exists" or the steps taken, e.g. "onboarded" or "key-added+policy".
    Raises RuntimeError if seeding is impossible (no admin URL) or did not
    result in a servable key and policy.
    """
    key_ok = key_servable(cfg)
    if key_ok and policy_servable(cfg):
        return "exists"
    if not cfg.pm_admin_url:
        raise RuntimeError(
            "PM key or policy not servable and SANITY_PM_ADMIN_URL not set — cannot "
            f"seed partner '{cfg.pm_partner_id}'"
        )

    token = _admin_token(cfg)
    admin = cfg.pm_admin_url
    steps = []

    if not key_ok:
        pub_pem, alg = public_pem_and_alg(cfg.pm_private_key_pem)
        key_input = {"public_key": pub_pem, "kid": cfg.pm_kid, "algorithm": alg}
        # Try a fresh onboarding request (partner + key + policy).
        r = httpx.post(
            f"{admin}/partners/requests/onboarding",
            headers=_headers(token),
            json={
                "partner_id": cfg.pm_partner_id,
                "name": "CM Sanity Test Partner",
                "org_name": "OpenG2P Consent Manager sanity suite",
                "description": "Persistent test partner for the CM signed-validate e2e. Test only.",
                "keys": [key_input],
                "policies": [desired_policy(cfg)],
            },
            verify=cfg.verify_tls,
            timeout=30,
        )
        if r.status_code == 409:
            # Partner already exists — make sure it is enabled and (re)add the key.
            httpx.post(
                f"{admin}/partners/{cfg.pm_partner_id}/enable",
                headers=_headers(token), verify=cfg.verify_tls, timeout=20,
            )
            r = httpx.post(
                f"{admin}/partners/requests/key-update",
                headers=_headers(token),
                json={"partner_id": cfg.pm_partner_id, "keys": [key_input]},
                verify=cfg.verify_tls,
                timeout=30,
            )
            steps.append("key-added")
        else:
            steps.append("onboarded")
        _check_auth(r, cfg, token)
        _approve(cfg, token, r.json()["id"])

        if not key_servable(cfg):
            raise RuntimeError(
                f"seeded partner '{cfg.pm_partner_id}' but key '{cfg.pm_kid}' is still "
                "not servable by PM's partner API"
            )

    if not policy_servable(cfg):
        r = httpx.post(
            f"{admin}/partners/requests/policy",
            headers=_headers(token),
            json={
                "partner_id": cfg.pm_partner_id,
                "description": "Scope policy for the CM sanity e2e. Test only.",
                "policies": [desired_policy(cfg)],
            },
            verify=cfg.verify_tls,
            timeout=30,
        )
        _check_auth(r, cfg, token)
        _approve(cfg, token, r.json()["id"])
        steps.append("policy")
        if not policy_servable(cfg):
            raise RuntimeError(
                f"raised a policy for partner '{cfg.pm_partner_id}' and controller "
                f"'{cfg.controller_id}' but PM's partner API does not serve it"
            )

    return "+".join(steps)


def main() -> int:
    """CLI entrypoint (`python -m sanity.pm_seed`) — used by the deploy-time seed
    Job. Idempotent: safe to run on every install/upgrade. Exits 0 on success or
    a benign skip (PM not configured); non-zero only on an unexpected failure."""
    from sanity.config import Config

    cfg = Config.from_env()
    if not cfg.can_reach_pm:
        print("[pm-seed] SANITY_PM_PARTNER_API_URL not set — nothing to seed; skipping")
        return 0
    try:
        status = ensure_seeded(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"[pm-seed] FAILED to seed partner '{cfg.pm_partner_id}': {exc}")
        return 1
    print(
        f"[pm-seed] partner '{cfg.pm_partner_id}' key '{cfg.pm_kid}' policy for "
        f"'{cfg.controller_id}': {status}"
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
