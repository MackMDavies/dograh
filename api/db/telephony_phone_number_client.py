"""Database access for telephony phone numbers.

Phone numbers are first-class entities (PSTN, SIP URI, or SIP extension)
owned by a telephony configuration. They power both outbound caller-ID
selection and inbound call routing.
"""

import json

from loguru import logger

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from sqlalchemy.orm import aliased

from api.db.base_client import BaseDBClient
from api.db.models import (
    TelephonyConfigurationModel,
    TelephonyPhoneNumberModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.utils.telephony_address import normalize_telephony_address


def _decrypted_credentials(config) -> dict:
    """Best-effort decrypt of a telephony config's credentials.

    Stored Fernet-encrypted, so they can never be filtered in SQL. Returns {} on
    any failure — a config we cannot read must not match, but it also must not
    break routing for every other config.
    """
    raw = getattr(config, "credentials", None)
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        from api.services.crypto import decrypt_secret

        decrypted = decrypt_secret(raw)
        return json.loads(decrypted) if isinstance(decrypted, str) else (decrypted or {})
    except Exception as exc:  # noqa: BLE001 - never break inbound routing
        logger.warning(f"Could not decrypt telephony credentials for config {getattr(config, 'id', '?')}: {exc}")
        return {}



class TelephonyPhoneNumberClient(BaseDBClient):
    async def list_phone_numbers_for_config(
        self, telephony_configuration_id: int
    ) -> List[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel)
                .where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id
                )
                .order_by(TelephonyPhoneNumberModel.created_at)
            )
            return list(result.scalars().all())

    async def list_phone_numbers_with_workflow_name_for_config(
        self, telephony_configuration_id: int
    ) -> List[Tuple[TelephonyPhoneNumberModel, Optional[str], Optional[str]]]:
        """Same as :meth:`list_phone_numbers_for_config` but also returns the
        inbound and outbound agents' display names (or None) for each row.

        Two aliased LEFT JOINs against the same table — without aliases the
        second join would collapse onto the first and both names would resolve
        to the inbound agent.
        """
        inbound_wf = aliased(WorkflowModel)
        outbound_wf = aliased(WorkflowModel)
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel, inbound_wf.name, outbound_wf.name)
                .join(
                    inbound_wf,
                    inbound_wf.id == TelephonyPhoneNumberModel.inbound_workflow_id,
                    isouter=True,
                )
                .join(
                    outbound_wf,
                    outbound_wf.id == TelephonyPhoneNumberModel.outbound_workflow_id,
                    isouter=True,
                )
                .where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id
                )
                .order_by(TelephonyPhoneNumberModel.created_at)
            )
            return [(row, inb, outb) for row, inb, outb in result.all()]

    async def list_active_normalized_addresses_for_config(
        self, telephony_configuration_id: int
    ) -> List[str]:
        """Active phone numbers as canonical address strings (E.164 for PSTN,
        normalized SIP otherwise) — the shape providers want in their
        ``from_numbers`` list for caller-ID and rate-limit pool keys.

        Excludes numbers that are inbound-only. A number reserved to answer a
        published line must never appear as an outbound caller ID, or bulk
        dialling accrues carrier spam flags against the number customers are
        told to ring.

        The rule is deliberately "not inbound-only" rather than "outbound
        assigned": rows predating the outbound column, and any row with neither
        direction set, keep dialling exactly as before. Migration
        c3d4e5f6a7b1 backfills outbound_workflow_id = inbound_workflow_id, so
        on deploy this excludes nothing — a number only leaves the pool once
        someone deliberately clears its outbound assignment.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel.address_normalized)
                .where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id,
                    TelephonyPhoneNumberModel.is_active.is_(True),
                    ~(
                        TelephonyPhoneNumberModel.inbound_workflow_id.isnot(None)
                        & TelephonyPhoneNumberModel.outbound_workflow_id.is_(None)
                    ),
                )
                .order_by(TelephonyPhoneNumberModel.created_at)
            )
            return [row[0] for row in result.all()]

    async def get_phone_number_usage(
        self, address_normalized: str, organization_id: int
    ) -> Dict[str, Any]:
        """Lifetime usage for one number, for the expandable row on the numbers page.

        Matched against ``workflow_runs.initial_context`` rather than a foreign
        key, because runs record the number they actually used rather than
        pointing at the row: ``caller_number`` is ours on outbound (see the
        dispatcher, which stamps it at dial time) and ``called_number`` is ours
        on inbound. A number can therefore accrue history and later be
        reassigned or released without the history following it.

        ``usage_info`` durations are stored per run as JSON; summed in Python
        because the key has varied across the schema's history and a SQL cast
        over a missing or non-numeric key raises rather than skipping the row.
        """
        ctx = WorkflowRunModel.initial_context
        # as_string(), not .astext — the column is generic JSON, and .astext is
        # a JSONB-only accessor that fails at query-build time.
        mine = or_(
            ctx["caller_number"].as_string() == address_normalized,
            ctx["called_number"].as_string() == address_normalized,
        )

        async with self.async_session() as session:
            result = await session.execute(
                select(
                    func.count(WorkflowRunModel.id),
                    func.count(func.distinct(WorkflowRunModel.campaign_id)),
                    func.max(WorkflowRunModel.created_at),
                    func.min(WorkflowRunModel.created_at),
                )
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(WorkflowModel.organization_id == organization_id, mine)
            )
            total_calls, campaign_count, last_used, first_used = result.one()

            # Direction split. call_type is an enum column, so this is cheap.
            dir_rows = await session.execute(
                select(WorkflowRunModel.call_type, func.count(WorkflowRunModel.id))
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(WorkflowModel.organization_id == organization_id, mine)
                .group_by(WorkflowRunModel.call_type)
            )
            by_direction = {str(k): int(v) for k, v in dir_rows.all()}

            usage_rows = await session.execute(
                select(WorkflowRunModel.usage_info)
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(WorkflowModel.organization_id == organization_id, mine)
            )

        total_seconds = 0.0
        for (usage,) in usage_rows.all():
            if not isinstance(usage, dict):
                continue
            for key in ("duration_seconds", "call_duration_seconds", "duration"):
                raw = usage.get(key)
                if raw is None:
                    continue
                try:
                    total_seconds += float(raw)
                except (TypeError, ValueError):
                    # A malformed row must not sink the whole panel.
                    pass
                break

        return {
            "total_calls": int(total_calls or 0),
            "campaign_count": int(campaign_count or 0),
            "total_minutes": round(total_seconds / 60.0, 1),
            "last_used_at": last_used,
            "first_used_at": first_used,
            "by_direction": by_direction,
        }

    async def list_outbound_numbers_for_workflow(
        self, workflow_id: int, organization_id: int
    ) -> List[TelephonyPhoneNumberModel]:
        """Active numbers this agent is allowed to dial out as.

        Backs the campaign wizard's caller step: a campaign may only send from
        a number explicitly assigned to its agent for outbound.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel)
                .where(
                    TelephonyPhoneNumberModel.outbound_workflow_id == workflow_id,
                    TelephonyPhoneNumberModel.organization_id == organization_id,
                    TelephonyPhoneNumberModel.is_active.is_(True),
                )
                .order_by(TelephonyPhoneNumberModel.created_at)
            )
            return list(result.scalars().all())

    async def list_phone_numbers_for_workflows(
        self, workflow_ids: List[int]
    ) -> List[TelephonyPhoneNumberModel]:
        """Return all phone numbers whose inbound_workflow_id is in workflow_ids."""
        if not workflow_ids:
            return []
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel).where(
                    TelephonyPhoneNumberModel.inbound_workflow_id.in_(workflow_ids)
                )
            )
            return list(result.scalars().all())

    async def get_phone_number(
        self, phone_number_id: int, organization_id: Optional[int] = None
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            if organization_id is None:
                return await session.get(TelephonyPhoneNumberModel, phone_number_id)
            result = await session.execute(
                select(TelephonyPhoneNumberModel).where(
                    TelephonyPhoneNumberModel.id == phone_number_id,
                    TelephonyPhoneNumberModel.organization_id == organization_id,
                )
            )
            return result.scalars().first()

    async def get_phone_number_for_config(
        self, phone_number_id: int, telephony_configuration_id: int
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel).where(
                    TelephonyPhoneNumberModel.id == phone_number_id,
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id,
                )
            )
            return result.scalars().first()

    async def find_active_phone_number_for_inbound(
        self,
        organization_id: int,
        address: str,
        provider: str,
        country_hint: Optional[str] = None,
    ) -> Optional[TelephonyPhoneNumberModel]:
        """Inbound routing primary lookup: normalize the called address and find
        the matching active row whose config is for the detected provider."""
        normalized = normalize_telephony_address(address, country_hint=country_hint)

        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel)
                .join(
                    TelephonyConfigurationModel,
                    TelephonyConfigurationModel.id
                    == TelephonyPhoneNumberModel.telephony_configuration_id,
                )
                .where(
                    TelephonyPhoneNumberModel.organization_id == organization_id,
                    TelephonyPhoneNumberModel.address_normalized
                    == normalized.canonical,
                    TelephonyPhoneNumberModel.is_active.is_(True),
                    TelephonyConfigurationModel.provider == provider,
                )
            )
            return result.scalars().first()

    async def find_inbound_route_by_account(
        self,
        provider: str,
        account_id_field: str,
        account_id: str,
        to_number: str,
        country_hint: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> Optional[Tuple[TelephonyConfigurationModel, TelephonyPhoneNumberModel]]:
        """Combined primary-path lookup for inbound dispatch.

        One SQL roundtrip that joins ``telephony_configurations`` and
        ``telephony_phone_numbers`` and matches all of:
        provider, ``credentials[account_id_field] == account_id``,
        ``phone.address_normalized == canonical(to_number)``, and
        ``phone.is_active``. Replaces the previous pattern of resolving the
        config and the phone number in two separate queries with a Python-side
        loop over candidate configs.

        Returns ``(config, phone_number)`` or None when the primary path
        misses (e.g. legacy non-E.164 stored addresses); the caller should
        fall back to the fuzzy ``numbers_match`` path in that case.
        """
        if not (provider and account_id_field and account_id and to_number):
            return None

        normalized = normalize_telephony_address(to_number, country_hint=country_hint)

        async with self.async_session() as session:
            stmt = (
                select(TelephonyConfigurationModel, TelephonyPhoneNumberModel)
                .join(
                    TelephonyPhoneNumberModel,
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == TelephonyConfigurationModel.id,
                )
                .where(
                    TelephonyConfigurationModel.provider == provider,
                    TelephonyPhoneNumberModel.address_normalized
                    == normalized.canonical,
                    TelephonyPhoneNumberModel.is_active.is_(True),
                )
            )
            if organization_id is not None:
                stmt = stmt.where(
                    TelephonyConfigurationModel.organization_id == organization_id
                )
            result = await session.execute(stmt)
            rows = result.all()
            if not rows:
                return None

            # The account_id match CANNOT be done in SQL. `credentials` is a
            # Fernet-encrypted string (all four rows on prod begin "g" and are
            # 228 chars), not queryable JSON, so
            # `credentials ->> 'account_sid' = :account_id` raised
            # `operator does not exist: text ->> character varying` and every
            # inbound call died there and returned a generic hangup — inbound
            # has never once worked on this deployment. Decrypt and compare in
            # Python, which is what this lookup did before it was "optimised"
            # into a single query (see the docstring's own history note).
            for config, phone in rows:
                if not account_id or not account_id_field:
                    return config, phone
                creds = _decrypted_credentials(config)
                if creds.get(account_id_field) == account_id:
                    return config, phone

            # A number we own, but no config whose account matches. Returning
            # None lets the caller fall back rather than pretending we matched.
            return None

    async def find_inbound_routing_conflict(
        self,
        provider: str,
        account_id_field: str,
        account_id: str,
        address: str,
        country_hint: Optional[str] = None,
    ) -> Optional[Tuple[TelephonyConfigurationModel, TelephonyPhoneNumberModel]]:
        """Inbound dispatch keys on (provider, credentials[account_id_field],
        address_normalized) — see ``find_inbound_route_by_account``. That tuple
        must be globally unique or two orgs would race for the same call.

        Returns the conflicting (config, phone_number) — possibly in another
        org — when inserting a row with this combination would break that
        invariant, or None when the row is safe to insert. Returns None for
        providers that don't carry an account_id (e.g. ARI), which use a
        different inbound path.
        """
        if not (provider and account_id_field and account_id):
            return None

        normalized = normalize_telephony_address(address, country_hint=country_hint)

        async with self.async_session() as session:
            stmt = (
                select(TelephonyConfigurationModel, TelephonyPhoneNumberModel)
                .join(
                    TelephonyPhoneNumberModel,
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == TelephonyConfigurationModel.id,
                )
                .where(
                    TelephonyConfigurationModel.provider == provider,
                    TelephonyPhoneNumberModel.address_normalized
                    == normalized.canonical,
                )
            )
            result = await session.execute(stmt)
            for config, phone_number in result.all():
                if _decrypted_credentials(config).get(account_id_field) == account_id:
                    return (config, phone_number)
            return None

    async def create_phone_number(
        self,
        organization_id: int,
        telephony_configuration_id: int,
        address: str,
        country_code: Optional[str] = None,
        label: Optional[str] = None,
        inbound_workflow_id: Optional[int] = None,
        outbound_workflow_id: Optional[int] = None,
        is_active: bool = True,
        is_default_caller_id: bool = False,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> TelephonyPhoneNumberModel:
        normalized = normalize_telephony_address(address, country_hint=country_code)

        async with self.async_session() as session:
            if is_default_caller_id:
                await self._clear_default_caller_id(session, telephony_configuration_id)

            row = TelephonyPhoneNumberModel(
                organization_id=organization_id,
                telephony_configuration_id=telephony_configuration_id,
                address=address,
                address_normalized=normalized.canonical,
                address_type=normalized.address_type,
                country_code=country_code or normalized.country_code,
                label=label,
                inbound_workflow_id=inbound_workflow_id,
                outbound_workflow_id=outbound_workflow_id,
                is_active=is_active,
                is_default_caller_id=is_default_caller_id,
                extra_metadata=extra_metadata or {},
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                raise e
            await session.refresh(row)
            return row

    async def update_phone_number(
        self,
        phone_number_id: int,
        telephony_configuration_id: int,
        label: Optional[str] = None,
        inbound_workflow_id: Optional[int] = None,
        outbound_workflow_id: Optional[int] = None,
        is_active: Optional[bool] = None,
        country_code: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        clear_inbound_workflow: bool = False,
        clear_outbound_workflow: bool = False,
    ) -> Optional[TelephonyPhoneNumberModel]:
        """Partial update. ``address`` is intentionally immutable — create a new
        row instead. Set ``clear_inbound_workflow`` / ``clear_outbound_workflow``
        to null out the respective FK — the directions are independent, so
        clearing one never touches the other."""
        async with self.async_session() as session:
            row = await session.get(TelephonyPhoneNumberModel, phone_number_id)
            if not row or row.telephony_configuration_id != telephony_configuration_id:
                return None

            if label is not None:
                row.label = label
            if inbound_workflow_id is not None:
                row.inbound_workflow_id = inbound_workflow_id
            elif clear_inbound_workflow:
                row.inbound_workflow_id = None
            if outbound_workflow_id is not None:
                row.outbound_workflow_id = outbound_workflow_id
            elif clear_outbound_workflow:
                row.outbound_workflow_id = None
            if is_active is not None:
                row.is_active = is_active
            if country_code is not None:
                row.country_code = country_code
            if extra_metadata is not None:
                row.extra_metadata = extra_metadata

            await session.commit()
            await session.refresh(row)
            return row

    async def set_default_caller_id(
        self, phone_number_id: int, telephony_configuration_id: int
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            row = await session.get(TelephonyPhoneNumberModel, phone_number_id)
            if not row or row.telephony_configuration_id != telephony_configuration_id:
                return None
            await self._clear_default_caller_id(session, telephony_configuration_id)
            row.is_default_caller_id = True
            await session.commit()
            await session.refresh(row)
            return row

    async def get_default_caller_id(
        self, telephony_configuration_id: int
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel).where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id,
                    TelephonyPhoneNumberModel.is_default_caller_id.is_(True),
                )
            )
            return result.scalars().first()

    async def delete_phone_number(
        self,
        phone_number_id: int,
        telephony_configuration_id: Optional[int] = None,
        organization_id: Optional[int] = None,
    ) -> bool:
        async with self.async_session() as session:
            row = await session.get(TelephonyPhoneNumberModel, phone_number_id)
            if row is None:
                return False
            if telephony_configuration_id is not None and row.telephony_configuration_id != telephony_configuration_id:
                return False
            if organization_id is not None and row.organization_id != organization_id:
                return False
            await session.delete(row)
            await session.commit()
            return True

    @staticmethod
    async def _clear_default_caller_id(
        session, telephony_configuration_id: int
    ) -> None:
        await session.execute(
            update(TelephonyPhoneNumberModel)
            .where(
                TelephonyPhoneNumberModel.telephony_configuration_id
                == telephony_configuration_id,
                TelephonyPhoneNumberModel.is_default_caller_id.is_(True),
            )
            .values(is_default_caller_id=False)
        )
