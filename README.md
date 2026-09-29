# Consent Manager

OpenG2P Consent Management Service.

Documentation (design, API, development, deployment) lives in the OpenG2P GitBook:
https://docs.openg2p.org/ → **Consent Management**
(source: `openg2p-documentation/consent-management/`).

## Setup guides

- **[DEPLOYMENT.md](DEPLOYMENT.md)** — deploy so everything works (docker images,
  AWE approvals, the async aggregator): dependencies, config model, feature
  flags, verification, and a troubleshooting table. **Start here for DevOps.**
- **[RUN-LOCAL.md](RUN-LOCAL.md)** — local development.
- **[KAFKA.md](KAFKA.md)** — the aggregation fan-out / callback queues.
- **`backend/.env.example`** — every configuration variable, documented.

## Aggregator and Kafka

The aggregated fetch (`POST /dci/registry/async/search`) queues its registry
fan-out and its partner callback on Kafka when
`CONSENT_MANAGER_KAFKA_ENABLED=true`, so a burst of subjects verifying their
OTP at the same time cannot saturate the API process or the registries, and a
partner webhook that is down gets retried instead of losing the record. With
the flag off it runs in-process exactly as before.
