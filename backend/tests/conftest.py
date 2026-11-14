"""Backend integration tests for the Consent Manager.

They run the real services, controllers and migration against a Postgres
database (the models use JSONB). Point them at a throwaway database with:

    CM_TEST_DB_DATASOURCE=postgresql+asyncpg://postgres@localhost:5432/cm_test \
        pytest backend/tests

The database is wiped at the start of the session. If it is unreachable, the
tests are skipped. Partner signing keys and policies come from Partner
Management: here the verifier is replaced by an in-process one holding test
keys, and PM's policy API by an in-memory fake (``pm`` fixture).
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
os.environ["CONSENT_MANAGER_API_AUDIENCE"] = "all"
# Partner Management's policy API — served by FakePM through an httpx transport.
PM_URL = "http://pm.test"
os.environ["CONSENT_MANAGER_PARTNER_MGMT_API_URL"] = PM_URL
os.environ.setdefault("CONSENT_MANAGER_LOGGING_LEVEL", "WARNING")

# Shape of the tables this change alters, as created by the previous release:
# `consent_artefacts.object_jti` unique on its own, controller_id NOT NULL, no
# grants / decision_logs.data_controller. The session starts from this so the
# migration is exercised on legacy data.
LEGACY_DDL = [
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


class FakePM:
    """Partner Management's internal policy API (``GET /policies/{partner_id}
    [?data_controller=]``), in memory. ``grant`` / ``revoke`` change a
    partner's active policy (a new version) and drop the CM's cached answer for
    it, so a change takes effect at once; ``down`` makes PM unreachable."""

    def __init__(self):
        self.policies = {}  # pm partner id -> {data_controller or "": PolicyOut}
        self.versions = {}  # (pm partner id, data_controller or "") -> last version
        self.down = False
        self.max_age = None  # Cache-Control max-age to send, if any
        self.requests = []
        self.client = None  # the app's PmPolicyClient

    def handler(self, request):
        import httpx

        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("Partner Management is down", request=request)
        prefix = "/policies/"
        if not request.url.path.startswith(prefix):
            return httpx.Response(404, json={"errors": [{"code": "PM-404"}]})
        pid = request.url.path[len(prefix):]
        policies = self.policies.get(pid, {})
        if "data_controller" in request.url.params:
            wanted = ("data_controller", request.url.params["data_controller"])
            items = [policies[wanted]] if wanted in policies else []
        else:
            items = list(policies.values())
        if not items:
            return httpx.Response(404, json={"errors": [{"code": "PM-POLICY-404"}]})
        headers = {}
        if self.max_age is not None:
            headers["Cache-Control"] = f"public, max-age={self.max_age}"
        return httpx.Response(200, json={"partner_id": pid, "policies": items},
                              headers=headers)

    def grant(self, audience, data_controller, scopes=(), **fields):
        """Make ``fields`` the partner's active scope policy for
        ``data_controller``. Returns the new version."""
        from openg2p_consent_manager.services import pm_partner_id

        pid = pm_partner_id(audience)
        key = ("data_controller", data_controller)
        version = self.versions.get((pid, key), 0) + 1
        self.versions[(pid, key)] = version
        policy = {
            "id": str(uuid.uuid4()),
            "partner_id": pid,
            "data_controller": data_controller,
            "version": version,
            "status": "active",
            "allowed_data_scopes": list(scopes),
            "allowed_purposes": ["credit-assessment"],
            "allowed_subject_id_types": ["FAYDA_FAN"],
            "allowed_signing_algs": ["EdDSA"],
            "max_validity_duration": "P1Y",
            "fetch_type": "oneshot",
            "max_fetch_frequency": None,
            "data_life": None,
            "effective_from": datetime.now(timezone.utc).isoformat(),
            # Most tests sign their own consents (consent_jws); PM's default
            # is false.
            "allow_partner_consents": True,
        }
        policy.update(fields)
        self.policies.setdefault(pid, {})[key] = policy
        self._changed(audience)
        return version

    def revoke(self, audience, data_controller):
        from openg2p_consent_manager.services import pm_partner_id

        key = ("data_controller", data_controller)
        self.policies.get(pm_partner_id(audience), {}).pop(key, None)
        self._changed(audience)

    def _changed(self, audience):
        if self.client is not None:
            self.client.invalidate(audience)


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
    from openg2p_consent_manager.services import PmPolicyClient, VerificationService

    init = Initializer()

    async def reset_to_legacy():
        async with dbengine.get().begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            for ddl in LEGACY_DDL:
                await conn.execute(text(ddl))

    loop.run_until_complete(reset_to_legacy())
    # The app's migrate runs its coroutine with asyncio.run; run it on our loop
    # so pooled connections stay bound to one loop.
    with mock.patch("asyncio.run", loop.run_until_complete):
        init.migrate_database(None)

    import httpx

    keys = FakePartnerKeys()
    VerificationService.get_component().crypto_helper = keys
    pm = FakePM()
    pm.client = PmPolicyClient.get_component()
    pm.client.transport = httpx.MockTransport(pm.handler)
    return {"app": app_registry.get(), "keys": keys, "pm": pm, "loop": loop, "init": init}


@pytest.fixture(scope="session")
def pm_store(env):
    return env["pm"]


@pytest.fixture
def pm(pm_store):
    """The fake Partner Management; up again (and no max-age) after each test."""
    yield pm_store
    pm_store.down = False
    pm_store.max_age = None


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
    """A test partner: a signing key registered under its PM id (PARTNER_<AUD>)."""

    def __init__(self, env, audience=None):
        from cryptography.hazmat.primitives.asymmetric import ed25519

        from openg2p_consent_manager.services import pm_partner_id

        self.audience = audience or f"bank-{uuid.uuid4().hex[:8]}"
        self.pm_id = pm_partner_id(self.audience)
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
