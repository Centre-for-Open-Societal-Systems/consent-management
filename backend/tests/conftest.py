"""Backend integration tests for the Consent Manager.

They run the real services, controllers and migration against a Postgres
database (the models use JSONB). Point them at a throwaway database with:

    CM_TEST_DB_DATASOURCE=postgresql+asyncpg://postgres@localhost:5432/cm_test \
        pytest backend/tests

The database is wiped at the start of the session. If it is unreachable, the
tests are skipped. Partner signing keys normally come from Partner Management;
here the verifier is replaced by an in-process one holding test keys.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DATASOURCE = os.environ.get(
    "CM_TEST_DB_DATASOURCE", "postgresql+asyncpg://postgres@localhost:5432/cm_test"
)

# Settings are read once at import — set them before importing the app.
os.environ["CONSENT_MANAGER_DB_DATASOURCE"] = DATASOURCE
os.environ["CONSENT_MANAGER_AUTH_ENABLED"] = "false"
os.environ["CONSENT_MANAGER_AWE_ENABLED"] = "false"
os.environ["CONSENT_MANAGER_API_AUDIENCE"] = "all"
os.environ["CONSENT_MANAGER_PARTNER_CACHE_ENABLED"] = "true"
os.environ.setdefault("CONSENT_MANAGER_LOGGING_LEVEL", "WARNING")

# Shape of the tables this change alters, as created by the previous release:
# `partners.audience` unique on its own, `consent_artefacts.object_jti` unique on
# its own, controller_id NOT NULL, no grants / decision_logs.data_controller.
# The session starts from this so the migration is exercised on legacy data.
LEGACY_DDL = [
    """CREATE TABLE partners (
        id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
        name VARCHAR(255), audience VARCHAR(255) NOT NULL, controller_id VARCHAR(255) NOT NULL,
        status VARCHAR(20) NOT NULL, partner_mgmt_id VARCHAR(255))""",
    "CREATE UNIQUE INDEX ix_partners_audience ON partners (audience)",
    "CREATE INDEX ix_partners_controller_id ON partners (controller_id)",
    """CREATE TABLE consent_artefacts (
        id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
        subject_id_type VARCHAR(50) NOT NULL, subject_id_value VARCHAR(255) NOT NULL,
        controller_id VARCHAR(255) NOT NULL, partner_id VARCHAR NOT NULL,
        purpose JSONB NOT NULL, data_scopes JSONB NOT NULL, effective_data_scopes JSONB NOT NULL,
        fetch_type VARCHAR(20) NOT NULL, valid_from TIMESTAMPTZ NOT NULL,
        valid_until TIMESTAMPTZ NOT NULL, source VARCHAR(20) NOT NULL, policy_version INTEGER,
        auth_context_id VARCHAR, object_jti VARCHAR(255) UNIQUE, status VARCHAR(20) NOT NULL,
        revoked_at TIMESTAMPTZ, expired_at TIMESTAMPTZ)""",
    """CREATE TABLE consent_requests (
        id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
        subject_id_type VARCHAR(50) NOT NULL, subject_id_value VARCHAR(255) NOT NULL,
        controller_id VARCHAR(255) NOT NULL, partner_id VARCHAR NOT NULL, purpose JSONB NOT NULL,
        requested_scopes JSONB NOT NULL, valid_from TIMESTAMPTZ, valid_until TIMESTAMPTZ,
        status VARCHAR(20) NOT NULL)""",
    """CREATE TABLE decision_logs (
        id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ,
        partner_id VARCHAR, consent_id VARCHAR, object_jti VARCHAR(255),
        decision VARCHAR(10) NOT NULL, reason_code VARCHAR(40) NOT NULL, detail TEXT,
        policy_version INTEGER, request_ctx_hash VARCHAR(128))""",
]

# A partner bound before the migration (legacy single binding + policy).
LEGACY_AUDIENCE = "legacy-bank"
LEGACY_CONTROLLER = "farmer-registry"
LEGACY_PARTNER_ID = "legacy-binding-1"


class FakePartnerKeys:
    """Stands in for the Partner-Management-backed crypto helper: verifies a
    compact JWS against a test public key registered per PM reference id."""

    def __init__(self):
        self.keys = {}

    def register(self, km_ref_id, public_key):
        self.keys[km_ref_id] = public_key

    async def verify_jwt(self, jws, km_ref_id=None, **_):
        from jwt.api_jws import PyJWS

        key = self.keys.get(km_ref_id)
        if key is None:
            return False
        try:
            PyJWS().decode(jws, key, algorithms=["EdDSA"])
            return True
        except Exception:
            return False


def _db_reachable() -> bool:
    import asyncpg

    url = DATASOURCE.replace("postgresql+asyncpg://", "postgresql://")

    async def ping():
        conn = await asyncpg.connect(url, timeout=3)
        await conn.close()

    try:
        asyncio.run(ping())
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture(scope="session")
def env(loop):
    """Initialise the app on a legacy-shaped database, then run the migration."""
    if not _db_reachable():
        pytest.skip(f"test database not reachable at {DATASOURCE}")

    from unittest import mock

    from openg2p_fastapi_common.context import app_registry, dbengine
    from sqlalchemy import text

    from openg2p_consent_manager.app import Initializer
    from openg2p_consent_manager.services import VerificationService

    init = Initializer()

    async def reset_to_legacy():
        async with dbengine.get().begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            for ddl in LEGACY_DDL:
                await conn.execute(text(ddl))
            now = datetime.now(timezone.utc)
            await conn.execute(
                text(
                    "INSERT INTO partners (id, created_at, name, audience, controller_id, "
                    "status, partner_mgmt_id) VALUES (:id, :now, 'Legacy Bank', :aud, :ctl, "
                    "'active', 'PM_LEGACY')"
                ),
                {"id": LEGACY_PARTNER_ID, "now": now, "aud": LEGACY_AUDIENCE,
                 "ctl": LEGACY_CONTROLLER},
            )

    loop.run_until_complete(reset_to_legacy())
    # The app's migrate runs its coroutine with asyncio.run; run it on our loop
    # so pooled connections stay bound to one loop.
    with mock.patch("asyncio.run", loop.run_until_complete):
        init.migrate_database(None)

    # The legacy partner's policy (created after migrate: partner_policies is new
    # to this DB, but its shape is unchanged).
    async def legacy_policy():
        from openg2p_consent_manager.db import async_session
        from openg2p_consent_manager.models import PartnerPolicy

        async with async_session()() as session:
            session.add(PartnerPolicy(
                partner_id=LEGACY_PARTNER_ID, version=1, status="active",
                allowed_data_scopes=["farmer_personal_details", "farm_details"],
                allowed_purposes=[], allowed_subject_id_types=[],
                allowed_signing_algs=["EdDSA"], fetch_type="oneshot",
                effective_from=datetime.now(timezone.utc),
            ))
            await session.commit()

    loop.run_until_complete(legacy_policy())

    keys = FakePartnerKeys()
    VerificationService.get_component().crypto_helper = keys
    return {"app": app_registry.get(), "keys": keys, "loop": loop, "init": init}


@pytest.fixture(scope="session")
def call(env):
    """``call(method, path, json=...)`` → (status, body) against the ASGI app."""
    import httpx

    transport = httpx.ASGITransport(app=env["app"])

    def _call(method, path, json=None, params=None, headers=None, content=None):
        async def go():
            async with httpx.AsyncClient(transport=transport, base_url="http://cm") as c:
                r = await c.request(
                    method, path, json=json, params=params, headers=headers,
                    content=content,
                )
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                return r.status_code, body

        return env["loop"].run_until_complete(go())

    return _call


@pytest.fixture(scope="session")
def run(env):
    return env["loop"].run_until_complete


class Partner:
    """A test partner: a signing key registered under its PM reference."""

    def __init__(self, env, audience=None, pm_id=None):
        from cryptography.hazmat.primitives.asymmetric import ed25519

        self.audience = audience or f"bank-{uuid.uuid4().hex[:8]}"
        self.pm_id = pm_id or f"PM_{self.audience}"
        self.key = ed25519.Ed25519PrivateKey.generate()
        env["keys"].register(self.pm_id, self.key.public_key())

    def sign(self, claims: dict) -> str:
        import json

        from jwt.api_jws import PyJWS

        payload = json.dumps(claims, sort_keys=True, separators=(",", ":"), default=str)
        return PyJWS().encode(payload.encode(), self.key, algorithm="EdDSA",
                              headers={"kid": "k1"})

    def claims(self, subject_value="123456789012", subject_type="FAYDA_FAN", **extra):
        now = datetime.now(timezone.utc)
        base = {
            "jti": str(uuid.uuid4()),
            "aud": self.audience,
            "subject_id": {"type": subject_type, "value": subject_value},
            "purpose": {"code": "credit-assessment"},
            "fetch_type": "oneshot",
            "validity": {
                "valid_from": (now - timedelta(minutes=1)).isoformat(),
                "valid_until": (now + timedelta(days=30)).isoformat(),
            },
            "issued_at": now.isoformat(),
        }
        base.update(extra)
        return base


@pytest.fixture
def new_partner(env):
    return lambda **kw: Partner(env, **kw)
