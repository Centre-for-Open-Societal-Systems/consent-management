FROM python:3.12-slim-bookworm

# External OpenG2P framework ref. Override via build-arg / CI input.
# Pinned to a commit with openg2p_fastapi_common.utils.crypto, which the code
# imports (tag 1.2.0 has it under openg2p_fastapi_common.crypto instead, so the
# service would not start). This is develop as CI resolved it on 2026-10-16.
ARG FASTAPI_COMMON_REF=164f07f7e7f38944b60cb1528d2582d025b830b5

# Only git is needed (to pip-install the framework from GitHub). All Python deps
# ship prebuilt wheels — no compiler/headers required.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Framework (installed from git, like the other OpenG2P services).
RUN pip install --no-cache-dir \
    "git+https://github.com/openg2p/openg2p-fastapi-common@${FASTAPI_COMMON_REF}#subdirectory=openg2p-fastapi-common"

# Local source. Build context = repo root, so the image always reflects the
# current branch.
COPY backend /src/consent-manager
RUN pip install --no-cache-dir /src/consent-manager

# Startup smoke check: fail the BUILD, not the pod, when the framework ref does
# not provide what the code imports (every service module is imported here).
RUN python3 -c "import openg2p_consent_manager.app"

ENV PYTHONUNBUFFERED=1
ENV CONSENT_MANAGER_APP_HOST=0.0.0.0
ENV CONSENT_MANAGER_APP_PORT=8000
ENV CONSENT_MANAGER_NO_OF_WORKERS=4

# Run migrations, then serve. `&&` (not `;`) so a FAILED migrate crashes the pod
# and Kubernetes retries — never serve against a half-created / empty schema.
# create_migrate() is idempotent (CREATE TABLE IF NOT EXISTS), so concurrent
# replicas are safe.
CMD python3 -m openg2p_consent_manager.main migrate && \
    gunicorn "openg2p_consent_manager.main:app" \
      --workers ${CONSENT_MANAGER_NO_OF_WORKERS} \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind ${CONSENT_MANAGER_APP_HOST}:${CONSENT_MANAGER_APP_PORT} \
      --log-level info
