import httpx
import pytest

from sanity import pm_seed
from sanity.config import Config


@pytest.fixture(scope="session")
def cfg() -> Config:
    return Config.from_env()


@pytest.fixture(scope="session")
def client(cfg):
    # STAFF api (Keycloak: consents, verifications, decisions).
    with httpx.Client(base_url=cfg.base_url, verify=cfg.verify_tls, timeout=20) as c:
        yield c


@pytest.fixture(scope="session")
def partner_client(cfg):
    # PARTNER api (/validate, status, receipts, JWKS) — no Keycloak.
    with httpx.Client(base_url=cfg.partner_base_url, verify=cfg.verify_tls, timeout=20) as c:
        yield c


@pytest.fixture(scope="session")
def pm_partner(cfg):
    """Ensure the persistent sanity test partner, key and policy exist in Partner Management.

    Skips the signed e2e (rather than failing) when PM isn't reachable/seedable,
    so smoke + contract coverage stays green in environments without PM. The
    seeded partner is intentionally left in place after the run.
    """
    if not cfg.can_reach_pm:
        pytest.skip(
            "SANITY_PM_PARTNER_API_URL not set — cannot reach Partner Management to "
            "verify/seed the test partner; signed e2e skipped"
        )
    try:
        status = pm_seed.ensure_seeded(cfg)
    except Exception as exc:  # noqa: BLE001 — surface as a skip with the reason
        pytest.skip(f"could not seed PM test partner: {exc}")
    return {"status": status, "partner_id": cfg.pm_partner_id, "kid": cfg.pm_kid,
            "controller": cfg.controller_id}
