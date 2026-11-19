"""Partner policies read from Partner Management (G2P-5719): the cached PM
policy client (soft / hard / negative TTL, single-flight) and /validate when
PM is unreachable (last known answer, then fail closed)."""
import asyncio

import httpx
import pytest
from test_grants import FR, FR_SCOPES, bind, validate

POLICY = {
    "id": "p1", "partner_id": "PARTNER_BANK_X", "data_controller": FR, "version": 3,
    "status": "active", "allowed_data_scopes": FR_SCOPES,
    "allowed_purposes": [], "allowed_subject_id_types": [], "allowed_signing_algs": ["EdDSA"],
    "max_validity_duration": "P1Y", "fetch_type": "oneshot", "max_fetch_frequency": None,
    "data_life": None, "effective_from": "2026-10-01T00:00:00Z",
    "allow_partner_consents": True,
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class PM:
    """A scriptable PM policy endpoint."""

    def __init__(self):
        self.status = 200
        self.down = False
        self.max_age = None
        self.calls = 0
        self.delay = 0.0

    async def handler(self, request):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": [{"code": "PM-POLICY-404"}]})
        headers = {"Cache-Control": f"public, max-age={self.max_age}"} if self.max_age else {}
        return httpx.Response(200, json={"partner_id": "PARTNER_BANK_X", "policies": [POLICY]},
                              headers=headers)


@pytest.fixture
def client(env):
    """A separate client (after the app's, which get_component() keeps
    returning) with its own clock and PM."""
    from openg2p_consent_manager.services import PmPolicyClient

    clock, pm = Clock(), PM()
    c = PmPolicyClient(name="pm-policy-client-test", clock=clock)
    c.transport = httpx.MockTransport(pm.handler)
    c.clock, c.pm = clock, pm
    return c


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_maps_policy_out(client):
    policy = run(client.get_policy("bank-x", FR))
    assert policy.version == 3 and policy.data_controller == FR
    assert policy.allowed_data_scopes == FR_SCOPES
    assert policy.max_validity_duration == "P1Y" and policy.fetch_type == "oneshot"
    # Another controller in the answer is not this controller's policy.
    assert run(client.get_policy("bank-x", "other-registry")) is None


def test_soft_ttl_is_min_of_setting_and_max_age(client):
    run(client.get_policy("bank-x", FR))
    client.clock.now += 59
    run(client.get_policy("bank-x", FR))
    assert client.pm.calls == 1  # within the 60 s setting
    client.clock.now += 2
    run(client.get_policy("bank-x", FR))
    assert client.pm.calls == 2

    client.pm.max_age = 10
    client.invalidate()
    run(client.get_policy("bank-x", FR))
    client.clock.now += 11
    run(client.get_policy("bank-x", FR))
    assert client.pm.calls == 4  # PM's max-age (10 s) wins


def test_hard_ttl_serves_last_answer_then_fails_closed(client):
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import PolicyUnavailable

    hard = Settings.get_config().partner_policy_hard_ttl_seconds
    run(client.get_policy("bank-x", FR))
    client.pm.down = True
    client.clock.now += 61
    assert run(client.get_policy("bank-x", FR)).version == 3  # stale, PM down
    calls = client.pm.calls
    client.clock.now += 5
    assert run(client.get_policy("bank-x", FR)).version == 3
    assert client.pm.calls == calls  # no retry before the negative TTL
    client.clock.now += hard
    with pytest.raises(PolicyUnavailable):
        run(client.get_policy("bank-x", FR))
    # Recovers when PM is back.
    client.pm.down = False
    client.clock.now += 31
    assert run(client.get_policy("bank-x", FR)).version == 3


def test_nothing_cached_and_pm_down_fails_closed(client):
    from openg2p_consent_manager.services import PolicyUnavailable

    client.pm.down = True
    with pytest.raises(PolicyUnavailable):
        run(client.get_policy("bank-x", FR))
    client.pm.status = 500
    client.pm.down = False
    client.clock.now += 31
    with pytest.raises(PolicyUnavailable):
        run(client.get_policy("bank-x", FR))


def test_404_is_negative_cached(client):
    client.pm.status = 404
    assert run(client.get_policy("bank-x", FR)) is None
    assert run(client.get_policy("bank-x", FR)) is None
    assert client.pm.calls == 1
    client.pm.status = 200
    client.clock.now += 31
    assert run(client.get_policy("bank-x", FR)).version == 3


def test_single_flight(client):
    client.pm.delay = 0.05

    async def many():
        return await asyncio.gather(*(client.get_policy("bank-x", FR) for _ in range(5)))

    assert {p.version for p in run(many())} == {3}
    assert client.pm.calls == 1


def test_list_policies_and_controller_query(client):
    seen = []
    original = client.pm.handler

    async def spy(request):
        seen.append(request)
        return await original(request)

    client.transport = httpx.MockTransport(spy)
    assert [p.version for p in run(client.list_policies("bank-x"))] == [3]
    assert "data_controller" not in seen[-1].url.params
    run(client.get_policy("bank-x", FR))
    assert seen[-1].url.path == "/policies/PARTNER_BANK_X"
    assert dict(seen[-1].url.params) == {"data_controller": FR}


def test_policies_without_a_data_controller_are_left_out(client):
    """PM may hold other kinds of policy (no data controller); they are not
    the Consent Manager's: list_policies leaves them out."""
    other = {**POLICY, "data_controller": None, "allowed_data_scopes": [], "version": 2}

    async def handler(request):
        return httpx.Response(200, json={"partner_id": "PARTNER_BANK_Y",
                                         "policies": [POLICY, other]})

    client.transport = httpx.MockTransport(handler)
    assert [p.data_controller for p in run(client.list_policies("bank-y"))] == [FR]


# ── /validate with PM unreachable ────────────────────────────────────────────


def test_validate_serves_cached_policy_while_pm_is_down(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    assert validate(call, p.sign(p.claims(data_controller=FR, data_scopes=FR_SCOPES)),
                    FR)["decision"] == "permit"
    # Expire the soft TTL, then take PM down: the last answer is still served.
    pm.client._cache = {k: type(v)(v.value, v.fetched_at - 120, v.last_attempt - 120, v.ttl)
                        for k, v in pm.client._cache.items()}
    pm.down = True
    dec = validate(call, p.sign(p.claims(data_controller=FR, data_scopes=FR_SCOPES)), FR)
    assert dec["decision"] == "permit", dec


def test_validate_fails_closed_when_pm_is_down_and_nothing_cached(call, pm, new_partner):
    p = new_partner()
    bind(pm, p, FR, FR_SCOPES)
    pm.down = True
    dec = validate(call, p.sign(p.claims(data_controller=FR, data_scopes=FR_SCOPES)), FR)
    assert dec["decision"] == "deny" and dec["reason_code"] == "policy_unavailable", dec
