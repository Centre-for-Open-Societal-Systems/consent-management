# OpenG2P Consent Partner Portal

Web app for **partner users** (employees of a partner such as a bank) to obtain a subject's consent
in person (consent scenario 1, "assisted") and use it:

- **Consents** (home) — **New consent** raises a consent request for a subject (at the Exchange:
  one of the consent templates offered to the partner; elsewhere: purpose and data per registry),
  with the subject's
  national-ID login and the signed consent form, submitted for verification by Consent Manager
  staff. Requests not yet decided are listed first ("In progress"), then the partner's consents:
  data granted per registry, validity, assurance, the signed receipt and the consent record.
- **Consent templates** (`/templates`) — read-only: at the Exchange, the consent templates offered
  to the partner, with their purpose, data per registry (required / optional) and limits; elsewhere
  titled **Data access**, the partner's active data-share policies per registry.
- **History** (`/history`) — every request the partner raised and what became of it (approved with
  its consent, rejected with the reason, cancelled).

It is a front end over the Consent Manager partner-portal API (`/consent/v1/partner-portal/*`)
only. Same stack and look as the CM
console in `../ui`: React + Vite + TypeScript, keycloak-js, TanStack Query, plain CSS on the OpenG2P
theme.

## Sign-in

Keycloak realm `partner`, public client `consent-partner-portal` (standard flow + PKCE). A partner
user carries the `partner_id` claim and the `PARTNER_OPERATOR` or `PARTNER_ADMIN` realm role; the
API scopes every call to that partner. A user without them sees "your account is not set up as a
partner user". An expired session (API 401) sends the user to sign in again.

## Runtime configuration

Read at start-up from `/config.json` (served by nginx, never cached), so one image works in every
environment; in Kubernetes mount it from a ConfigMap over `/usr/share/nginx/html/config.json`.

```json
{
  "apiBaseUrl": "",
  "keycloak": {
    "url": "https://keycloak.example.org",
    "realm": "partner",
    "clientId": "consent-partner-portal"
  }
}
```

| Key | Meaning |
| --- | --- |
| `apiBaseUrl` | Consent Manager API origin. Empty: same origin as the portal (Istio routes `/consent/` to the API). |
| `keycloak.url` | Keycloak base URL. Empty: **dev mode** — no sign-in; an unsigned token with `partner_id` `dev-partner` is sent (accepted only by a backend without partner-realm verification). |
| `keycloak.realm` | `partner` |
| `keycloak.clientId` | `consent-partner-portal` |

If `apiBaseUrl` is another origin, the CM API must allow the portal's origin (CORS).

## Development

```bash
npm install
CM_API_URL=http://localhost:8000 npm run dev   # http://localhost:5174, /consent proxied to the API
npm run build                                  # type-check + production build into dist/
npm run lint
```

Edit `public/config.json` for local Keycloak URLs.

## Docker

```bash
docker build -t openg2p-consent-partner-portal:latest ./partner-portal
docker run -p 8080:8080 openg2p-consent-partner-portal:latest
```

Static SPA served by unprivileged nginx on port 8080 (`nginx.conf`: SPA fallback, no-cache
`config.json` and `index.html`, long cache for hashed assets).
