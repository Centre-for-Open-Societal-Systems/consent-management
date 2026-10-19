# Consent Manager

OpenG2P Consent Management Service.

Documentation (design, API, development, deployment) lives in the OpenG2P GitBook:
https://docs.openg2p.org/ → **Consent Management**
(source: `openg2p-documentation/consent-management/`).

## COSS deployment (DevOps checklist)

This repository is OpenG2P's consent manager plus the COSS additions: OTP consent on the
partner portal API, the policy field `consent_method`, purpose binding at `/validate`, and
the OTP limits. This section lists what a deployment of it needs on top of a standard
OpenG2P install with the Helm chart in `deployment/charts/openg2p-consent-manager`.

Every service setting is an environment variable `CONSENT_MANAGER_<NAME>`
(`backend/src/openg2p_consent_manager/config.py`). In the chart, plain values go under
`consentManagerApi.envVars` and secrets under `consentManagerApi.envVarsFrom`
(`secretKeyRef`); both apply to every API deployment (staff, partner, beneficiary).
Never put secrets in git; `backend/.env` (local runs) is git-ignored.

### 1. Image

The chart defaults to OpenG2P's Docker Hub image (`openg2p/consent-manager-api:develop`),
which does **not** contain the COSS additions. Build this repo and point the chart at it:

```bash
docker build -f docker/consent-manager-api/Dockerfile -t <registry>/consent-manager-api:<tag> .
```

```yaml
consentManagerApi:
  image:
    repository: <registry>/consent-manager-api
    tag: <tag>
```

The Dockerfile pins `FASTAPI_COMMON_REF` to a framework commit the code works with, and the
build fails if the app cannot be imported. The partner API and the other API deployments
use the same image (they inherit `consentManagerApi`). The UI and partner-portal images
are unchanged OpenG2P ones.

### 2. Helm values

| Value | Set to | Why |
|---|---|---|
| `consentManagerApi.signingKey.mode` | `existing` (production) + `secretName` | The CM signing key (`.p12`). Receipts are now minted by the **staff** API too (OTP and assisted consents), so the chart mounts the key on the staff and the partner APIs. `demo` is a public key: testing only. |
| `partnerPortal.enabled` | `true` | The OTP consent API lives on the partner portal API (`/consent/v1/partner-portal/*`, served by the staff API deployment). This sets the partner-realm token settings (`PARTNER_AUTH_ISSUER` / `PARTNER_AUTH_JWKS_URL`) on the staff API and routes `/consent/v1/partner-portal/` on the portal host. |
| `partnerPortal.keycloak.*` | defaults, or the environment's Keycloak | `issuerUrl` = the public partner realm URL (the `iss` in partner tokens); `jwksUrl` = an in-cluster URL the pods can reach. |
| `evidence.s3.*` | an S3/MinIO bucket + `existingSecret` | Turned on with the partner portal (assisted requests upload the signed form). Not used by OTP requests. |
| `global.aweEnabled` | as the environment decides | With AWE on, a policy change that loosens access (including `consent_method` `otp` → `partner_signed` / `internal`) waits for approval. |

### 3. Environment variables for OTP consent

Add these under `consentManagerApi.envVars` / `envVarsFrom`. Only the staff API uses them.

**Required:**

| Variable | Value | Notes |
|---|---|---|
| `CONSENT_MANAGER_OTP_SALT` | **Secret**, a long random string (e.g. `openssl rand -hex 32`) | Mixed into the stored OTP hash. No default: without it every OTP request is refused (`503 otp_unavailable`). Use a different value per environment and keep it stable (changing it invalidates codes in flight). |
| a delivery channel | see below | Without one, no code is issued (`503 otp_unavailable`). |

**Delivery of the code (read this):** there is **no SMS / Fayda delivery yet**. The providers
generate and verify codes but do not send them. Until a delivering provider is added, an
environment can only use OTP consent with one of the **test harnesses**, which must never be
enabled where real people's data is:

| Variable | Value | Effect |
|---|---|---|
| `CONSENT_MANAGER_OTP_PUBLISH_ENABLED` | `true` (test environments only) | Each code is written as JSON to a bucket (`OTP_PUBLISH_BUCKET`, default `a2c-webhook`, prefix `otp/`), as in OAN Gen 1 / A2C. Anyone who can read the bucket can read live codes. |
| `CONSENT_MANAGER_OTP_PUBLISH_ENDPOINT_URL` | `http://minio:9000` (empty = AWS S3) | |
| `CONSENT_MANAGER_OTP_PUBLISH_ACCESS_KEY`, `CONSENT_MANAGER_OTP_PUBLISH_SECRET_KEY` | **Secret** | Bucket credentials. |
| `CONSENT_MANAGER_OTP_PUBLISH_REGION`, `CONSENT_MANAGER_OTP_PUBLISH_ID_TYPE` | `us-east-1`, `FIN` | |
| `CONSENT_MANAGER_OTP_LOG_CODES` | `true` (local development only) | Writes live codes to the service log. Default `false`. |

**Optional** (defaults shown):

| Variable | Default | Meaning |
|---|---|---|
| `CONSENT_MANAGER_OTP_PROVIDER` | `fayda` | `fayda` (Fayda OTP rules, in-process) or `internal`. |
| `CONSENT_MANAGER_OTP_LENGTH`, `CONSENT_MANAGER_OTP_TTL_SEC` | `6`, `300` | Code length; code lifetime. |
| `CONSENT_MANAGER_OTP_MAX_ATTEMPTS` | `3` | Wrong codes allowed per code. |
| `CONSENT_MANAGER_OTP_RESEND_COOLDOWN_SEC` | `60` | Minimum gap between two codes for one request. |
| `CONSENT_MANAGER_OTP_MAX_SENDS_PER_REQUEST` | `3` | Codes per request (first + 2 resends). |
| `CONSENT_MANAGER_OTP_MAX_FAILURES_PER_REQUEST` | `6` | Wrong codes over all of a request's codes; then the request is cancelled. |
| `CONSENT_MANAGER_OTP_MAX_SENDS_PER_SUBJECT`, `CONSENT_MANAGER_OTP_SUBJECT_WINDOW_SEC` | `10`, `3600` | Codes per person (any partner) per window. |
| `CONSENT_MANAGER_OTP_CHANNEL` | `PHONE` | Recorded with the request. |
| `CONSENT_MANAGER_PARTNER_AUTH_AUDIENCE` | empty | Set to check the `aud` of partner tokens. |
| `CONSENT_MANAGER_PARTNER_AUTH_PARTNER_CLAIM` | `partner_id` | Claim naming the partner in a partner token. |

`consent_method` and the purpose check have **no environment variables**: `consent_method`
is set per partner binding on its policy (step 5), and the purpose check is always on.

### 4. Keycloak: one client per partner (bank)

In the partner realm (created by `partner-keycloak-init` when `partnerPortal.enabled`), each
partner back-end that calls the API gets:

1. A confidential client with **service accounts enabled** (client_credentials), standard
   flow off. Hand the client id + secret to the partner.
2. A **hardcoded claim** mapper: claim `partner_id`, value = the partner's id, which is the
   `audience` of its CM binding (e.g. `bank-a`), in the access token.
3. The realm role `PARTNER_OPERATOR` on the client's service account.

The aggregation layer verifies the same partner tokens (its `PARTNER_AUTH_ISSUER` must be
this realm).

### 5. After deploying (admin steps, through the staff API or console)

1. Bind each partner: `POST /consent/v1/partners` with `audience` = the `partner_id` claim
   above, `controller_id` = the data controller (`aggregation_layer` for partners that call
   the aggregation layer, with namespaced scopes such as `FARMER_REGISTRY.farmer_personal_details`).
2. Give the binding a policy: `PUT /consent/v1/partners/{binding_id}/policy`.
   **External partners (banks): add `"consent_method": "otp"`.** They then get data only
   through a consent the person confirmed with an OTP; their own signed `consent_jws` is
   refused (`consent_method_not_allowed`).

   | `consent_method` | Accepts |
   |---|---|
   | not set (every existing policy) = `partner_signed` | partner-signed `consent_jws` and stored consents (unchanged behaviour) |
   | `partner_signed` | the same |
   | `internal` | the same; marks an integration without the person's consent (e.g. the aggregation layer → a registry) |
   | `otp` | only an OTP-confirmed consent presented by `consent_id` |

   Left out of a later policy update (e.g. an edit in the console, which does not know the
   field), the binding keeps its method; `null` resets it to `partner_signed`.
3. Check: the partner portal `GET /consent/v1/partner-portal/bindings` (partner token) shows
   `consent_method` per binding.
4. Smoke test (test environment): partner token → `POST /consent/v1/partner-portal/consent-requests`
   with `"method": "otp"` → read the code (test harness) → `POST …/consent-requests/{id}/verify-otp`
   → `consent_id` → `POST /consent/v1/validate` (partner API) with that `consent_id` and the
   partner → `permit`.

Database: the service adds its new columns at startup (`migrate`); no manual migration.

Deploy together with the matching aggregation layer release (`oan-aggregation-api`), which
sends `authorize.consent_id` and the purpose to `/validate`.
