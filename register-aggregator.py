"""Register everything the aggregator needs to work. Idempotent, re-runnable.

The aggregator is not a special case inside the platform - it is a partner, and
it has to be onboarded like one. Four things get created:

1. A Partner Management partner ``AGGREGATOR_CM`` holding the Consent Manager's
   Ed25519 public key. This is what each registry fetches to verify the consent
   objects the aggregator signs on its internal hops.

2. A CM binding **partner -> aggregator** (``komal-aggregator``). Its policy's
   allowed_data_scopes are FIELD ALIASES (farmer.firstname, livestock.UIN …),
   not registry block names, because CM treats scopes as opaque strings. That
   makes effective_data_scopes mean exactly "the fields this partner may ask
   for", and the aggregator intersects the request against it.

3. Three CM bindings **aggregator -> registry** (agg-farmer / agg-livestock /
   agg-cropsown), whose scopes ARE registry block names, because that is what
   each registry's clamp understands.

4. AWE approval for each policy. The first policy on a partner is a widening
   (a grant from nothing), so it lands `pending` and has to be approved before
   it counts - a policy still pending reads as "no policy" to /validate.

Run inside consent-manager-backend-1, which already has httpx and can reach PM
by service alias:

    docker cp postman/register-aggregator.py consent-manager-backend-1:/tmp/r.py
    docker exec consent-manager-backend-1 python /tmp/r.py
"""
import json
import os
import sys

import httpx

KEYCLOAK = os.environ.get(
    "G2P_KEYCLOAK", "http://localhost:8080") + "/realms/staff/protocol/openid-connect/token"
CM = os.environ.get("G2P_CM", "http://localhost:8000")
PM_ADMIN = os.environ.get("G2P_PM_ADMIN", "http://commons-services-pm-staff-portal-api:8000")
PM_KEYS = os.environ.get("G2P_PM_KEYS", "http://commons-services-pm-partner-api:8000")

# The registry derives the envelope-signing partner from the DCI header as
# PARTNER_{sender_id.replace("-","_").upper()}, and CM derives the
# consent-signing partner from the binding's partner_mgmt_id. Using ONE
# identity for both means one key registration and one thing to reason about:
#   sender_id "cm-aggregator"  ->  PARTNER_CM_AGGREGATOR
AGG_PM_ID = "PARTNER_CM_AGGREGATOR"
PARTNER_PM_ID = "PARTNER_KC"          # the partner that will call the aggregator
CM_KID = os.environ.get("G2P_CM_KID", "cm-2025-01")

# The fields a partner may ask the aggregator for. Keep in step with
# services/field_catalog.py - anything not listed here is refused by the policy
# ceiling before the catalog is even consulted.
FIELD_SCOPES = [
    "farmer.firstname", "farmer.lastname", "farmer.middlename", "farmer.mobile",
    "farmer.UIN", "farmer.gender", "farmer.birthdate", "farmer.marital_status",
    "farmer.education_level", "farmer.registration_date",
    "farmer.family_details", "farmer.farm_details",
    "livestock.UIN", "livestock.oan_id", "livestock.status",
    "livestock.total_animals", "livestock.registration_date",
    "livestock.farmer_firstname", "livestock.farmer_lastname",
    "livestock.farmer_mobile", "livestock.place",
    "livestock.animal_details", "livestock.health_event_details",
    "livestock.vaccination_details", "livestock.vital_event_details",
    "livestock.breeding_details",
    "cropsown.crop_sown_details", "cropsown.crop_production_details",
    "cropsown.farm_details", "cropsown.infestation_details",
    "cropsown.cluster_details",
]

BINDINGS = [
    # audience, controller_id, pm_id, scopes, label, required_auth_method
    ("komal-aggregator", "consent-manager-aggregator", PARTNER_PM_ID, FIELD_SCOPES,
     "Partner -> Aggregator (field aliases)", "otp"),
    ("agg-farmer", "farmer_registry", AGG_PM_ID,
     ["farmer_personal_details", "family_details", "farm_details"],
     "Aggregator -> Farmer registry", None),
    ("agg-livestock", "livestock_registry", AGG_PM_ID,
     ["livestock_details", "animal_details", "health_event_details",
      "vaccination_details", "vital_event_details", "breeding_details"],
     "Aggregator -> Livestock registry", None),
    ("agg-cropsown", "cropsown_registry", AGG_PM_ID,
     ["crop_sown_details", "crop_production_details", "farm_details",
      "infestation_details", "cluster_details"],
     "Aggregator -> Crop Sown registry", None),
]


def hdr():
    r = httpx.post(KEYCLOAK, timeout=20, data={
        "grant_type": "password", "client_id": "consent-manager-ui",
        "username": "staff", "password": "staff", "scope": "openid"})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"],
            "Content-Type": "application/json"}


def step(n, text):
    print("\n" + "=" * 74)
    print("STEP %s: %s" % (n, text))
    print("=" * 74)


def pm_register_key(H, pub_pem):
    """Create AGGREGATOR_CM in PM if absent, then register the CM public key."""
    existing = httpx.get(PM_ADMIN + "/partners/" + AGG_PM_ID, headers=H, timeout=20)
    if existing.status_code == 200:
        print("  PM partner %s already exists" % AGG_PM_ID)
    else:
        # PM requires at least one key at onboarding (PM-KEY-400), so the
        # signing key goes in with the partner rather than in a later update.
        r = httpx.post(PM_ADMIN + "/partners/requests/onboarding", headers=H, timeout=30,
                       json={"partner_id": AGG_PM_ID,
                             "name": "Consent Manager Aggregator",
                             "org_name": "OpenG2P",
                             "description": "Signs internal consent objects for the "
                                            "cross-registry aggregated fetch.",
                             "keys": [{"public_key": pub_pem, "kid": CM_KID,
                                       "algorithm": "EdDSA"}]})
        if r.status_code >= 400:
            raise SystemExit("PM onboarding failed: %s %s" % (r.status_code, r.text[:400]))
        req_id = r.json()["id"]
        a = httpx.post(PM_ADMIN + "/partners/requests/%s/approve" % req_id,
                       headers=H, timeout=30, json={"notes": "aggregator registration"})
        print("  onboarding %s -> %s" % (req_id, a.status_code))
        a.raise_for_status()

    # Re-register only if PM is not already serving this kid, so a re-run does
    # not churn the key record for no reason.
    if httpx.get(PM_KEYS + "/keys/%s/%s" % (AGG_PM_ID, CM_KID), timeout=20).status_code != 200:
        x = httpx.post(PM_ADMIN + "/partners/requests/key-update", headers=H, timeout=30,
                       json={"partner_id": AGG_PM_ID,
                             "keys": [{"public_key": pub_pem, "kid": CM_KID,
                                       "algorithm": "EdDSA"}]})
        if x.status_code >= 400:
            raise SystemExit("key-update failed: %s %s" % (x.status_code, x.text[:400]))
        httpx.post(PM_ADMIN + "/partners/requests/%s/approve" % x.json()["id"],
                   headers=H, timeout=30, json={"notes": "aggregator key"}).raise_for_status()

    served = httpx.get(PM_KEYS + "/keys/%s/%s" % (AGG_PM_ID, CM_KID), timeout=20)
    print("  PM serves %s/%s -> HTTP %s" % (AGG_PM_ID, CM_KID, served.status_code))
    if served.status_code != 200:
        raise SystemExit("PM is not serving the aggregator key; registries will "
                         "reject every internal hop with signature_invalid")


def cm_binding(H, audience, controller, pm_id, scopes, label, auth_method=None):
    rows = httpx.get(CM + "/consent/v1/partners", headers=H, timeout=30).json()
    rows = rows if isinstance(rows, list) else rows.get("items", rows.get("data", []))
    match = [p for p in rows if p.get("audience") == audience]
    if match:
        pid = match[0]["id"]
        print("  binding exists: %-20s %s" % (audience, pid))
        if match[0].get("partner_mgmt_id") != pm_id:
            u = httpx.put(CM + "/consent/v1/partners/%s" % pid, headers=H, timeout=30,
                          json={"partner_mgmt_id": pm_id})
            print("    partner_mgmt_id %s -> %s  (HTTP %s)"
                  % (match[0].get("partner_mgmt_id"), pm_id, u.status_code))
    else:
        r = httpx.post(CM + "/consent/v1/partners", headers=H, timeout=30, json={
            "name": label, "audience": audience, "controller_id": controller,
            "partner_mgmt_id": pm_id, "status": "active"})
        if r.status_code >= 400:
            raise SystemExit("create binding %s failed: %s %s"
                             % (audience, r.status_code, r.text[:300]))
        pid = r.json()["id"]
        print("  created:        %-20s %s" % (audience, pid))

    p = httpx.put(CM + "/consent/v1/partners/%s/policy" % pid, headers=H, timeout=30, json={
        "allowed_data_scopes": scopes,
        "allowed_purposes": ["loan_origination", "subsidy_verification"],
        "allowed_subject_id_types": ["national_id"],
        # EdDSA for the aggregator's own key; ES256 for partner keys.
        "allowed_signing_algs": ["EdDSA", "ES256", "RS256"],
        "max_validity_duration": "P1Y",
        "fetch_type": "oneshot",
        # The subject authenticates on the consent screen. Enforced in approve(),
        # not in the UI, so bypassing the screen does not bypass the check.
        "required_auth_method": auth_method,
    })
    body = p.json() if p.headers.get("content-type", "").startswith("application/json") else {}
    print("  policy ->        HTTP %s  status=%s  v%s  (%d scopes)"
          % (p.status_code, body.get("status"), body.get("version"), len(scopes)))
    return pid


def approve_pending(H):
    tasks = httpx.get(CM + "/consent/v1/awe/tasks", headers=H, timeout=30).json()
    rows = tasks if isinstance(tasks, list) else tasks.get("items", tasks.get("data", []))
    print("  open AWE tasks: %d" % len(rows))
    for t in rows:
        tid = t.get("task_id") or t.get("id")
        httpx.post(CM + "/consent/v1/awe/tasks/%s/claim" % tid, headers=H, timeout=30)
        d = httpx.post(CM + "/consent/v1/awe/tasks/%s/decision" % tid, headers=H, timeout=30,
                       json={"action": "approve", "comment": "aggregator onboarding"})
        print("    approve %s -> HTTP %s" % (tid, d.status_code))


def main():
    pub_path = os.environ.get("G2P_AGG_PUBKEY", "/tmp/aggregator-public-key.pem")
    if not os.path.exists(pub_path):
        raise SystemExit("public key not found at %s (set G2P_AGG_PUBKEY)" % pub_path)
    pub_pem = open(pub_path).read().strip()

    H = hdr()

    step(1, "Register the CM public key in Partner Management")
    pm_register_key(H, pub_pem)

    step(2, "Create the CM bindings and their policy ceilings")
    ids = {}
    for audience, controller, pm_id, scopes, label, auth_method in BINDINGS:
        ids[audience] = cm_binding(H, audience, controller, pm_id, scopes, label,
                                   auth_method)

    step(3, "Approve the pending policies through AWE")
    approve_pending(H)

    step(4, "Confirm every policy is active")
    # AWE approves through an asynchronous webhook, so a check fired immediately
    # after the decision races it and reports pending for a policy that is about
    # to be active. Poll briefly rather than report a false failure.
    import time
    for _ in range(15):
        states = []
        for audience, _c, _p, _s, _l, _a in BINDINGS:
            r = httpx.get(CM + "/consent/v1/partners/%s/policy" % ids[audience],
                          headers=H, timeout=30)
            states.append(r.status_code == 200 and (r.json() or {}).get("status") == "active")
        if all(states):
            break
        time.sleep(1)

    ok = True
    for audience, _c, _p, scopes, _l, _a in BINDINGS:
        pol = httpx.get(CM + "/consent/v1/partners/%s/policy" % ids[audience],
                        headers=H, timeout=30)
        body = pol.json() if pol.status_code == 200 else {}
        status = body.get("status")
        mark = "OK  " if status == "active" else "FAIL"
        print("  %s %-20s status=%-8s v%-3s scopes=%d"
              % (mark, audience, status, body.get("version"), len(body.get(
                  "allowed_data_scopes") or [])))
        ok = ok and status == "active"

    print("\n" + ("All bindings active. The aggregator can now fetch."
                  if ok else "Some policies are NOT active - /validate will treat "
                             "them as no policy and deny."))
    print("\nBinding ids:")
    for a, i in ids.items():
        print("  %-20s %s" % (a, i))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
