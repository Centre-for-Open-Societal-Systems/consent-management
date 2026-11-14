# OpenG2P Consent Manager — Backend

The Policy Decision Point (PDP) for outbound data sharing, built on
[`openg2p-fastapi-common`](https://github.com/OpenG2P/openg2p-fastapi-common).

All documentation lives in the OpenG2P GitBook:

- **Design & API:** https://docs.openg2p.org/ → Consent Management
- **Development & local setup:** Consent Management → Development
- **Deployment:** Consent Management → Deployment

(Source in this repo: `openg2p-documentation/consent-management/`.)

## Consent claims and `/validate` (summary)

The consent is a compact JWS signed by the partner with its Partner-Management
key. Its claims carry `jti`, `aud`, `subject_id`, `purpose`, `fetch_type`,
`validity`, `issued_at`, and the data to share as either:

- `grants: [{data_controller, data_scopes}, ...]` — one consent, one grant per
  registry; or
- `data_controller` + `data_scopes` — the earlier single-registry form, treated
  as one grant.

`POST /consent/v1/validate` takes `consent_jws`, an optional `data_controller`
(the calling registry) and an optional `request_context {requested_scopes,
subject_id, purpose, limits, template}`:

- With `grants`, `data_controller` is required and selects the grant; a
  controller without a grant is denied `controller_not_granted`. With the
  single-registry form, a `data_controller` that is given must match.
- What a partner may access is its policy in Partner Management (PM), one per
  controller, read from PM (`GET /policies/PARTNER_<AUD>?data_controller=…`)
  and cached. Effective scopes = grant ∩ that policy (∩ `requested_scopes`).
  No policy: `unknown_partner`; PM unreachable and nothing cached:
  `policy_unavailable`.
- Caller-terms mode (a configured receipt presenter, e.g. the Composite,
  sending `request_context.purpose`): the terms it sends apply instead of the
  partner's scope policies, and no partner policy is read. The consent's
  purpose must be `purpose` (`purpose_not_allowed`); `requested_scopes` is
  required and is the ceiling (grant ∩ `requested_scopes`); `limits`
  (`allowed_subject_id_types`, `max_validity_duration`, `allowed_signing_algs`,
  `allow_partner_consents` — absent = false) apply; `template` is a label,
  recorded as `policy_ref: template:<code>`. `limits` or `template` from any
  other caller: `caller_terms_not_allowed`.
- A partner-signed `consent_jws` is accepted only when the applicable policy
  (or, in caller-terms mode, `limits`) sets `allow_partner_consents` (else
  `partner_consent_not_accepted`).
- Consent by lookup at a registry: neither `consent_jws` nor `consent_id`, with
  `partner_id`, `data_controller` and `request_context.subject_id` — the CM
  picks the partner's active consent for that subject (its subject or one of its
  `subject_identifiers`; type spelled exactly)
  with a grant for that controller (latest `valid_until`, then newest; none:
  `consent_not_found`) and validates it under the scope policy.
- `request_context.subject_id` of the consent subject's type but another value
  is denied `subject_mismatch`.
- Replay/idempotency is per (`jti`, `data_controller`): each registry gets its
  own decision and receipt.
- The response adds `data_controller`.

Partners, their keys and their policies are managed in Partner Management;
the Consent Manager keeps none.

Originated consents (`/consent-requests`) may carry `grants` too, and the
subject approves each controller's scopes. In the exchange role
(`receipt_issuer` and `receipt_presenters` set), a consent request (lifecycle
or partner portal) must name a consent `template` offered to the partner by the
template source (`CONSENT_MANAGER_CONSENT_TEMPLATE_SOURCE_URL`, the URL of its
template collection: `GET <url>?partner=<audience>`, `GET
<url>/<code>?partner=<audience>`; not offered → `422 template_not_offered`;
unreachable → 503; a request without a template → `400`). The consent takes
the template's purpose and its scopes per registry (optional `grants` choose
within them, required scopes included), its limits apply (subject ID types,
longest validity, also the default validity), and the template's code is kept
as a label (`template`). It is presented in caller-terms mode: by
`consent_id`, or by lookup — a presenter sends neither `consent_jws` nor
`consent_id`, and the CM picks the partner's active consent for the subject
and purpose (latest `valid_until`, then newest; none: `consent_not_found`).

## Tests

`tests/` runs the services, controllers and migration against Postgres
(it wipes the database it is given):

```bash
pip install -e '.[test]'
CM_TEST_DB_DATASOURCE=postgresql+asyncpg://postgres@localhost:5432/cm_test pytest tests
```

The tests are skipped if the database cannot be reached.
