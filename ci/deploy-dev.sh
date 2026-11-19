#!/usr/bin/env bash
# Upgrade the New Dev commons-services release (namespace commons, the registries
# cluster) so it runs this build's Consent Manager images. Called by the
# Jenkinsfile with KUBECONFIG set from the Jenkins credential, IMAGE_TAG
# (<version>-<branch>.<build>, ci/image_names.py) in the environment and
# .build/{repository,digest}-<image> written by ci/build-push.sh.
#
# Safety, in order:
#  1. the chart is the version the release already runs (never a newer or older
#     commons-services), fetched from oan-helm and checked against its SHA256SUMS
#  2. that chart must still pin openg2p-consent-manager $CONSENT_CHART_PIN (1.0.2)
#  3. a server-side dry run is compared with the live manifest; only image
#     references on the consent-manager resources may change (ci/manifest_guard.py)
#  4. real upgrade with --reuse-values (everything else stays as installed)
#  5. consent deployments must roll out; otherwise the release is rolled back to
#     the last `deployed` revision and the build fails
set -euo pipefail

: "${KUBECONFIG:?}" "${IMAGE_TAG:?}" "${HELM_CHART_NAME:?}" "${OAN_HELM_RAW:?}" \
  "${COMMONS_RELEASE:?}" "${COMMONS_NAMESPACE:?}" "${CONSENT_CHART_PIN:?}"
NS=$COMMONS_NAMESPACE
REL=$COMMONS_RELEASE
WORK=.build
mkdir -p "$WORK"

# Last good revision (for rollback) and the chart version it runs.
if ! helm list -n "$NS" --deployed -q --filter "^${REL}\$" | grep -qx "$REL"; then
    echo "ERROR: release $REL is not deployed in $NS; this pipeline only upgrades it" >&2
    exit 1
fi
read -r PREV LIVE_CHART < <(helm list -n "$NS" --deployed --filter "^${REL}\$" -o json \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)[0]; print(r["revision"], r["chart"])')
case "$LIVE_CHART" in
    "${HELM_CHART_NAME}-"?*) HELM_CHART_VERSION=${LIVE_CHART#"${HELM_CHART_NAME}-"} ;;
    *) echo "ERROR: release $REL runs chart '$LIVE_CHART', expected ${HELM_CHART_NAME}-<version>" >&2; exit 1 ;;
esac
echo "Last deployed revision: $PREV ($LIVE_CHART)"
CHART="$WORK/${LIVE_CHART}.tgz"

# 1. the live chart's archive, checked against oan-helm's published checksum
curl -fsSL -o "$CHART" "${OAN_HELM_RAW}/repository/${LIVE_CHART}.tgz"
curl -fsSL -o "$WORK/SHA256SUMS" "${OAN_HELM_RAW}/repository/SHA256SUMS"
SUM=$(awk -v f="${LIVE_CHART}.tgz" '$2 == f || $2 == "*" f {print $1; exit}' "$WORK/SHA256SUMS")
[ -n "$SUM" ] || { echo "ERROR: ${LIVE_CHART}.tgz is not in oan-helm repository/SHA256SUMS" >&2; exit 1; }
echo "${SUM}  $CHART" | sha256sum -c -

# 2. Consent Manager chart version must not move
FOUND=$(helm show chart "$CHART" | awk '/name: openg2p-consent-manager/{f=1} f && /version:/{print $2; exit}')
if [ "$FOUND" != "$CONSENT_CHART_PIN" ]; then
    echo "ERROR: $HELM_CHART_NAME $HELM_CHART_VERSION pins openg2p-consent-manager '${FOUND}', expected ${CONSENT_CHART_PIN}" >&2
    exit 1
fi

# Images built in this run (eoan/consent-management/...), pinned by digest. The
# live values supply the extraApis list (the partner api pins its own image).
helm get values "$REL" -n "$NS" -a -o json > "$WORK/live-values.json"
python3 ci/image_values.py "$WORK/live-values.json" "$WORK" "$IMAGE_TAG" > "$WORK/images.yaml"
rm -f "$WORK/live-values.json"
VALS="--reuse-values -f deploy/dev/values-consent.yaml -f $WORK/images.yaml"

helm get manifest "$REL" -n "$NS" > "$WORK/live-manifest.yaml"

# 3. dry run against the server, then the guard
helm upgrade "$REL" "$CHART" -n "$NS" $VALS --dry-run=server > "$WORK/dryrun.txt"
GUARD_ONLY='/commons-services-cm-' GUARD_REVISION="$PREV" python3 ci/manifest_guard.py "$WORK/live-manifest.yaml" "$WORK/dryrun.txt" "$WORK/guard-report.txt"

diag() {
    {
        echo "== helm history"; helm history "$REL" -n "$NS" --max 5
        echo "== consent pods"; kubectl get pods -n "$NS" -o wide | grep -iE 'NAME|cm-' || true
        echo "== pods not Running/Succeeded"; kubectl get pods -n "$NS" --field-selector=status.phase!=Running,status.phase!=Succeeded || true
        echo "== events"; kubectl get events -n "$NS" --sort-by=.lastTimestamp | tail -40
        for d in cm-api cm-partner-api cm-ui cm-partner-portal; do
            echo "== logs ${REL}-$d"; kubectl logs -n "$NS" "deploy/${REL}-$d" --all-containers --tail=40 2>&1 || true
        done
    } > "deploy-failure-${BUILD_NUMBER:-0}.txt" 2>&1 || true
}

# 4. upgrade (hooks are always waited for; no --wait so an unrelated slow pod in
#    commons cannot fail or roll back a consent-only change)
set +e
helm upgrade "$REL" "$CHART" -n "$NS" $VALS --timeout 25m
RC=$?

# 5. the consent deployments must actually roll out
if [ "$RC" -eq 0 ]; then
    for d in cm-api cm-partner-api cm-ui cm-partner-portal; do
        kubectl rollout status "deployment/${REL}-$d" -n "$NS" --timeout=600s || RC=1
    done
fi
set -e

if [ "$RC" -ne 0 ]; then
    diag
    echo "Deploy failed; rolling back to revision $PREV"
    helm rollback "$REL" "$PREV" -n "$NS" --timeout 25m
    echo "Rolled back to revision $PREV"
    exit 1
fi
helm status "$REL" -n "$NS" | head -12
