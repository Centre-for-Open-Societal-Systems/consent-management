#!/usr/bin/env bash
# Build one image, push it to ECR and record what was pushed.
#
#   ci/build-push.sh <image> <dockerfile> <context> <buildinfo yes|no>
#
# Name: ${ECR_REGISTRY}/eoan/consent-management/<image>-<CM version>:${IMAGE_TAG}
# (ci/image_names.py, same standard as the registries). The ECR repository is
# created when it does not exist yet (scan on push, like the registry repos).
# Writes .build/repository-<image> (full repository URL) and .build/digest-<image>
# (sha256:<digest>), which the deploy pins.
set -euo pipefail

NAME=$1; DOCKERFILE=$2; CONTEXT=$3; BUILDINFO=${4:-no}
: "${ECR_REGISTRY:?ECR_REGISTRY not set}" "${AWS_REGION:?AWS_REGION not set}" "${IMAGE_TAG:?IMAGE_TAG not set}"

REPO=$(python3 ci/image_names.py repository "$NAME")
IMAGE="${ECR_REGISTRY}/${REPO}:${IMAGE_TAG}"

if ! aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$REPO" >/dev/null 2>&1; then
    echo "creating ECR repository $REPO"
    aws ecr create-repository --region "$AWS_REGION" --repository-name "$REPO" \
        --image-scanning-configuration scanOnPush=true >/dev/null
fi

ARGS=()
if [ "$BUILDINFO" = "yes" ]; then
    ARGS+=(--build-arg "GIT_COMMIT=${GIT_COMMIT:-unknown}" --build-arg "BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)")
fi

# ${ARGS[@]+...} keeps an empty array legal under `set -u` on bash < 4.4
docker build ${ARGS[@]+"${ARGS[@]}"} -f "$DOCKERFILE" -t "$IMAGE" "$CONTEXT"
docker push "$IMAGE"

# Digest of the manifest that was just pushed (RepoDigests is filled by the push).
DIGEST=$(docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE" \
    | grep "^${ECR_REGISTRY}/${REPO}@" | head -1 | cut -d@ -f2)
[ -n "$DIGEST" ] || { echo "ERROR: no digest recorded for $IMAGE" >&2; exit 1; }
mkdir -p .build
echo "${ECR_REGISTRY}/${REPO}" > ".build/repository-${NAME}"
echo "$DIGEST" > ".build/digest-${NAME}"
echo "pushed ${IMAGE}@${DIGEST}"
