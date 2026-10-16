import logging
from datetime import datetime, timezone
from typing import Optional

from openg2p_fastapi_common.service import BaseService
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..db import async_session
from ..models import (
    Partner,
    PartnerPolicy,
    PartnerStatus,
    PolicyStatus,
)
from ..utils import TTLCache, iso_duration_to_timedelta

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class PartnerConflict(Exception):
    """A binding request conflicts with existing bindings (HTTP 409)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class PolicyPending(Exception):
    """A widening was saved while another version awaits approval (HTTP 409)."""

    def __init__(self, pending_version: int):
        super().__init__(f"policy v{pending_version} is awaiting approval")
        self.pending_version = pending_version
        self.detail = (
            f"Policy v{pending_version} is awaiting approval. Wait for its decision "
            f"before submitting another widening (narrowing changes still apply "
            f"immediately)."
        )


class PolicyNotResubmittable(Exception):
    """Only failed / stale / rejected versions can be resubmitted (HTTP 409)."""

    def __init__(self, version: int, status: str):
        super().__init__(f"policy v{version} is {status}")
        self.detail = (
            f"Policy v{version} is {status}; only failed, stale or rejected "
            f"versions can be resubmitted."
        )


_RESUBMITTABLE = {
    PolicyStatus.failed.value,
    PolicyStatus.stale.value,
    PolicyStatus.rejected.value,
}

_FETCH_RANK = {"oneshot": 0, "periodic": 1}


def _fetch_rank(fetch_type) -> int:
    value = getattr(fetch_type, "value", fetch_type)
    # Unknown values rank highest so a change to them needs approval.
    return _FETCH_RANK.get(value, 2) if value is not None else 0


class _PolicyCopy:
    """The policy fields of a stored version, shaped like a PolicyUpsert."""

    _FIELDS = (
        "allowed_data_scopes", "allowed_purposes", "allowed_subject_id_types",
        "allowed_signing_algs", "max_validity_duration", "fetch_type",
        "max_fetch_frequency", "data_life",
    )

    def __init__(self, source):
        for field in self._FIELDS:
            value = getattr(source, field)
            setattr(self, field, list(value) if isinstance(value, list) else value)


def _cache_key(audience: str, controller_id: str) -> str:
    return f"{audience}\x1f{controller_id}"


class PartnerService(BaseService):
    """Partner *policy bindings* (admin) plus the cached lookups the hot path
    needs. Partner identity + keys live in Partner Management; a row here binds a
    PM partner to a controller and a versioned data-share policy. One partner
    (audience) may have several bindings — one per controller."""

    def __init__(self, name="", **kwargs):
        super().__init__(name, **kwargs)
        self._cache = TTLCache(_config.partner_cache_ttl_sec)

    # ── Admin: bindings ──────────────────────────────────────────────────────

    async def create_partner(self, data) -> Partner:
        """Create a binding of a partner (audience) to one controller.

        A binding is created active. Partner *identity* onboarding/approval is
        Partner Management's concern; CM only gates data-share POLICY widening
        (see upsert_policy). A binding with no policy simply denies everything
        until a policy is set.

        The same audience may be bound to several controllers (one binding and
        policy per controller). All bindings of one audience share its PM
        reference: a new binding inherits ``partner_mgmt_id`` from the existing
        ones when omitted, and may not name a different one. Raises
        PartnerConflict on a duplicate (audience, controller) or a PM-reference
        mismatch.
        """
        async with async_session()() as session:
            result = await session.execute(
                select(Partner).where(Partner.audience == data.audience)
            )
            siblings = list(result.scalars().all())
            if any(p.controller_id == data.controller_id for p in siblings):
                raise PartnerConflict(
                    f"audience '{data.audience}' is already bound to controller "
                    f"'{data.controller_id}'"
                )
            partner_mgmt_id = data.partner_mgmt_id
            if siblings:
                existing_ref = siblings[0].partner_mgmt_id
                if partner_mgmt_id is None:
                    partner_mgmt_id = existing_ref
                elif (partner_mgmt_id or data.audience) != (existing_ref or data.audience):
                    raise PartnerConflict(
                        f"audience '{data.audience}' is bound with partner_mgmt_id "
                        f"'{existing_ref or data.audience}'; a new binding cannot use "
                        f"'{partner_mgmt_id}'"
                    )
            partner = Partner(
                name=data.name if data.name is not None else (
                    siblings[0].name if siblings else None
                ),
                audience=data.audience,
                controller_id=data.controller_id,
                partner_mgmt_id=partner_mgmt_id,
                status=PartnerStatus.active.value,
            )
            session.add(partner)
            await session.commit()
            await session.refresh(partner)
            return partner

    async def get_partner(self, partner_id: str) -> Optional[Partner]:
        async with async_session()() as session:
            return await session.get(Partner, partner_id)

    async def list_partners(
        self,
        controller_id: Optional[str] = None,
        status: Optional[str] = None,
        audience: Optional[str] = None,
    ) -> list:
        """List bindings for the admin console, newest first. Optional filters by
        controller (registry), lifecycle status and audience (all bindings of
        one partner)."""
        async with async_session()() as session:
            query = select(Partner)
            if audience:
                query = query.where(Partner.audience == audience)
            if controller_id:
                query = query.where(Partner.controller_id == controller_id)
            if status:
                query = query.where(Partner.status == status)
            query = query.order_by(Partner.created_at.desc())
            result = await session.execute(query)
            return list(result.scalars().all())

    async def update_partner(self, partner_id: str, data) -> Optional[Partner]:
        """Update one binding's label/status. A ``partner_mgmt_id`` change is
        partner identity, so it is applied to every binding of the audience."""
        async with async_session()() as session:
            partner = await session.get(Partner, partner_id)
            if partner is None:
                return None
            for field in ("name", "status"):
                value = getattr(data, field, None)
                if value is not None:
                    setattr(partner, field, value)
            touched = [partner]
            if getattr(data, "partner_mgmt_id", None) is not None:
                result = await session.execute(
                    select(Partner).where(Partner.audience == partner.audience)
                )
                touched = list(result.scalars().all())
                for binding in touched:
                    binding.partner_mgmt_id = data.partner_mgmt_id
            await session.commit()
            await session.refresh(partner)
        for binding in touched:
            self._invalidate(binding.audience, binding.controller_id)
        return partner

    # ── Admin: policy (versioned) ────────────────────────────────────────────

    async def upsert_policy(self, partner_id: str, data) -> Optional[PartnerPolicy]:
        """Create a new data-share policy version.

        If AWE approval is enabled AND the change *widens* access relative to the
        current active policy (or is the first policy, see
        ``awe_gate_first_policy``), the new version is created ``pending`` and
        does NOT supersede the active one — the caller submits it to AWE and it
        only goes active on approval. A non-widening change (or AWE disabled)
        activates immediately, superseding the prior active version.

        At most one version per binding may be pending: a second widening while
        one awaits approval raises PolicyPending (409). A narrowing may still go
        live meanwhile; the pending version is then applied only if approved
        against the version it was created on (else it ends ``stale``).

        Concurrent saves for one binding are serialised on the binding row; a
        version-number clash that slips through raises PartnerConflict (409).
        """
        try:
            async with async_session()() as session:
                # Lock the binding: serialises concurrent saves and decisions.
                partner = await session.get(Partner, partner_id, with_for_update=True)
                if partner is None:
                    return None

                result = await session.execute(
                    select(PartnerPolicy)
                    .where(PartnerPolicy.partner_id == partner_id)
                    .order_by(PartnerPolicy.version.desc())
                )
                existing = list(result.scalars().all())
                active = next(
                    (p for p in existing if p.status == PolicyStatus.active.value), None
                )
                pending = next(
                    (p for p in existing if p.status == PolicyStatus.pending.value), None
                )
                next_version = (existing[0].version + 1) if existing else 1

                gated = _config.awe_enabled and self._needs_approval(data, active)
                if gated and pending is not None:
                    raise PolicyPending(pending.version)

                if gated:
                    status = PolicyStatus.pending.value
                    effective_from = None
                    # Do NOT supersede the active policy — it stays in force until
                    # this pending version is approved.
                else:
                    status = PolicyStatus.active.value
                    effective_from = datetime.now(timezone.utc)
                    if active is not None:
                        active.status = PolicyStatus.superseded.value

                policy = PartnerPolicy(
                    partner_id=partner_id,
                    version=next_version,
                    status=status,
                    allowed_data_scopes=data.allowed_data_scopes,
                    allowed_purposes=data.allowed_purposes,
                    allowed_subject_id_types=data.allowed_subject_id_types,
                    allowed_signing_algs=data.allowed_signing_algs,
                    max_validity_duration=data.max_validity_duration,
                    fetch_type=data.fetch_type,
                    max_fetch_frequency=data.max_fetch_frequency,
                    data_life=data.data_life,
                    effective_from=effective_from,
                    base_version=active.version if active is not None else 0,
                )
                session.add(policy)
                await session.commit()
                await session.refresh(policy)
                audience, controller_id = partner.audience, partner.controller_id
        except IntegrityError as exc:
            # A concurrent save took the same version number, or a second pending
            # version raced past the check (one-pending partial unique index).
            raise PartnerConflict(
                "another policy change for this binding was saved at the same "
                "time; reload and retry"
            ) from exc

        if not gated:
            self._invalidate(audience, controller_id)  # active policy changed
        return policy

    async def resubmit_policy(
        self, partner_id: str, version: int
    ) -> Optional[PartnerPolicy]:
        """Copy a ``failed`` / ``stale`` / ``rejected`` version into a NEW version
        and save it like any other change: it is re-evaluated against the
        current active policy (pending again if it still widens, else active).
        Returns None if the binding or version does not exist; raises
        PolicyNotResubmittable if the version is in another state."""
        source = await self.get_policy(partner_id, version)
        if source is None:
            return None
        if source.status not in _RESUBMITTABLE:
            raise PolicyNotResubmittable(source.version, source.status)
        return await self.upsert_policy(partner_id, _PolicyCopy(source))

    async def set_policy_awe_request_id(self, policy_id: str, awe_request_id: str) -> None:
        async with async_session()() as session:
            policy = await session.get(PartnerPolicy, policy_id)
            if policy is not None and policy.awe_request_id is None:
                policy.awe_request_id = awe_request_id
                await session.commit()

    async def mark_policy_submit_failed(self, policy_id: str, reason: str) -> None:
        """The AWE submission for a pending version failed: end it as ``failed``
        so it does not sit pending forever (nobody can approve it). Leaves a
        version alone if a decision already arrived for it."""
        async with async_session()() as session:
            policy = await session.get(PartnerPolicy, policy_id)
            if policy is not None and policy.status == PolicyStatus.pending.value:
                policy.status = PolicyStatus.failed.value
                policy.status_reason = f"AWE submission failed: {reason}"[:2000]
                await session.commit()

    async def apply_policy_decision(
        self, awe_request_id: str, artifact_id: str, approved: bool, reason: str = ""
    ) -> Optional[str]:
        """Apply a terminal AWE decision to a pending policy version.

        On approve: if the version that was active when this one was created is
        still the active one, activate it and supersede that version; otherwise
        (the active policy changed meanwhile, e.g. a narrowing went live) do not
        apply it — mark it ``stale`` so it can be resubmitted. On reject: mark it
        ``rejected``. Matches by AWE request id, else artifact id (== policy id).

        Returns the resulting status (``active`` / ``stale`` / ``rejected``, or
        the current status for a re-delivered decision), or None if no policy
        correlates."""
        async with async_session()() as session:
            policy = None
            if awe_request_id:
                res = await session.execute(
                    select(PartnerPolicy).where(
                        PartnerPolicy.awe_request_id == awe_request_id
                    )
                )
                policy = res.scalars().first()
            if policy is None and artifact_id:
                policy = await session.get(PartnerPolicy, artifact_id)
            if policy is None:
                return None

            # Lock the binding (same lock as upsert_policy), then re-read the
            # version under it.
            partner = await session.get(Partner, policy.partner_id, with_for_update=True)
            await session.refresh(policy)
            # Idempotent: a re-delivered webhook for an already-decided version.
            if policy.status != PolicyStatus.pending.value:
                return policy.status

            if policy.awe_request_id is None and awe_request_id:
                policy.awe_request_id = awe_request_id

            if approved:
                res = await session.execute(
                    select(PartnerPolicy).where(
                        PartnerPolicy.partner_id == policy.partner_id,
                        PartnerPolicy.status == PolicyStatus.active.value,
                    )
                )
                actives = list(res.scalars().all())
                current = max((p.version for p in actives), default=0)
                if policy.base_version is not None and policy.base_version != current:
                    policy.status = PolicyStatus.stale.value
                    policy.status_reason = (
                        f"Approved, but the active policy changed from "
                        f"v{policy.base_version or 'none'} to v{current or 'none'} "
                        f"after this version was submitted; not applied. Resubmit "
                        f"to re-evaluate it against the current policy."
                    )
                else:
                    for old in actives:
                        old.status = PolicyStatus.superseded.value
                    policy.status = PolicyStatus.active.value
                    policy.effective_from = datetime.now(timezone.utc)
            else:
                policy.status = PolicyStatus.rejected.value
                policy.status_reason = reason or "Rejected in AWE"

            key = (partner.audience, partner.controller_id) if partner else None
            result = policy.status
            await session.commit()
        if key and result == PolicyStatus.active.value:
            self._invalidate(*key)
        return result

    async def list_policies(self, partner_id: str) -> Optional[list]:
        """All policy versions for a binding, newest first. None if no binding."""
        async with async_session()() as session:
            partner = await session.get(Partner, partner_id)
            if partner is None:
                return None
            result = await session.execute(
                select(PartnerPolicy)
                .where(PartnerPolicy.partner_id == partner_id)
                .order_by(PartnerPolicy.version.desc())
            )
            return list(result.scalars().all())

    async def known_values(self) -> dict:
        """Values already in use, offered as form suggestions: controller ids of
        bindings and the scopes / purposes / subject id types of policies. These
        are open sets (registry-defined), so this is a hint, not a constraint."""
        async with async_session()() as session:
            controllers = await session.execute(
                select(Partner.controller_id).distinct().order_by(Partner.controller_id)
            )
            out = {"controller_ids": [c for (c,) in controllers.all() if c]}
            for key, column in (
                ("data_scopes", PartnerPolicy.allowed_data_scopes),
                ("purposes", PartnerPolicy.allowed_purposes),
                ("subject_id_types", PartnerPolicy.allowed_subject_id_types),
            ):
                value = func.jsonb_array_elements_text(column).label("v")
                rows = await session.execute(select(value).distinct())
                out[key] = sorted({v for (v,) in rows.all() if v})
        if _config.subject_default_id_type not in out["subject_id_types"]:
            out["subject_id_types"] = sorted(
                out["subject_id_types"] + [_config.subject_default_id_type]
            )
        return out

    async def get_policy(
        self, partner_id: str, version: Optional[int] = None
    ) -> Optional[PartnerPolicy]:
        async with async_session()() as session:
            query = select(PartnerPolicy).where(PartnerPolicy.partner_id == partner_id)
            if version is not None:
                query = query.where(PartnerPolicy.version == version)
            else:
                query = query.where(
                    PartnerPolicy.status == PolicyStatus.active.value
                )
            result = await session.execute(query)
            return result.scalars().first()

    # ── Widening detection (drives whether AWE approval is required) ──────────

    @classmethod
    def _needs_approval(cls, data, active: Optional[PartnerPolicy]) -> bool:
        """Whether this change must go through AWE (AWE being on). The first
        policy of a binding is a grant from nothing — gated unless
        ``awe_gate_first_policy`` is off."""
        if active is None:
            return _config.awe_gate_first_policy
        return cls._is_widening(data, active)

    @staticmethod
    def _is_widening(data, active: Optional[PartnerPolicy]) -> bool:
        """True if `data` grants anything the current active policy did not — a
        larger allowed set, a longer validity/data-life, periodic instead of
        one-shot fetching, or more frequent fetching. The first policy (no
        active prior) counts as a widening (a grant from nothing)."""
        if active is None:
            return True
        # An empty data-scope list allows nothing.
        new_scopes = set(getattr(data, "allowed_data_scopes", None) or [])
        if new_scopes - set(getattr(active, "allowed_data_scopes", None) or []):
            return True
        # For these, an empty list means "any" (see policy_service and
        # verification_service): clearing a non-empty list widens to everything.
        for field in ("allowed_purposes", "allowed_subject_id_types", "allowed_signing_algs"):
            new_set = set(getattr(data, field, None) or [])
            old_set = set(getattr(active, field, None) or [])
            if not old_set:
                continue  # already "any": nothing can widen it
            if not new_set or new_set - old_set:
                return True
        if PartnerService._duration_loosened(
            data.max_validity_duration, active.max_validity_duration
        ):
            return True
        if PartnerService._duration_loosened(data.data_life, active.data_life):
            return True
        # Periodic fetching allows repeated pulls; one-shot allows one.
        if _fetch_rank(getattr(data, "fetch_type", None)) > _fetch_rank(
            getattr(active, "fetch_type", None)
        ):
            return True
        # max_fetch_frequency is the minimum interval between fetches: a SHORTER
        # interval (or removing it) allows more fetches.
        if PartnerService._interval_shortened(
            getattr(data, "max_fetch_frequency", None),
            getattr(active, "max_fetch_frequency", None),
        ):
            return True
        return False

    @staticmethod
    def _duration_loosened(new: Optional[str], old: Optional[str]) -> bool:
        """True if ISO-8601 duration `new` permits a LONGER window than `old`.
        None means "no cap" (widest). On a parse error, err toward requiring
        approval (return True)."""
        if new == old:
            return False
        if new is None:  # removed the cap → wider
            return old is not None
        if old is None:  # added a cap → narrower
            return False
        try:
            return iso_duration_to_timedelta(new) > iso_duration_to_timedelta(old)
        except Exception:
            return True

    @staticmethod
    def _interval_shortened(new: Optional[str], old: Optional[str]) -> bool:
        """True if minimum fetch interval `new` allows MORE frequent fetching than
        `old`. None means "no limit" (most frequent). On a parse error, err
        toward requiring approval (return True)."""
        if new == old:
            return False
        if new is None:  # removed the limit → wider
            return old is not None
        if old is None:  # added a limit → narrower
            return False
        try:
            return iso_duration_to_timedelta(new) < iso_duration_to_timedelta(old)
        except Exception:
            return True

    # ── Hot path: cached verification material ───────────────────────────────

    async def get_verification_material(
        self, audience: str, controller_id: str
    ) -> Optional[dict]:
        """Return the active binding of ``audience`` to ``controller_id`` and its
        active policy — cached per pod for the validation hot path. The policy is
        per (audience, controller): a partner bound to two controllers has two
        independent policies.

        Partner public keys are NOT included here: they are owned by the Partner
        Management service and fetched separately (and cached with their own
        discipline) by the shared fastapi-common CryptoHelper (partner-mgmt
        backend) during consent-JWS verification, keyed by the partner's
        ``partner_mgmt_id``. This method only resolves the onboarded party +
        policy. Returns None if the partner has no active binding to the
        controller (unknown, suspended, or not bound to that controller).

        Shape: {"partner": Partner, "policy": PartnerPolicy | None}
        """
        key = _cache_key(audience, controller_id)
        if _config.partner_cache_enabled:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        async with async_session()() as session:
            result = await session.execute(
                select(Partner).where(
                    Partner.audience == audience,
                    Partner.controller_id == controller_id,
                    Partner.status == PartnerStatus.active.value,
                )
            )
            partner = result.scalars().first()
            if partner is None:
                return None

            policy_result = await session.execute(
                select(PartnerPolicy).where(
                    PartnerPolicy.partner_id == partner.id,
                    PartnerPolicy.status == PolicyStatus.active.value,
                )
            )
            policy = policy_result.scalars().first()

        material = {"partner": partner, "policy": policy}
        if _config.partner_cache_enabled:
            self._cache.set(key, material)
        return material

    async def get_binding(self, audience: str, controller_id: str) -> Optional[Partner]:
        """The binding of ``audience`` to ``controller_id`` (any status)."""
        async with async_session()() as session:
            result = await session.execute(
                select(Partner).where(
                    Partner.audience == audience, Partner.controller_id == controller_id
                )
            )
            return result.scalars().first()

    def _invalidate(self, audience: Optional[str], controller_id: Optional[str]) -> None:
        if audience and controller_id:
            self._cache.invalidate(_cache_key(audience, controller_id))

    async def count_partners(self) -> int:
        async with async_session()() as session:
            result = await session.execute(select(func.count(Partner.id)))
            return int(result.scalar() or 0)
