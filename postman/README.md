# Postman collections

API tests for the farmer's **national-ID login through eSignet**. Every request has
`pm.test` assertions, so a run is a pass/fail check that also works in CI.

| Collection | What it tests | Needs |
|---|---|---|
| `consent-manager-bene-api-esignet.postman_collection.json` | Beneficiary (bene) API auth: the farmer logs in with eSignet as an API client, gets a bene API token, then lists, approves, denies and revokes their own requests and consents. Negative checks: no token, staff token, another farmer's request, tampered token, replayed code and state, open redirect. | `beneApiAuth.enabled: true` |
| `consent-manager-consent-login-esignet.postman_collection.json` | Consent login: a token for **one** consent request, used to grant or decline that request and refused for everything else. The national ID that logs in must be the request's subject. | `consentLogin.enabled: true` |

Both follow [`../BENE-API-ESIGNET-AUTH.md`](../BENE-API-ESIGNET-AUTH.md) (sections 1, 4 and 7).

## Before you run

- eSignet with the **mock identity system**. The collections play the eSignet login
  page (CSRF → oauth-details → send OTP → authenticate → auth-code), so no browser is
  needed. A real ID system sends a real OTP, so there you log in through the browser.
- The two test national IDs exist in the mock identity system.
- A staff user that may raise consent requests, and a partner (`partner_audience`)
  with a binding for `data_controller`. Folder **0 Setup** raises new requests on
  every run, so a run can be repeated.

## Files

Each collection has its own environment file next to it, with the same name.
Import both into Postman (**Import** → select the two files), or pass both to the CLI.

| Collection | Environment |
|---|---|
| `consent-manager-bene-api-esignet.postman_collection.json` | `consent-manager-bene-api-esignet.postman_environment.json` ("Consent Manager - Bene API (eSignet login)") |
| `consent-manager-consent-login-esignet.postman_collection.json` | `consent-manager-consent-login-esignet.postman_environment.json` ("Consent Manager - Consent login (eSignet)") |

The collections hold no host, ID or password; everything comes from the environment.
The environment values are the defaults of the repo's Docker Compose setup
(`local-run/`): bene API `:8073`, staff API `:8071`, eSignet `:8088`, Keycloak `:8090`,
and the test IDs and OTP of eSignet's mock identity system. For another deployment,
change the values in the environment after import, or override them for one run
with `--env-var <name>=<value>`. No password is stored.

On a deployed Consent Manager, the bene API and the staff API share the console host
(the console route sends the farmer's paths to the bene API), so `bene_api_url` and
`staff_api_url` are both `https://<console host>/consent/v1`.

Before the first request, the run checks that every variable below has a value. If one
is missing, it stops there and names it.

| Variable | Meaning |
|---|---|
| `bene_api_url` | Bene API base URL, ending in `/consent/v1` |
| `staff_api_url` | Staff API base URL, ending in `/consent/v1` (only used to find the partner binding) |
| `esignet_api_url` | eSignet API base URL, ending in `/v1/esignet` |
| `keycloak_url`, `staff_realm`, `staff_client_id` | Keycloak base URL (without `/realms`), staff realm, and a client that allows the password grant |
| `staff_username`, `staff_password` | Staff user for setup. Empty in the files: **set them after import, or pass them with `--env-var`. Never commit them.** |
| `partner_audience`, `data_controller`, `data_scope`, `purpose_code` | What the setup requests ask for |
| `subject_id_type` | ID type of the requests; must be one of `SUBJECT_AUTH_ID_TYPES` |
| `farmer_national_id`, `other_national_id` | Test national IDs in the mock identity system |
| `otp` | OTP the mock identity system accepts |

All other variables (tokens, request IDs, login state) are collection variables
that the run sets itself.

## Run

**In the Postman app:** import the collection and its environment, select the
environment, fill in `staff_username` and `staff_password` (and change the URLs
if the Consent Manager is not on your machine), then run the collection with the
Collection Runner. Keep the request order: later requests use the IDs and tokens the
earlier ones set.

With the [Postman CLI](https://learning.postman.com/docs/postman-cli/postman-cli-overview/):

```sh
postman collection run postman/consent-manager-bene-api-esignet.postman_collection.json   -e postman/consent-manager-bene-api-esignet.postman_environment.json   --env-var staff_username=<staff user> --env-var staff_password=<password>

postman collection run postman/consent-manager-consent-login-esignet.postman_collection.json   -e postman/consent-manager-consent-login-esignet.postman_environment.json   --env-var staff_username=<staff user> --env-var staff_password=<password>
```

Against a deployed Consent Manager, override the URLs and test data the same way:

```sh
postman collection run postman/consent-manager-bene-api-esignet.postman_collection.json   -e postman/consent-manager-bene-api-esignet.postman_environment.json   --env-var bene_api_url=https://<console host>/consent/v1   --env-var staff_api_url=https://<console host>/consent/v1   --env-var esignet_api_url=https://<esignet host>/v1/esignet   --env-var keycloak_url=https://<keycloak host>   --env-var staff_username=<staff user> --env-var staff_password=<password>   --env-var partner_audience=<partner> --env-var farmer_national_id=<test ID>   --env-var other_national_id=<second test ID> --env-var otp=<mock OTP>
```

[newman](https://github.com/postmanlabs/newman) takes the same files
(`newman run <collection> -e <environment> --env-var ...`). For CI, add
`-r cli,junit --reporter-junit-export results.xml`.

Expected result: every assertion passes (bene API: 36 requests, 74 assertions;
consent login: 53 requests, 112 assertions). Each request also checks that it did
not get a server error (5xx).

## When a run fails

| Failure | Usual cause |
|---|---|
| `0a Staff token` is not 200 | Wrong `keycloak_url`, `staff_realm`, client or staff password |
| `0b Find the partner binding` finds nothing | The partner has no binding for `data_controller` |
| `1a` says the login is off | `beneApiAuth.enabled` (or `consentLogin.enabled`) is not `true` |
| eSignet `oauth-details` returns `invalid_client` or `invalid_redirect_uri` | The CM's eSignet client is not registered, or the callback is missing from its redirect URIs (`BENE-API-ESIGNET-AUTH.md` 3.4, 3.5) |
| eSignet `authenticate` fails | The national ID is not in the mock identity system, or `otp` is wrong |
| Callback is not 200 | The CM cannot exchange the code: check its client key and `kid` (`BENE-API-ESIGNET-AUTH.md` section 5) |
