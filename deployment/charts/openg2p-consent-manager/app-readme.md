# OpenG2P Consent Manager

The **Consent Manager** is the Policy Decision Point (PDP) for outbound data sharing in OpenG2P.
A single instance is shared across all data-holding modules (registry, PBMS, …): a partner embeds
a signed consent object in its request, the module forwards it to the Consent Manager, and the
service verifies the signature against the partner's onboarded keys, evaluates it against the
partner's policy, and returns the exact set of fields that may be released.

This chart deploys the API (horizontally autoscaled, stateless), provisions its PostgreSQL
database, optionally provisions its Keycloak client and admin role, and runs a periodic
consent-expiry job.

**Partner portal (optional, off by default).** Turn on `partnerPortal.enabled` to deploy the partner
portal at `consent-partner-portal.<domain>`: partner users (e.g. bank staff) create consent requests,
upload the signed consent form and submit it for verification by CM staff. It also creates the Keycloak
realm `partner` with the public client `consent-partner-portal`, and stores uploaded forms in the
commons Garage (bucket `consent-evidence`). keycloak-init cannot do the rest, so once after install:

1. Keycloak, realm `partner` → Realm roles: create `PARTNER_OPERATOR` and `PARTNER_ADMIN`.
2. Realm settings → User profile: create attribute `partner_id` (admin view/edit only).
3. Clients → `consent-partner-portal` → Client scopes → `consent-partner-portal-dedicated` → Add mapper →
   User Attribute: user attribute `partner_id`, token claim `partner_id`, add to access token.
4. Clients → `consent-partner-portal` → Advanced: PKCE method `S256` (optional).
5. Users: create each partner user with attribute `partner_id` (the partner's ID, e.g. `bank-a`), a role
   and a password. Passwords for `partnerPortal.testUsers` are in the `<release>-partner-test-users` Secret.
6. Garage: `garage bucket create consent-evidence` and
   `garage bucket allow --read --write --owner consent-evidence --key garage-key`.

Full documentation: [docs.openg2p.org → Consent Management](https://docs.openg2p.org/).
