# Consent Manager

OpenG2P Consent Management Service.

Documentation (design, API, development, deployment) lives in the OpenG2P GitBook:
https://docs.openg2p.org/ → **Consent Management**
(source: `openg2p-documentation/consent-management/`).

## Aggregator and Kafka

The aggregated fetch (`POST /dci/registry/async/search`) queues its registry
fan-out and its partner callback on Kafka when
`CONSENT_MANAGER_KAFKA_ENABLED=true`, so a burst of subjects verifying their
OTP at the same time cannot saturate the API process or the registries, and a
partner webhook that is down gets retried instead of losing the record. With
the flag off it runs in-process exactly as before.
