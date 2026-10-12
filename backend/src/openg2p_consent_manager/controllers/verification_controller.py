import logging
from datetime import datetime, timezone

from fastapi import Body
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController
from pydantic import ValidationError

from ..config import Settings
from ..schemas.common import ReasonCode, ReceiptStatusResponse, StatusResponse
from ..schemas.verification import Decision, ValidateRequest
from ..services import ConsentService, ExchangeService, VerificationService

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


_VALIDATE_DESCRIPTION = """
Policy decision for one registry (data controller) presenting a partner-signed
consent JWS.

**Consent claims** (JWS payload, signed with the partner's PM-registered key):
`jti`, `aud` (the partner's audience), `subject_id {type, value}`,
`purpose {code}`, `fetch_type`, `validity {valid_from, valid_until}`,
`issued_at`, and either

- `grants: [{data_controller, data_scopes}]` — one consent, one grant per
  registry; or
- legacy `data_controller` + `data_scopes` — treated as a single grant.

**Request:** `consent_jws`, optional `partner_id`, optional `data_controller`
(the calling registry), optional `request_context {requested_scopes, subject_id}`.

- Consent with `grants`: `data_controller` is required (deny `malformed_object`
  if missing) and selects the grant (deny `controller_not_granted` if the
  consent has none for it).
- Legacy consent: if `data_controller` is given it must equal the consent's
  (deny `controller_not_granted`).
- The partner must have an active binding to that controller (deny
  `unknown_partner`). Policies are per (partner audience, controller).
- Effective scopes = grant scopes ∩ that binding's policy (∩
  `requested_scopes` if sent).
- `request_context.subject_id` with the same type as the consent subject but a
  different value: deny `subject_mismatch`. Other types are for the registry to
  resolve.
- Replay/idempotency is per (`jti`, `data_controller`): the same consent
  validated by two registries gives two decisions and two receipts; a repeat
  for the same controller returns the stored decision.

**Response:** the decision (`permit`/`deny`, `reason_code`, `detail`), and on
permit `consent_id`, `receipt_id`, `subject_id` (the consent's subject),
`data_controller`, `effective_data_scopes`, `valid_until`, `policy_version`.

**Agri Stack exchange (opt-in, off by default):**

- `issue_receipts: true` (exchange role) — only for a caller whose
  `partner_id` is in the CM's `receipt_presenters` (else deny
  `receipt_presenter_not_allowed`). Every granted controller (or just
  `data_controller`) is validated as above; if all permit, the response adds
  `receipts: {data_controller: "<receipt JWS>"}` — one signed consent receipt
  (`typ: consent-receipt+jwt`) per controller, verifiable with this CM's JWKS.
- A consent receipt as `consent_jws` (department role) — accepted only from a
  configured trusted issuer (else deny `receipt_issuer_not_trusted`); its
  `aud` must be `data_controller` and its `presenter` the caller's
  `partner_id`. Effective scopes = receipt scopes ∩ this CM's policy for the
  presenter at that controller.

**A stored consent by its ID** (instead of `consent_jws`; exactly one of the
two, else HTTP 400): `consent_id` and `consent_partner_id` (the partner that
obtained it). The consent must be an originated one (deny `unknown_consent`),
active (`revoked` / `expired` / `not_yet_valid`), obtained by
`consent_partner_id` (`partner_mismatch`) and for the subject in
`request_context.subject_id` (`subject_mismatch`). Per grant, effective scopes
= the consent's effective scopes ∩ the partner's CURRENT policy for that
controller ∩ `requested_scopes`; a controller left with none is not granted.
The permit carries `grants: {data_controller: [scopes]}` (also returned with
`issue_receipts`); with `issue_receipts`, one receipt per granted controller.
"""


class VerificationController(BaseController):
    """PARTNER-api PDP endpoints — the registry/PEP hot path.

    Trust is NOT Keycloak: it's the partner-signed consent object, verified inside
    ``validate`` against the partner's keys from Partner Management (replay-guarded
    per ``jti`` and data controller). The partner api carries no Keycloak realm;
    registry↔CM is secured at the transport layer (Istio mTLS / network policy).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.verification = VerificationService.get_component()
        self.consents = ConsentService.get_component()
        self.exchange = ExchangeService.get_component()
        self.router.prefix += "/consent/v1"
        self.router.tags += ["Verification"]

        self.router.add_api_route(
            "/validate", self.validate,
            responses={200: {"model": Decision}}, methods=["POST"],
            summary="Validate a partner-signed consent for one data controller",
            description=_VALIDATE_DESCRIPTION,
        )
        self.router.add_api_route(
            "/consents/{consent_id}/status", self.get_status,
            responses={200: {"model": StatusResponse}}, methods=["GET"],
        )
        # Agri Stack exchange: status of a consent receipt this CM issued.
        # Partner-api convention (like consent status): no Keycloak; the jti is
        # an unguessable UUID and the answer is only active/revoked/expired.
        self.router.add_api_route(
            "/receipts/{jti}/status", self.get_receipt_status,
            responses={200: {"model": ReceiptStatusResponse}}, methods=["GET"],
            summary="Status of an issued consent receipt (exchange role)",
        )
        # Receipt fetch is public — the signature makes it self-verifying.
        self.router.add_api_route(
            "/receipts/{receipt_id}", self.get_receipt, methods=["GET"],
        )

    async def validate(self, payload: dict = Body(...)) -> Decision:
        # Parse defensively so a malformed request is a clean 200 deny, not a
        # 4xx (the partner api's contract). The consent object itself is a
        # compact JWS (consent_jws) or a stored consent's ID (consent_id); its
        # claims and signature are validated inside the verification service.
        if not isinstance(payload, dict) or bool(payload.get("consent_jws")) == bool(
            payload.get("consent_id")
        ):
            return Decision(
                decision="deny", reason_code=ReasonCode.malformed_object,
                detail="give exactly one of 'consent_jws' or 'consent_id'",
                evaluated_at=datetime.now(timezone.utc),
            )
        try:
            parsed = ValidateRequest(**payload)
        except ValidationError as exc:
            _logger.info("Malformed validate request: %s", exc)
            return Decision(
                decision="deny", reason_code=ReasonCode.malformed_object,
                detail="validate request failed schema validation",
                evaluated_at=datetime.now(timezone.utc),
            )
        return await self.verification.validate(parsed)

    async def get_status(self, consent_id: str):
        result = await self.consents.get_status(consent_id)
        if result is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return StatusResponse(**result)

    async def get_receipt_status(self, jti: str):
        result = await self.exchange.receipt_status(jti)
        if result is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return ReceiptStatusResponse(**result)

    async def get_receipt(self, receipt_id: str):
        receipt = await self.consents.get_receipt(receipt_id)
        if receipt is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return JSONResponse(content=receipt.document)
