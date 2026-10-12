"""AWE approval of policy widenings: submission, failure handling, the one-pending
rule, decisions arriving after the active policy changed, resubmission, the
first-policy flag, and the AWE client's error parsing / token / requester /
inbox handling. AWE itself is faked: ``create_request`` is replaced in-process,
or httpx is routed to a mock transport."""
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest

from openg2p_consent_manager.config import Settings
from openg2p_consent_manager.services import awe_client as awe_client_mod
from openg2p_consent_manager.services.awe_client import AweClient, AweClientError

CONTROLLER = "farmer-registry"
SECRET = "test-hmac-secret"
_config = Settings.get_config()


class FakeAwe:
    """Stands in for AweClient.create_request; records each call."""

    def __init__(self):
        self.calls = []
        self.fail = None  # an AweClientError to raise instead of succeeding

    async def create_request(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return f"req-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def awe_on(env, monkeypatch):
    monkeypatch.setattr(_config, "awe_enabled", True)
    monkeypatch.setattr(_config, "awe_gate_first_policy", True)
    monkeypatch.setattr(_config, "awe_callback_hmac_secret", SECRET)
    fake = FakeAwe()
    monkeypatch.setattr(AweClient.get_component(), "create_request", fake.create_request)
    return fake


@pytest.fixture
def binding(call):
    status, body = call("POST", "/consent/v1/partners", json={
        "audience": f"awe-{uuid.uuid4().hex[:8]}", "controller_id": CONTROLLER,
    })
    assert status == 201, body
    return body["id"]


BASE = {
    "allowed_data_scopes": ["farm_details"],
    "allowed_purposes": ["credit-assessment"],
    "allowed_signing_algs": ["EdDSA"],
    "max_validity_duration": "P90D",
}


def put_policy(call, binding_id, headers=None, **fields):
    payload = dict(BASE)
    payload.update(fields)
    return call("PUT", f"/consent/v1/partners/{binding_id}/policy", json=payload,
                headers=headers)


def versions(call, binding_id):
    status, body = call("GET", f"/consent/v1/partners/{binding_id}/policies")
    assert status == 200, body
    return {p["version"]: p for p in body}


def webhook(call, event_type, request_id, artifact_id, event_id=None, actor=None):
    body = json.dumps({
        "event_id": event_id or str(uuid.uuid4()), "event_type": event_type,
        "request_id": request_id, "artifact_type": "consent_manager.policy_change",
        "artifact_id": artifact_id, "status": "x", "stage_order": 1, "actor": actor,
        "occurred_at": "2026-01-01T00:00:00Z",
    }, separators=(",", ":")).encode()
    ts = int(time.time())
    sig = "sha256=" + hmac.new(SECRET.encode(), f"{ts}.".encode() + body,
                               hashlib.sha256).hexdigest()
    return call("POST", "/consent/v1/awe/webhooks/decision", content=body, headers={
        "Content-Type": "application/json",
        "X-Approval-Event-Id": json.loads(body)["event_id"],
        "X-Approval-Timestamp": str(ts),
        "X-Approval-Signature": sig,
    })


def activate_first(call, binding_id, awe):
    """First policy is gated: approve it so later tests have an active base."""
    status, v1 = put_policy(call, binding_id)
    assert status == 200 and v1["status"] == "pending", v1
    status, body = webhook(call, "request_approved", v1["awe_request_id"], v1["id"])
    assert status == 200 and body["status"] == "active", body
    return v1


# ── Submission ──────────────────────────────────────────────────────────────


def test_widening_is_submitted_with_requester_and_callers_bearer(call, awe_on, binding):
    status, v1 = put_policy(call, binding, headers={"Authorization": "Bearer admin-jwt"})
    assert status == 200, v1
    assert v1["status"] == "pending" and v1["awe_request_id"].startswith("req-")
    assert v1["base_version"] == 0
    sent = awe_on.calls[-1]
    assert sent["artifact_type"] == "consent_manager.policy_change"
    assert sent["artifact_id"] == v1["id"]
    assert sent["caller_bearer"] == "admin-jwt"
    assert sent["requester"] == "dev"  # auth disabled → the synthetic dev identity
    assert sent["context"]["policy_version"] == 1


def test_submit_failure_marks_version_failed_then_resubmit(call, awe_on, binding):
    activate_first(call, binding, awe_on)
    awe_on.fail = AweClientError(404, "No active policy for 'consent-manager.policy_change.v1'")
    status, body = put_policy(call, binding, allowed_purposes=["credit-assessment", "insurance"])
    assert status == 502, body
    assert body["error"] == "awe_submit_failed" and body["status"] == "failed"
    assert "No active policy" in body["message"]

    v = versions(call, binding)
    assert v[2]["status"] == "failed"
    assert "No active policy" in v[2]["status_reason"]
    assert v[1]["status"] == "active"  # the active policy is untouched
    assert not any(p["status"] == "pending" for p in v.values())

    # A failed version doesn't block new submissions, and can be resubmitted.
    awe_on.fail = None
    status, v3 = call("POST", f"/consent/v1/partners/{binding}/policies/2/resubmit")
    assert status == 200, v3
    assert v3["version"] == 3 and v3["status"] == "pending" and v3["awe_request_id"]
    assert v3["allowed_purposes"] == ["credit-assessment", "insurance"]


def test_only_failed_stale_or_rejected_can_be_resubmitted(call, awe_on, binding):
    activate_first(call, binding, awe_on)
    status, body = call("POST", f"/consent/v1/partners/{binding}/policies/1/resubmit")
    assert status == 409 and body["error"] == "not_resubmittable", body
    status, _ = call("POST", f"/consent/v1/partners/{binding}/policies/99/resubmit")
    assert status == 404


# ── One pending version per binding ─────────────────────────────────────────


def test_second_widening_while_one_is_pending_is_rejected(call, awe_on, binding):
    activate_first(call, binding, awe_on)
    status, v2 = put_policy(call, binding, allowed_data_scopes=["farm_details", "land"])
    assert status == 200 and v2["status"] == "pending", v2
    status, body = put_policy(call, binding, allowed_purposes=[])
    assert status == 409, body
    assert body["error"] == "policy_pending" and body["pending_version"] == 2
    # A narrowing still applies immediately.
    status, v3 = put_policy(call, binding, max_validity_duration="P30D")
    assert status == 200 and v3["status"] == "active", v3


# ── Decisions ───────────────────────────────────────────────────────────────


def test_approval_activates_and_supersedes(call, awe_on, binding):
    activate_first(call, binding, awe_on)
    status, v2 = put_policy(call, binding, allowed_data_scopes=["farm_details", "land"])
    event_id = str(uuid.uuid4())
    status, body = webhook(call, "request_approved", v2["awe_request_id"], v2["id"],
                           event_id=event_id)
    assert status == 200 and body["status"] == "active", body
    v = versions(call, binding)
    assert v[2]["status"] == "active" and v[1]["status"] == "superseded"
    # A re-delivered webhook is acknowledged without re-applying.
    status, body = webhook(call, "request_approved", v2["awe_request_id"], v2["id"],
                           event_id=event_id)
    assert status == 200 and body["status"] == "duplicate", body


def test_approval_after_a_narrowing_is_not_applied(call, awe_on, binding):
    activate_first(call, binding, awe_on)
    status, v2 = put_policy(call, binding, allowed_data_scopes=["farm_details", "land"])
    assert v2["status"] == "pending" and v2["base_version"] == 1
    # Meanwhile an admin narrows the live policy (e.g. drops a purpose).
    status, v3 = put_policy(call, binding, max_validity_duration="P7D")
    assert status == 200 and v3["status"] == "active", v3

    status, body = webhook(call, "request_approved", v2["awe_request_id"], v2["id"])
    assert status == 200 and body["status"] == "stale", body
    v = versions(call, binding)
    assert v[3]["status"] == "active"  # the narrowing stays in force
    assert v[2]["status"] == "stale" and "v1 to v3" in v[2]["status_reason"]

    # Resubmitting re-evaluates it against v3 (still a widening → pending).
    status, v4 = call("POST", f"/consent/v1/partners/{binding}/policies/2/resubmit")
    assert status == 200 and v4["status"] == "pending" and v4["base_version"] == 3, v4


@pytest.mark.parametrize("event_type, reason", [
    ("request_rejected", "Rejected in AWE by alex"),
    ("request_cancelled", "Cancelled in AWE by alex"),
])
def test_rejection_and_cancellation(call, awe_on, binding, event_type, reason):
    activate_first(call, binding, awe_on)
    status, v2 = put_policy(call, binding, allowed_data_scopes=["farm_details", "land"])
    status, body = webhook(call, event_type, v2["awe_request_id"], v2["id"], actor="alex")
    assert status == 200 and body["status"] == "rejected", body
    v = versions(call, binding)
    assert v[2]["status"] == "rejected" and v[2]["status_reason"] == reason
    assert v[1]["status"] == "active"


def test_decision_before_request_id_is_stored_correlates_by_artifact(call, awe_on, binding):
    activate_first(call, binding, awe_on)
    status, v2 = put_policy(call, binding, allowed_data_scopes=["farm_details", "land"])
    status, body = webhook(call, "request_approved", "req-not-yet-stored", v2["id"])
    assert status == 200 and body["status"] == "active", body


def test_unknown_policy_is_retryable(call, awe_on):
    status, body = webhook(call, "request_approved", "req-unknown", str(uuid.uuid4()))
    assert status == 422, body


# ── First policy of a binding ───────────────────────────────────────────────


def test_first_policy_gated_by_default(call, awe_on, binding):
    status, v1 = put_policy(call, binding)
    assert v1["status"] == "pending"


def test_first_policy_ungated_when_flag_off(call, awe_on, binding, monkeypatch):
    monkeypatch.setattr(_config, "awe_gate_first_policy", False)
    status, v1 = put_policy(call, binding)
    assert status == 200 and v1["status"] == "active", v1
    # Later widenings are still gated.
    status, v2 = put_policy(call, binding, allowed_data_scopes=["farm_details", "land"])
    assert v2["status"] == "pending"


def test_awe_off_activates_immediately(call, env, binding, monkeypatch):
    monkeypatch.setattr(_config, "awe_enabled", False)
    status, v1 = put_policy(call, binding)
    assert v1["status"] == "active" and v1["awe_request_id"] is None


# ── AWE client ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_awe(monkeypatch):
    """Route the AWE client's httpx calls to a handler; returns the request log."""
    seen = []
    state = {"handler": None}

    def transport_handler(request):
        seen.append(request)
        return state["handler"](request)

    real = httpx.AsyncClient
    transport = httpx.MockTransport(transport_handler)

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real(*args, **kwargs)

    monkeypatch.setattr(awe_client_mod.httpx, "AsyncClient", factory)
    monkeypatch.setattr(_config, "awe_base_url", "http://awe")
    monkeypatch.setattr(_config, "awe_static_token", "service-token")
    return seen, state


@pytest.mark.parametrize("response, message, code", [
    (httpx.Response(404, json={"id": "x", "errors": [
        {"errorCode": "AWE-001", "message": "No active policy for 'k'"}]}),
     "No active policy for 'k'", "AWE-001"),
    (httpx.Response(401, json={"detail": "Invalid bearer token: bad issuer"}),
     "Invalid bearer token: bad issuer", ""),
    (httpx.Response(502, text="upstream down"), "upstream down", ""),
])
def test_client_parses_awe_errors(run, mock_awe, response, message, code):
    _, state = mock_awe
    state["handler"] = lambda request: response
    with pytest.raises(AweClientError) as err:
        run(AweClient.get_component().create_request(
            artifact_type="t", artifact_id="a", context={}))
    assert err.value.message == message and err.value.error_code == code
    assert err.value.status_code == response.status_code


def test_client_forwards_callers_bearer_and_requester(run, mock_awe):
    seen, state = mock_awe
    state["handler"] = lambda request: httpx.Response(201, json={"request_id": "r1"})
    client = AweClient.get_component()
    assert run(client.create_request(
        artifact_type="t", artifact_id="a1", context={}, requester="alice",
        caller_bearer="admin-jwt")) == "r1"
    sent = seen[-1]
    assert sent.headers["Authorization"] == "Bearer admin-jwt"
    assert sent.headers["Idempotency-Key"] == "cm-partner-a1"
    assert json.loads(sent.content)["requester"] == "alice"
    # Without a caller bearer the service token is used.
    run(client.create_request(artifact_type="t", artifact_id="a2", context={}))
    assert seen[-1].headers["Authorization"] == "Bearer service-token"


def test_inbox_lists_open_and_claimed(run, mock_awe):
    seen, state = mock_awe

    def handler(request):
        status = request.url.params["status"]
        items = {
            "open": [{"id": "t1", "status": "open", "created_at": "2026-01-01T00:00:00"}],
            "claimed": [{"id": "t2", "status": "claimed", "created_at": "2026-01-02T00:00:00"}],
        }[status]
        return httpx.Response(200, json={"items": items, "total": 1, "page": 1,
                                         "page_size": 100, "pages": 1})

    state["handler"] = handler
    out = run(AweClient.get_component().list_my_tasks("approver-jwt"))
    assert [t["id"] for t in out["items"]] == ["t2", "t1"]
    assert out["total"] == 2
    assert {r.url.params["status"] for r in seen} == {"open", "claimed"}
    assert all(r.headers["Authorization"] == "Bearer approver-jwt" for r in seen)


def test_concurrent_widenings_leave_one_pending(call, run, awe_on, binding):
    import asyncio
    from types import SimpleNamespace

    from openg2p_consent_manager.services import PartnerService, PolicyPending

    activate_first(call, binding, awe_on)
    svc = PartnerService.get_component()

    def change(scope):
        return SimpleNamespace(**{**BASE, "allowed_data_scopes": ["farm_details", scope],
                                  "allowed_subject_id_types": [], "fetch_type": "oneshot",
                                  "max_fetch_frequency": None, "data_life": None})

    async def both():
        return await asyncio.gather(
            svc.upsert_policy(binding, change("land")),
            svc.upsert_policy(binding, change("crops")),
            return_exceptions=True,
        )

    results = run(both())
    assert sum(isinstance(r, PolicyPending) for r in results) == 1, results
    pending = [p for p in versions(call, binding).values() if p["status"] == "pending"]
    assert len(pending) == 1


def test_inbox_when_approvals_are_off_is_empty_and_says_so(call, env, monkeypatch, mock_awe):
    seen, _state = mock_awe
    monkeypatch.setattr(_config, "awe_enabled", False)
    status, body = call("GET", "/consent/v1/awe/tasks")
    assert status == 200, body
    assert body["items"] == [] and body["total"] == 0 and body["awe_enabled"] is False
    assert seen == []  # AWE is not called
