"""Consent from a consent template at the Exchange (exchange role).

The Consent Manager knows no use cases: consents made through the partner
portal (assisted) or the lifecycle API name a consent template offered to the
partner by a template source (at the Agri Stack Exchange, the Composite's
internal endpoint; here a fake behind an httpx transport). The consent records
the template's purpose and the grants (scopes chosen within the template's
required / optional ones), with the template's code as a label; the template's
limits apply. No Partner Management policy is read for it.
"""
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from test_assisted_consent import (  # noqa: F401 (fixtures)
    PRESENTER,
    STAFF,
    as_partner,
    portal,
    realm_key,
    upload,
)
from test_exchange import cfg, exchange_on, post  # noqa: F401 (fixture)
from test_grants import CSR, FR

TEMPLATE = "loan-profile@1"
OTHER = "crop-insurance@1"
SOURCE_URL = "http://templates.test/composite/v1/internal/consent-templates"
SUBJECT = {"type": "FAYDA_FAN", "value": "123456789012"}
ALL_SCOPES = ["farmer_personal_details", "crop_season", "measures"]


def describe(code, purpose="credit-assessment", scopes=None, limits=None):
    """A consent template as the template source publishes it."""
    scopes = scopes or {
        FR: {"required": ["farmer_personal_details"], "optional": []},
        CSR: {"required": ["crop_season"], "optional": ["measures"]},
    }
    name = code.split("@")[0]
    return {
        "code": code, "title": f"Title of {name}", "description": "", "purpose": purpose,
        "scopes": scopes,
        "limits": limits if limits is not None else {
            "allowed_subject_id_types": ["FAYDA_FAN"], "max_validity_duration": "P1Y",
            "allow_partner_consents": False,
        },
    }


class FakeTemplateSource:
    """``GET <source>?partner=`` and ``GET <source>/{code}?partner=``: the
    templates offered to that partner (``offers``); 404 otherwise."""

    def __init__(self):
        self.templates = {}  # code -> template JSON
        self.offers = {}  # partner audience -> [codes]
        self.down = False
        self.max_age = None
        self.requests = []

    def offer(self, partner, codes):
        self.offers[partner] = list(codes)

    def handler(self, request):
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("template source is down", request=request)
        base = httpx.URL(SOURCE_URL).path
        path = request.url.path
        partner = request.url.params.get("partner")
        offered = [c for c in self.offers.get(partner, []) if c in self.templates]
        headers = {"Cache-Control": f"max-age={self.max_age}"} if self.max_age is not None else {}
        if path == base:
            return httpx.Response(
                200, json={"templates": [self.templates[c] for c in offered]}, headers=headers
            )
        code = path[len(base) + 1:]
        if code in offered:
            return httpx.Response(200, json=self.templates[code], headers=headers)
        return httpx.Response(404, json={"error": {"code": "unknown_template"}})


@pytest.fixture
def templates(env):
    from openg2p_consent_manager.config import Settings
    from openg2p_consent_manager.services import ConsentTemplateClient

    config = Settings.get_config()
    saved = config.consent_template_source_url
    config.consent_template_source_url = SOURCE_URL
    fake = FakeTemplateSource()
    fake.templates = {TEMPLATE: describe(TEMPLATE), OTHER: describe(OTHER, purpose="insurance")}
    client = ConsentTemplateClient.get_component()
    client.transport = httpx.MockTransport(fake.handler)
    client.invalidate()
    yield fake
    config.consent_template_source_url = saved
    client.transport = None
    client.invalidate()


@pytest.fixture
def exchange(cfg, portal, templates):  # noqa: F811
    exchange_on(cfg)
    return templates


@pytest.fixture
def bank(templates, new_partner):
    """A partner at the Exchange offered two templates (no PM policies)."""
    p = new_partner()
    templates.offer(p.audience, [TEMPLATE, OTHER])
    return p


def invalidate():
    from openg2p_consent_manager.services import ConsentTemplateClient

    ConsentTemplateClient.get_component().invalidate()


def create(pc, **extra):
    body = {"subject_id": SUBJECT, "template": TEMPLATE}
    body.update(extra)
    return pc("POST", "/consent-requests", json=body)


def approve_assisted(call, pc, upload, **extra):  # noqa: F811
    status, req = create(pc, **extra)
    assert status == 201, req
    assert upload(pc, req["id"])[0] == 201
    assert pc("POST", f"/consent-requests/{req['id']}/submit")[0] == 200
    return req, call("POST", f"{STAFF}/{req['id']}/approve", json={"note": "ok"})


def present(call, consent_id, audience, purpose="credit-assessment", scopes=None, **ctx):
    return post(call, {
        "consent_id": consent_id, "consent_partner_id": audience,
        "partner_id": PRESENTER, "issue_receipts": True,
        "request_context": {
            "subject_id": SUBJECT, "purpose": purpose,
            "requested_scopes": scopes or ALL_SCOPES, "template": TEMPLATE, **ctx,
        },
    })


def by_controller(grants, key="data_scopes"):
    return {g["data_controller"]: g[key] for g in grants}


# ── partner portal ───────────────────────────────────────────────────────────


def test_access_lists_offered_templates(exchange, as_partner, pm, new_partner):
    p = new_partner()
    exchange.templates["unscoped@1"] = describe("unscoped@1", scopes={FR: {}})
    exchange.offer(p.audience, [TEMPLATE, OTHER, "unscoped@1"])
    exchange.templates[OTHER]["limits"] = {"max_validity_duration": "P6M"}
    pm.requests.clear()
    pc = as_partner(p.audience)
    status, access = pc("GET", "/access")
    assert status == 200, access
    assert access["consent_by"] == "template" and access["scope_policies"] == []
    by = {t["code"]: t for t in access["templates"]}
    assert sorted(by) == sorted([TEMPLATE, OTHER, "unscoped@1"])
    loan = by[TEMPLATE]
    assert loan["available"] and loan["purpose"] == "credit-assessment"
    assert loan["title"] == "Title of loan-profile"
    assert by_controller(loan["grants"]) == {
        FR: ["farmer_personal_details"], CSR: ["crop_season", "measures"]}
    csr = next(g for g in loan["grants"] if g["data_controller"] == CSR)
    assert csr["required_scopes"] == ["crop_season"] and csr["optional_scopes"] == ["measures"]
    assert loan["allowed_subject_id_types"] == ["FAYDA_FAN"]
    assert loan["max_validity_duration"] == "P1Y" and loan["allow_partner_consents"] is False
    # Each template with its own limits; absent = none.
    assert by[OTHER]["max_validity_duration"] == "P6M"
    assert by[OTHER]["allowed_subject_id_types"] == []
    assert by["unscoped@1"]["available"] is False and FR in by["unscoped@1"]["detail"]
    assert pc("GET", "/request-settings")[1]["consent_by"] == "template"
    # The CM reads no Partner Management policy for templates.
    assert pm.requests == []
    # The source is asked for this partner's templates.
    assert exchange.requests[-1].url.params["partner"] == p.audience


def test_access_without_templates_is_empty(exchange, as_partner, new_partner):
    status, access = as_partner(new_partner().audience)("GET", "/access")
    assert status == 200 and access == {"consent_by": "template", "scope_policies": [],
                                        "templates": []}


def test_portal_consent_from_template_and_validate(call, exchange, as_partner, bank, upload):  # noqa: F811
    pc = as_partner(bank.audience)
    req, (status, verified) = approve_assisted(call, pc, upload)
    assert req["template"] == TEMPLATE and req["purpose"] == {"code": "credit-assessment"}
    assert by_controller(req["grants"]) == {
        FR: ["farmer_personal_details"], CSR: ["crop_season", "measures"]}
    assert status == 200, verified
    consent_id = verified["consent_id"]
    status, consent = pc("GET", f"/consents/{consent_id}")
    assert consent["template"] == TEMPLATE
    assert by_controller(consent["grants"], "effective_data_scopes") == {
        FR: ["farmer_personal_details"], CSR: ["crop_season", "measures"]}
    # No PM policy behind a grant made from a template: the template is the reference.
    assert all(g["policy_id"] is None and g["policy_ref"] == f"template:{TEMPLATE}"
               for g in consent["grants"])

    body = present(call, consent_id, bank.audience)
    assert body["decision"] == "permit", body
    assert body["grants"] == {FR: ["farmer_personal_details"],
                              CSR: ["crop_season", "measures"]}
    assert set(body["receipts"]) == {FR, CSR}

    # Another purpose (e.g. the other template's): not this consent's.
    other = present(call, consent_id, bank.audience, purpose="insurance", template=OTHER)
    assert other["decision"] == "deny" and other["reason_code"] == "purpose_not_allowed"


def test_scopes_chosen_within_the_template(call, exchange, as_partner, bank, upload):  # noqa: F811
    pc = as_partner(bank.audience)
    chosen = [{"data_controller": FR, "data_scopes": ["farmer_personal_details"]},
              {"data_controller": CSR, "data_scopes": ["crop_season"]}]
    req, (status, verified) = approve_assisted(call, pc, upload, grants=chosen)
    assert by_controller(req["grants"]) == by_controller(chosen)
    assert status == 200, verified
    consent = pc("GET", f"/consents/{verified['consent_id']}")[1]
    assert by_controller(consent["grants"], "effective_data_scopes") == by_controller(chosen)

    for grants, why in (
        ([{"data_controller": FR, "data_scopes": ["farm_details"]},
          {"data_controller": CSR, "data_scopes": ["crop_season"]}], "scope_exceeds_template"),
        ([{"data_controller": FR, "data_scopes": ["farmer_personal_details"]},
          {"data_controller": CSR, "data_scopes": ["measures"]}], "required scopes"),
        ([{"data_controller": FR, "data_scopes": ["farmer_personal_details"]}],
         f"needs {CSR}"),
        ([{"data_controller": "land-registry", "data_scopes": ["parcel"]}],
         "is not in consent template"),
    ):
        status, body = create(pc, grants=grants)
        assert status == 422 and why in body["error"], body


def test_exchange_refuses_requests_without_a_template(call, exchange, as_partner, bank):
    """At the Exchange a consent request must name a template: grants alone are
    refused, portal and lifecycle alike."""
    pc = as_partner(bank.audience)
    grants = [{"data_controller": FR, "data_scopes": ["farmer_personal_details"]}]
    purpose = {"code": "credit-assessment"}
    for extra in ({"grants": grants, "purpose": purpose, "template": None},):
        status, body = create(pc, **extra)
        assert status == 400 and "consent template" in body["error"], body
    status, body = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": bank.audience, "purpose": purpose,
        "grants": grants})
    assert status == 400 and "consent template" in body["error"], body
    status, body = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": bank.audience, "purpose": purpose,
        "data_controller": FR, "requested_scopes": ["farmer_personal_details"]})
    assert status == 400 and "consent template" in body["error"], body


def test_portal_template_not_offered(exchange, as_partner, bank, new_partner):
    status, body = create(as_partner(bank.audience), template="soil-health@1")
    assert status == 422 and "template_not_offered" in body["error"]
    # Offered to another partner only.
    status, body = create(as_partner(new_partner().audience))
    assert status == 422 and "template_not_offered" in body["error"]


def test_portal_template_checks(exchange, as_partner, bank):
    pc = as_partner(bank.audience)
    status, body = create(pc, template="loan profile")
    assert status == 422 and "not a consent template code" in body["error"]
    status, body = create(pc, purpose={"code": "marketing"})
    assert status == 422 and "is not the purpose of consent template" in body["error"]
    exchange.templates[OTHER]["scopes"] = {FR: {"required": [], "optional": []}}
    status, body = create(pc, template=OTHER)
    assert status == 422 and "names no scopes" in body["error"]
    # The template's limits.
    exchange.templates[TEMPLATE]["limits"] = {"allowed_subject_id_types": ["PHONE"]}
    invalidate()
    status, body = create(pc)
    assert status == 422 and "subject_not_allowed" in body["error"]
    exchange.templates[TEMPLATE]["limits"] = {"max_validity_duration": "P7D"}
    invalidate()
    status, body = create(pc, validity={"valid_until": "2099-01-01T00:00:00Z"})
    assert status == 422 and "validity_exceeds_policy" in body["error"]


def test_default_validity_is_the_templates_longest(call, exchange, as_partner, bank, upload):  # noqa: F811
    exchange.templates[TEMPLATE]["limits"] = {"max_validity_duration": "P30D"}
    pc = as_partner(bank.audience)
    _, (status, verified) = approve_assisted(call, pc, upload)
    assert status == 200, verified
    consent = pc("GET", f"/consents/{verified['consent_id']}")[1]
    length = (datetime.fromisoformat(consent["valid_until"])
              - datetime.fromisoformat(consent["valid_from"]))
    assert length == timedelta(days=30)


def test_template_source_down_fails_closed(call, exchange, as_partner, bank, upload):  # noqa: F811
    pc = as_partner(bank.audience)
    status, req = create(pc)
    assert status == 201, req
    assert upload(pc, req["id"])[0] == 201
    assert pc("POST", f"/consent-requests/{req['id']}/submit")[0] == 200

    invalidate()
    exchange.down = True
    status, body = pc("GET", "/access")
    assert status == 503 and "template source" in body["error"]
    status, body = create(pc)
    assert status == 503 and "template source" in body["error"]
    status, body = call("POST", f"{STAFF}/{req['id']}/approve", json={"note": "ok"})
    assert status == 503 and "template source" in body["error"]


def test_template_source_not_configured_fails_closed(exchange, as_partner, bank):
    from openg2p_consent_manager.config import Settings

    Settings.get_config().consent_template_source_url = ""
    invalidate()
    status, body = create(as_partner(bank.audience))
    assert status == 503 and "template source" in body["error"]


def test_approval_narrows_to_the_template_now(call, exchange, as_partner, bank, upload):  # noqa: F811
    pc = as_partner(bank.audience)
    status, req = create(pc)
    assert upload(pc, req["id"])[0] == 201
    assert pc("POST", f"/consent-requests/{req['id']}/submit")[0] == 200
    # The template drops `measures` before staff approve.
    exchange.templates[TEMPLATE] = describe(TEMPLATE, scopes={
        FR: {"required": ["farmer_personal_details"], "optional": []},
        CSR: {"required": ["crop_season"], "optional": []},
    })
    invalidate()
    status, verified = call("POST", f"{STAFF}/{req['id']}/approve", json={"note": "ok"})
    assert status == 200, verified
    status, consent = pc("GET", f"/consents/{verified['consent_id']}")
    assert by_controller(consent["grants"], "effective_data_scopes") == {
        FR: ["farmer_personal_details"], CSR: ["crop_season"]}


def test_approval_needs_the_template_still_offered(call, exchange, as_partner, bank, upload):  # noqa: F811
    pc = as_partner(bank.audience)
    status, req = create(pc)
    assert upload(pc, req["id"])[0] == 201
    assert pc("POST", f"/consent-requests/{req['id']}/submit")[0] == 200
    exchange.offer(bank.audience, [OTHER])
    invalidate()
    status, body = call("POST", f"{STAFF}/{req['id']}/approve", json={"note": "ok"})
    assert status == 422 and "template_not_offered" in body["error"]


def test_portal_outside_exchange_role_keeps_scope_requests(portal, templates, as_partner, bank):
    """Not the exchange role: template is a label and grants are required."""
    status, body = create(as_partner(bank.audience))
    assert status == 422 and "'grants' is required" in body["error"]


def test_template_answers_are_cached(exchange, as_partner, bank):
    pc = as_partner(bank.audience)
    invalidate()
    exchange.requests.clear()
    assert create(pc)[0] == 201
    assert create(pc)[0] == 201
    reads = [r for r in exchange.requests if r.url.path.endswith(TEMPLATE)]
    assert len(reads) == 1 and reads[0].url.params["partner"] == bank.audience
    # max-age=0 from the source: not reused.
    exchange.max_age = 0
    invalidate()
    exchange.requests.clear()
    create(pc)
    create(pc)
    assert len([r for r in exchange.requests if r.url.path.endswith(TEMPLATE)]) == 2


def test_template_client_reads_list_or_object(run, templates):
    """``/{code}`` may answer the object or ``{"templates": [it]}``; another
    code served is not the one asked for."""
    from openg2p_consent_manager.services import ConsentTemplateClient

    client = ConsentTemplateClient.get_component()
    templates.offer("bank-z", [TEMPLATE])
    tpl = run(client.get_template("bank-z", TEMPLATE))
    assert tpl.code == TEMPLATE and tpl.limits.max_validity_duration == "P1Y"
    assert [g["data_controller"] for g in tpl.grants] == [CSR, FR]
    assert client._parse({"templates": [describe(TEMPLATE)]}, TEMPLATE).code == TEMPLATE
    assert client._parse(describe("loan-profile@2"), TEMPLATE) is None
    assert run(client.get_template("bank-z", OTHER)) is None
    assert [t.code for t in run(client.list_templates("bank-z"))] == [TEMPLATE]
    assert run(client.list_templates("nobody")) == []


# ── lifecycle API ────────────────────────────────────────────────────────────


def test_lifecycle_consent_from_template(call, exchange, bank):
    status, req = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": bank.audience, "template": TEMPLATE})
    assert status == 201, req
    assert req["template"] == TEMPLATE and req["purpose"] == {"code": "credit-assessment"}
    token = jwt.encode({"sub": SUBJECT["value"]}, "k" * 32, algorithm="HS256")
    call("POST", f"/consent/v1/consent-requests/{req['id']}/authenticate",
         json={"id_token": token})
    status, art = call("POST", f"/consent/v1/consent-requests/{req['id']}/approve",
                       json={"granted_scopes": ["farmer_personal_details", "crop_season"]})
    assert status == 201, art
    assert art["template"] == TEMPLATE
    assert by_controller(art["grants"], "effective_data_scopes") == {
        FR: ["farmer_personal_details"], CSR: ["crop_season"]}
    assert present(call, art["consent_id"], bank.audience)["decision"] == "permit"
    assert present(call, art["consent_id"], bank.audience, purpose="insurance")[
        "reason_code"] == "purpose_not_allowed"


def test_lifecycle_template_shape_and_role(call, cfg, templates, bank):  # noqa: F811
    status, body = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": bank.audience, "template": TEMPLATE})
    assert status == 422 and "exchange role" in body["error"]
    exchange_on(cfg)
    status, body = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": bank.audience, "template": TEMPLATE,
        "data_controller": FR, "requested_scopes": ["farmer_personal_details"]})
    assert status == 400, body
    status, body = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": bank.audience, "template": "soil-health@1"})
    assert status == 422 and "template_not_offered" in body["error"]
    # Scopes chosen within the template.
    status, req = call("POST", "/consent/v1/consent-requests", json={
        "subject_id": SUBJECT, "partner_id": bank.audience, "template": TEMPLATE,
        "grants": [{"data_controller": FR, "data_scopes": ["farmer_personal_details"]},
                   {"data_controller": CSR, "data_scopes": ["crop_season"]}]})
    assert status == 201, req
    assert req["requested_scopes"] == ["crop_season", "farmer_personal_details"]


# ── migration: use_case → template ───────────────────────────────────────────


def test_migration_renames_use_case_to_template(env, run):
    """A database from the previous release (a ``use_case`` column on
    consents and requests, a ``use_case`` marker on request grants) keeps its
    data as ``template``."""
    from unittest import mock

    from openg2p_fastapi_common.context import dbengine
    from sqlalchemy import text

    now = datetime.now(timezone.utc)

    async def legacy():
        async with dbengine.get().begin() as conn:
            for table in ("consent_artefacts", "consent_requests"):
                await conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN template TO use_case"))
            await conn.execute(text("DROP INDEX IF EXISTS ix_consent_artefacts_purpose_lookup"))
            await conn.execute(text(
                "INSERT INTO consent_requests (id, created_at, subject_id_type, subject_id_value, "
                "partner_id, purpose, requested_scopes, grants, status, use_case) VALUES "
                "('mig-req', :now, 'FAYDA_FAN', '1', 'bank-mig', '{}', '[]', "
                "'[{\"data_controller\": \"a\", \"use_case\": \"x@1\"}, "
                "{\"data_controller\": \"b\"}]', 'pending', 'x@1')"), {"now": now})
            await conn.execute(text(
                "INSERT INTO consent_artefacts (id, created_at, subject_id_type, "
                "subject_id_value, partner_id, purpose, data_scopes, effective_data_scopes, "
                "fetch_type, valid_from, valid_until, source, status, use_case) VALUES "
                "('mig-art', :now, 'FAYDA_FAN', '1', 'bank-mig', '{}', '[]', '[]', 'oneshot', "
                ":now, :now, 'originated', 'active', 'x@1')"), {"now": now})

    async def read():
        async with dbengine.get().connect() as conn:
            cols = {
                (r[0], r[1]) for r in await conn.execute(text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_name IN ('consent_artefacts', 'consent_requests')"))
            }
            req = (await conn.execute(text(
                "SELECT template, grants FROM consent_requests WHERE id = 'mig-req'"))).one()
            art = (await conn.execute(text(
                "SELECT template FROM consent_artefacts WHERE id = 'mig-art'"))).one()
            indexes = {r[0] for r in await conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'consent_artefacts'"))}
            return cols, req, art, indexes

    run(legacy())
    with mock.patch("asyncio.run", env["loop"].run_until_complete):
        env["init"].migrate_database(None)
    cols, req, art, indexes = run(read())
    for table in ("consent_artefacts", "consent_requests"):
        assert (table, "template") in cols and (table, "use_case") not in cols
    assert req[0] == "x@1" and art[0] == "x@1"
    assert req[1] == [{"data_controller": "a", "template": "x@1"}, {"data_controller": "b"}]
    assert "ix_consent_artefacts_purpose_lookup" in indexes
    assert "ix_consent_artefacts_lookup" not in indexes
    # Idempotent.
    with mock.patch("asyncio.run", env["loop"].run_until_complete):
        env["init"].migrate_database(None)
    assert run(read())[1][0] == "x@1"
