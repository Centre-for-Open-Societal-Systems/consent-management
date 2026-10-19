#!/usr/bin/env bash
#
# setup-partner-realm.sh
# ----------------------
# One-time (idempotent) Keycloak setup for the Consent Manager's partner portal: the
# parts the chart's keycloak-init cannot do. Run after installing the CM with
# partnerPortal.enabled (keycloak-init creates the "partner" realm and the
# "consent-partner-portal" client; if the realm is missing this script creates it).
#
#   1. realm roles PARTNER_OPERATOR and PARTNER_ADMIN
#   2. the user-profile attribute partner_id (Keycloak 24+ rejects undeclared attributes)
#   3. a protocol mapper putting partner_id into the portal client's tokens
#   4. optionally, partner users (--user): attribute partner_id, a realm role, and a
#      generated password kept in a Kubernetes Secret "partner-user-<username>"
#
# Runs Keycloak's admin CLI (kcadm.sh) inside the Keycloak pod; the admin password is
# read from the Keycloak Secret in the cluster and never printed.
#
# Requires: kubectl (exec into the Keycloak pod, read/create Secrets), bash 4+, python3.
#
# USAGE:
#   ./setup-partner-realm.sh --namespace <ns> \
#       [--user <username>:<partner_id>:<ROLE>] ...   (e.g. bank-a-operator:bank-a:PARTNER_OPERATOR)
#       [--realm partner] [--client consent-partner-portal]
#       [--keycloak-pod commons-keycloak-0] [--admin-secret commons-keycloak] [--admin-secret-key admin-password]
#       [--dry-run]
#
# EXAMPLE:
#   ./setup-partner-realm.sh --namespace agrix --user bank-a-operator:bank-a:PARTNER_OPERATOR

set -euo pipefail

NAMESPACE=""
REALM="partner"
CLIENT="consent-partner-portal"
KC_POD="commons-keycloak-0"
ADMIN_SECRET="commons-keycloak"
ADMIN_SECRET_KEY="admin-password"
DRY_RUN=false
USERS=()

usage() { sed -n '2,32p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n)      NAMESPACE="$2"; shift 2 ;;
    --realm)             REALM="$2"; shift 2 ;;
    --client)            CLIENT="$2"; shift 2 ;;
    --keycloak-pod)      KC_POD="$2"; shift 2 ;;
    --admin-secret)      ADMIN_SECRET="$2"; shift 2 ;;
    --admin-secret-key)  ADMIN_SECRET_KEY="$2"; shift 2 ;;
    --user)              USERS+=("$2"); shift 2 ;;
    --dry-run)           DRY_RUN=true; shift ;;
    -h|--help)           usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done
[[ -z "$NAMESPACE" ]] && { echo "ERROR: --namespace is required"; exit 1; }
for u in "${USERS[@]+"${USERS[@]}"}"; do
  [[ "$u" =~ ^[^:]+:[^:]+:(PARTNER_OPERATOR|PARTNER_ADMIN)$ ]] \
    || { echo "ERROR: --user must be <username>:<partner_id>:PARTNER_OPERATOR|PARTNER_ADMIN (got '$u')"; exit 1; }
done

_green() { printf "\033[32m%s\033[0m\n" "$*"; }
_blue()  { printf "\033[34m%s\033[0m\n" "$*"; }

KCADM="/opt/bitnami/keycloak/bin/kcadm.sh"
kc() {
  # kcadm.sh in the Keycloak pod, logged in as the master-realm admin (config in /tmp).
  kubectl -n "$NAMESPACE" exec -i "$KC_POD" -c keycloak -- "$KCADM" "$@" --config /tmp/kcadm-partner.config
}
change() {
  echo "  > kcadm $*"
  [[ "$DRY_RUN" == true ]] || kc "$@"
}

_blue "==> Log in to Keycloak (pod $KC_POD)"
ADMIN_PW=$(kubectl -n "$NAMESPACE" get secret "$ADMIN_SECRET" -o jsonpath="{.data.$ADMIN_SECRET_KEY}" | base64 -d)
kubectl -n "$NAMESPACE" exec -i "$KC_POD" -c keycloak -- "$KCADM" config credentials --config /tmp/kcadm-partner.config \
  --server http://localhost:8080 --realm master --user admin --password "$ADMIN_PW" >/dev/null
unset ADMIN_PW

_blue "==> Realm '$REALM'"
if kc get "realms/$REALM" --fields realm >/dev/null 2>&1; then
  echo "  exists"
else
  change create realms -s "realm=$REALM" -s enabled=true
  if [[ "$DRY_RUN" == true ]]; then
    echo "  (dry-run: the realm does not exist yet, so the remaining steps cannot be planned)"
    exit 0
  fi
fi

_blue "==> Realm roles"
for role in PARTNER_OPERATOR PARTNER_ADMIN; do
  if kc get "roles/$role" -r "$REALM" --fields name >/dev/null 2>&1; then
    echo "  $role exists"
  else
    change create roles -r "$REALM" -s "name=$role" -s "description=Consent Manager partner portal: $role"
  fi
done

_blue "==> User-profile attribute partner_id"
PROFILE=$(kc get users/profile -r "$REALM")
if python3 -c 'import json,sys; sys.exit(0 if any(a.get("name")=="partner_id" for a in json.load(sys.stdin).get("attributes",[])) else 1)' <<<"$PROFILE"; then
  echo "  declared"
else
  NEW_PROFILE=$(python3 -c '
import json,sys
p=json.load(sys.stdin)
p.setdefault("attributes",[]).append({
  "name":"partner_id","displayName":"Partner ID",
  "permissions":{"view":["admin","user"],"edit":["admin"]},
  "validations":{"length":{"max":255}}, "multivalued":False})
print(json.dumps(p))' <<<"$PROFILE")
  echo "  > kcadm update users/profile (add partner_id: admin-edit, user-view)"
  [[ "$DRY_RUN" == true ]] || kc update users/profile -r "$REALM" -f - <<<"$NEW_PROFILE" >/dev/null
fi

_blue "==> Token mapper on client '$CLIENT'"
CID=$(kc get clients -r "$REALM" -q "clientId=$CLIENT" --fields id | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')
if [[ -z "$CID" ]]; then
  echo "  client '$CLIENT' not found in realm '$REALM' — install the CM with partnerPortal.enabled first (keycloak-init creates it)"
  exit 1
fi
if kc get "clients/$CID/protocol-mappers/models" -r "$REALM" --fields name | grep -q '"partner_id"'; then
  echo "  mapper exists"
else
  change create "clients/$CID/protocol-mappers/models" -r "$REALM" \
    -s name=partner_id -s protocol=openid-connect -s protocolMapper=oidc-usermodel-attribute-mapper \
    -s 'config."user.attribute"=partner_id' -s 'config."claim.name"=partner_id' -s 'config."jsonType.label"=String' \
    -s 'config."access.token.claim"=true' -s 'config."id.token.claim"=true' -s 'config."userinfo.token.claim"=true'
fi

for spec in "${USERS[@]+"${USERS[@]}"}"; do
  IFS=: read -r username partner_id role <<<"$spec"
  _blue "==> Partner user '$username' (partner $partner_id, $role)"
  if kc get users -r "$REALM" -q "username=$username" -q exact=true --fields id | grep -q '"id"'; then
    echo "  exists (attribute, role and password left as they are)"
    continue
  fi
  SECRET_NAME="partner-user-$username"
  if kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
    PW=$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o jsonpath='{.data.password}' | base64 -d)
  else
    PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
    echo "  > kubectl create secret generic $SECRET_NAME (username, password)"
    [[ "$DRY_RUN" == true ]] || kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
      --from-literal=username="$username" --from-literal=password="$PW" >/dev/null
  fi
  change create users -r "$REALM" -s "username=$username" -s enabled=true -s emailVerified=true \
    -s "attributes.partner_id=[\"$partner_id\"]"
  echo "  > kcadm set-password $username (from Secret $SECRET_NAME)"
  [[ "$DRY_RUN" == true ]] || kc set-password -r "$REALM" --username "$username" --new-password "$PW"
  change add-roles -r "$REALM" --uusername "$username" --rolename "$role"
  unset PW
  _green "  password in Secret $NAMESPACE/$SECRET_NAME"
done

kubectl -n "$NAMESPACE" exec "$KC_POD" -c keycloak -- rm -f /tmp/kcadm-partner.config >/dev/null 2>&1 || true
_green "==> Done."
[[ "$DRY_RUN" == true ]] && echo "    (dry-run: nothing was changed)"
exit 0
