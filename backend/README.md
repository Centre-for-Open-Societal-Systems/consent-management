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
subject_id}`:

- With `grants`, `data_controller` is required and selects the grant; a
  controller without a grant is denied `controller_not_granted`. With the
  single-registry form, a `data_controller` that is given must match.
- A partner (audience) can be bound to several controllers, each binding with
  its own policy. Effective scopes = grant ∩ that binding's policy (∩
  `requested_scopes`).
- `request_context.subject_id` of the consent subject's type but another value
  is denied `subject_mismatch`.
- Replay/idempotency is per (`jti`, `data_controller`): each registry gets its
  own decision and receipt.
- The response adds `data_controller`.

Bindings: `POST /consent/v1/partners` with an existing `audience` and a new
`controller_id` adds a binding (409 if that pair exists);
`GET /consent/v1/partners?audience=…` lists a partner's bindings. Policies stay
per binding id (`/partners/{id}/policy`).

Originated consents (`/consent-requests`) may carry `grants` too, and the
subject approves each controller's scopes. They cannot yet be presented at
`/validate`, which takes a partner-signed JWS only.

## Tests

`tests/` runs the services, controllers and migration against Postgres
(it wipes the database it is given):

```bash
pip install -e '.[test]'
CM_TEST_DB_DATASOURCE=postgresql+asyncpg://postgres@localhost:5432/cm_test pytest tests
```

The tests are skipped if the database cannot be reached.
