#!/usr/bin/env bash
# Upgrade the dev commons-services release so it runs this commit's Consent
# Manager images. Called by the Jenkinsfile with KUBECONFIG set from the Jenkins
# credential and IMAGE_TAG (12-char commit) in the environment.
#
# Safety, in order:
#  1. the commons-services chart is the pinned, checksummed archive from oan-helm
#  2. that chart must still pin openg2p-consent-manager $CONSENT_CHART_PIN (1.0.2)
#  3. a server-side dry run is compared with the live manifest; only image
#     references on the consent-manager resources may change (ci/manifest_guard.py)
#  4. real upgrade with --reuse-values (everything else stays as installed)
#  5. consent deployments must roll out; otherwise the release is rolled back to
#     the last `deployed` revision and the build fails
set -euo pipefail

: "${KUBECONFIG:?}" "${IMAGE_TAG:?}" "${HELM_CHART_NAME:?}" "${HELM_CHART_VERSION:?}" \
  "${HELM_CHART_SHA256:?}" "${OAN_HELM_RAW:?}" "${COMMONS_RELEASE:?}" "${COMMONS_NAMESPACE:?}" \
  "${CONSENT_CHART_PIN:?}" "${ECR_REGISTRY:?}"
NS=$COMMONS_NAMESPACE
REL=$COMMONS_RELEASE
WORK=.build
mkdir -p "$WORK"
CHART="$WORK/${HELM_CHART_NAME}-${HELM_CHART_VERSION}.tgz"

# 1. pinned chart archive
curl -fsSL -o "$CHART" "${OAN_HELM_RAW}/repository/${HELM_CHART_NAME}-${HELM_CHART_VERSION}.tgz"
echo "${HELM_CHART_SHA256}  $CHART" | sha256sum -c -

# 2. Consent Manager chart version must not move
FOUND=$(helm show chart "$CHART" | awk '/name: openg2p-consent-manager/{f=1} f && /version:/{print $2; exit}')
if [ "$FOUND" != "$CONSENT_CHART_PIN" ]; then
    echo "ERROR: $HELM_CHART_NAME $HELM_CHART_VERSION pins openg2p-consent-manager '${FOUND}', expected ${CONSENT_CHART_PIN}" >&2
    exit 1
fi

# Images built in this run, pinned by digest.
dig() { cat "$WORK/digest-$1"; }
cat > "$WORK/images.yaml" <<EOF
openg2p-consent-manager:
  consentManagerApi:
    image:
      tag: "dev-${IMAGE_TAG}@$(dig consent-manager-api)"
  ui:
    image:
      tag: "dev-${IMAGE_TAG}@$(dig consent-manager-ui)"
  sanity:
    image:
      tag: "dev-${IMAGE_TAG}@$(dig consent-manager-sanity)"
consentPartnerPortal:
  image:
    digest: "$(dig consent-manager-partner-portal)"
EOF
VALS="--reuse-values -f deploy/dev/values-consent.yaml -f $WORK/images.yaml"

# Last good revision (for rollback) and the live manifest.
PREV=""
if helm list -n "$NS" --deployed -q --filter "^${REL}\$" | grep -qx "$REL"; then
    PREV=$(helm status "$REL" -n "$NS" -o json | sed -n 's/.*"version":\([0-9][0-9]*\).*/\1/p' | head -1)
else
    echo "ERROR: release $REL is not deployed in $NS; this pipeline only upgrades it" >&2
    exit 1
fi
echo "Last deployed revision: $PREV"
helm get manifest "$REL" -n "$NS" > "$WORK/live-manifest.yaml"

# 3. dry run against the server, then the guard
helm upgrade "$REL" "$CHART" -n "$NS" $VALS --dry-run=server > "$WORK/dryrun.txt"
GUARD_ONLY='/commons-services-cm-' python3 ci/manifest_guard.py "$WORK/live-manifest.yaml" "$WORK/dryrun.txt" "$WORK/guard-report.txt"

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
