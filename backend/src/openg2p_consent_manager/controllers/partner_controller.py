import logging
from typing import Optional

from fastapi import Depends, Header, Query
from fastapi.responses import JSONResponse
from openg2p_fastapi_common.controller import BaseController

from ..auth import CallerIdentity, require_role
from ..config import Settings
from ..models import PolicyStatus
from ..schemas.partner import (
    PartnerCreate,
    PartnerResponse,
    PartnerUpdate,
    PolicyResponse,
    PolicyUpsert,
)
from ..services import (
    PartnerConflict,
    PartnerService,
    PolicyNotResubmittable,
    PolicyPending,
)
from ..services.awe_client import AweClient, AweClientError

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)

_NOT_FOUND = JSONResponse(status_code=404, content={"error": "not_found"})

# One dependency object, used both as the route guard and as the handler
# parameter that needs the identity: FastAPI then evaluates it once per request.
_require_admin = require_role(_config.auth_admin_role)

# AWE artifact type for policy-change approvals (the inbox filters on it).
AWE_ARTIFACT_TYPE = "consent_manager.policy_change"


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :] or None
    return None


def _requester(identity: Optional[CallerIdentity]) -> Optional[str]:
    """The acting admin as AWE should record them: the username approvers see
    (AWE matches assignees on preferred_username too), else the subject."""
    if identity is None:
        return None
    claims = identity.raw_claims or {}
    return claims.get("preferred_username") or identity.subject or None


class PartnerController(BaseController):
    """Administrative policy-binding + data-share-policy management.

    A "partner" here is a policy BINDING: partner identity, lifecycle and signing
    keys are owned by the Partner Management service (CM stores only the PM
    reference `partner_mgmt_id` and fetches keys at verification time). Widening a
    binding's data-share policy is gated behind AWE approval (see upsert_policy).

    One partner (audience) may be bound to several data controllers: POST another
    binding with the same ``audience`` and a different ``controller_id``. Each
    binding has its own id and its own versioned policy (the policy endpoints are
    per binding id), so policies are per (audience, controller). List a partner's
    bindings with ``GET /partners?audience=...``.
    """

    # TODO(partner-delete): add a SOFT delete for bindings (audit-safe).
    #   A partner is referenced by ConsentArtefact / ConsentReceipt / ConsentRequest
    #   / DecisionLog / AuditLog, so it must NEVER be hard-deleted — that would
    #   orphan the audit trail and break non-repudiation. Plan:
    #     1. Add an `archived` value to PartnerStatus (alongside active/suspended);
    #        like suspended it fails validation (validate filters status==active).
    #     2. Add DELETE /partners/{id} that does a SOFT delete: set status=archived,
    #        keep the row, return 200.
    #     3. Switch the sanity e2e cleanup from PATCH suspended -> DELETE (archived).
    #   Until then, "delete" = PATCH /partners/{id} {status: "suspended"}.

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.partners = PartnerService.get_component()
        self.awe = AweClient.get_component()
        self.router.prefix += "/consent/v1/partners"
        self.router.tags += ["Partners & Policy"]

        # All partner/policy admin endpoints require the admin role.
        admin = [Depends(_require_admin)]

        self.router.add_api_route(
            "", self.list_partners, dependencies=admin,
            responses={200: {"model": list[PartnerResponse]}}, methods=["GET"],
        )
        self.router.add_api_route(
            "", self.create_partner, dependencies=admin,
            responses={201: {"model": PartnerResponse}}, methods=["POST"], status_code=201,
        )
        self.router.add_api_route(
            "/{partner_id}", self.get_partner, dependencies=admin,
            responses={200: {"model": PartnerResponse}}, methods=["GET"],
        )
        self.router.add_api_route(
            "/{partner_id}", self.update_partner, dependencies=admin,
            responses={200: {"model": PartnerResponse}}, methods=["PATCH"],
        )
        self.router.add_api_route(
            "/{partner_id}/policy", self.upsert_policy, dependencies=admin,
            responses={200: {"model": PolicyResponse}}, methods=["PUT"],
        )
        self.router.add_api_route(
            "/{partner_id}/policy", self.get_policy, dependencies=admin,
            responses={200: {"model": PolicyResponse}}, methods=["GET"],
        )
        self.router.add_api_route(
            "/{partner_id}/policies", self.list_policies, dependencies=admin,
            responses={200: {"model": list[PolicyResponse]}}, methods=["GET"],
        )
        self.router.add_api_route(
            "/{partner_id}/policies/{version}/resubmit", self.resubmit_policy,
            dependencies=admin, responses={200: {"model": PolicyResponse}},
            methods=["POST"],
        )

    async def list_partners(
        self,
        controller_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        audience: Optional[str] = Query(
            None, description="Only the bindings of this partner audience"
        ),
    ):
        partners = await self.partners.list_partners(
            controller_id=controller_id, status=status, audience=audience
        )
        return [PartnerResponse.model_validate(p) for p in partners]

    async def create_partner(self, data: PartnerCreate):
        # A binding is created active; partner identity onboarding is PM's job.
        # 409 if (audience, controller_id) is already bound, or if the audience's
        # other bindings use a different partner_mgmt_id.
        try:
            partner = await self.partners.create_partner(data)
        except PartnerConflict as exc:
            return JSONResponse(
                status_code=409, content={"error": "conflict", "detail": exc.detail}
            )
        return PartnerResponse.model_validate(partner)

    async def get_partner(self, partner_id: str):
        partner = await self.partners.get_partner(partner_id)
        if partner is None:
            return _NOT_FOUND
        return PartnerResponse.model_validate(partner)

    async def update_partner(self, partner_id: str, data: PartnerUpdate):
        partner = await self.partners.update_partner(partner_id, data)
        if partner is None:
            return _NOT_FOUND
        return PartnerResponse.model_validate(partner)

    async def upsert_policy(
        self,
        partner_id: str,
        data: PolicyUpsert,
        identity: CallerIdentity = Depends(_require_admin),
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        partner = await self.partners.get_partner(partner_id)
        if partner is None:
            return _NOT_FOUND
        try:
            policy = await self.partners.upsert_policy(partner_id, data)
        except PolicyPending as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "policy_pending",
                    "detail": exc.detail,
                    "pending_version": exc.pending_version,
                },
            )
        except PartnerConflict as exc:
            return JSONResponse(
                status_code=409, content={"error": "conflict", "detail": exc.detail}
            )
        if policy is None:
            return _NOT_FOUND
        return await self._submit_if_pending(partner, policy, identity, authorization)

    async def resubmit_policy(
        self,
        partner_id: str,
        version: int,
        identity: CallerIdentity = Depends(_require_admin),
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ):
        """Copy a failed / stale / rejected version into a new version and save
        it again (re-evaluated against the current active policy; submitted to
        AWE if it still widens)."""
        partner = await self.partners.get_partner(partner_id)
        if partner is None:
            return _NOT_FOUND
        try:
            policy = await self.partners.resubmit_policy(partner_id, version)
        except PolicyNotResubmittable as exc:
            return JSONResponse(
                status_code=409, content={"error": "not_resubmittable", "detail": exc.detail}
            )
        except PolicyPending as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "policy_pending",
                    "detail": exc.detail,
                    "pending_version": exc.pending_version,
                },
            )
        except PartnerConflict as exc:
            return JSONResponse(
                status_code=409, content={"error": "conflict", "detail": exc.detail}
            )
        if policy is None:
            return _NOT_FOUND
        return await self._submit_if_pending(partner, policy, identity, authorization)

    async def _submit_if_pending(self, partner, policy, identity, authorization):
        """A widening comes back `pending` — submit it to AWE for approval. It
        stays pending (the prior active policy remains in force) until AWE's
        terminal webhook decides it. If the submission fails, the version is
        marked `failed` (never left pending without an AWE request) and 502 is
        returned; resubmit it once AWE is reachable."""
        if policy.status != PolicyStatus.pending.value:
            return PolicyResponse.model_validate(policy)

        context = {
            "partner_label": partner.name or partner.partner_mgmt_id or partner.audience,
            "partner_mgmt_id": partner.partner_mgmt_id,
            "audience": partner.audience,
            "controller_id": partner.controller_id,
            "policy_version": policy.version,
            "base_version": policy.base_version,
            "allowed_data_scopes": policy.allowed_data_scopes,
            "allowed_purposes": policy.allowed_purposes,
            "allowed_subject_id_types": policy.allowed_subject_id_types,
            "max_validity_duration": policy.max_validity_duration,
            "fetch_type": policy.fetch_type,
            "max_fetch_frequency": policy.max_fetch_frequency,
            "data_life": policy.data_life,
        }
        try:
            request_id = await self.awe.create_request(
                artifact_type=AWE_ARTIFACT_TYPE,
                artifact_id=policy.id,
                context=context,
                requester=_requester(identity),
                caller_bearer=_bearer(authorization),
            )
        except AweClientError as exc:
            _logger.error("AWE policy-change submit failed for %s: %s", policy.id, exc)
            await self.partners.mark_policy_submit_failed(policy.id, exc.message)
            return JSONResponse(
                status_code=502,
                content={
                    "error": "awe_submit_failed",
                    "detail": (
                        f"Could not submit policy v{policy.version} for approval: "
                        f"{exc.message}. The version was marked failed and the "
                        f"active policy is unchanged; resubmit it once the "
                        f"approval service is reachable."
                    ),
                    "message": exc.message,
                    "awe_status": exc.status_code,
                    "policy_id": policy.id,
                    "version": policy.version,
                    "status": PolicyStatus.failed.value,
                },
            )
        await self.partners.set_policy_awe_request_id(policy.id, request_id)
        # Re-read: a fast decision (e.g. a policy with no stages) may already
        # have been applied by the webhook.
        fresh = await self.partners.get_policy(policy.partner_id, policy.version)
        return PolicyResponse.model_validate(fresh or policy)

    async def get_policy(self, partner_id: str, version: Optional[int] = Query(None)):
        policy = await self.partners.get_policy(partner_id, version)
        if policy is None:
            return _NOT_FOUND
        return PolicyResponse.model_validate(policy)

    async def list_policies(self, partner_id: str):
        policies = await self.partners.list_policies(partner_id)
        if policies is None:
            return _NOT_FOUND
        return [PolicyResponse.model_validate(p) for p in policies]
