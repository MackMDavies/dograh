import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import JSON, Text, cast, func, literal, or_, text, update
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload, selectinload

from api.db.base_client import BaseDBClient
from api.db.filters import apply_workflow_run_filters, get_workflow_run_order_clause
from api.db.models import (
    OrganizationModel,
    UserModel,
    WorkflowDefinitionModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.db.workflow_run_visibility import customer_visible_workflow_run
from api.enums import CallType, StorageBackend
from api.schemas.workflow import WorkflowRunResponseSchema
from api.services.workflow.variable_resolution import sanitize_context_variables


class WorkflowRunClient(BaseDBClient):
    async def save_agent_test_voice_result(
        self, run_id, workflow_id, organization_id, session_id, result, *, tool_trace=None
    ):
        """Atomically add immutable private evidence without replacing annotations."""
        async with self.async_session() as session:
            annotations = cast(WorkflowRunModel.annotations, JSONB)
            saved = annotations.op("->")("agent_test_voice_result")
            updated_annotations = func.jsonb_set(
                annotations, array(["agent_test_voice_result"], type_=Text), literal(result, JSONB), True,
            )
            trace_guards = []
            if tool_trace is not None:
                saved_trace = annotations.op("->")("agent_test_voice_tool_trace")
                trace_guards.append(or_(saved_trace.is_(None), saved_trace == literal(tool_trace, JSONB)))
                updated_annotations = func.jsonb_set(
                    updated_annotations, array(["agent_test_voice_tool_trace"], type_=Text),
                    literal(tool_trace, JSONB), True,
                )
            written = await session.execute(
                update(WorkflowRunModel)
                .where(
                    WorkflowRunModel.id == run_id,
                    WorkflowRunModel.workflow_id == workflow_id,
                    WorkflowRunModel.workflow_id.in_(
                        select(WorkflowModel.id).where(
                            WorkflowModel.organization_id == organization_id
                        )
                    ),
                    annotations.contains({
                        "sysevo_test_sandbox": {
                            "enabled": True,
                            "voice_session_id": session_id,
                            "organization_id": organization_id,
                        }
                    }),
                    or_(saved.is_(None), saved == literal(result, JSONB)),
                    *trace_guards,
                )
                .values(annotations=cast(updated_annotations, JSON))
                .returning(WorkflowRunModel.id)
            )
            exists = written.scalar_one_or_none() is not None
            await session.commit()
            return exists

    async def create_workflow_run(
        self,
        name: str,
        workflow_id: int,
        mode: str,
        user_id: int,
        call_type: CallType = CallType.OUTBOUND,
        initial_context: dict = None,
        gathered_context: dict = None,
        logs: dict = None,
        campaign_id: int = None,
        queued_run_id: int = None,
        use_draft: bool = False,
        definition_version_number: int | None = None,
        definition_version_status: str = "published",
        organization_id: int | None = None,
        bypass_user_check: bool = False,
        api_key_id: int | None = None,
        definition_id: int | None = None,
        idempotency_key: str | None = None,
        annotations: dict | None = None,
    ) -> WorkflowRunModel:
        async with self.async_session() as session:
            workflow_query = select(WorkflowModel).options(joinedload(WorkflowModel.user)).where(
                WorkflowModel.id == workflow_id
            )
            if not bypass_user_check:
                workflow_query = workflow_query.where(WorkflowModel.user_id == user_id)
            if organization_id is not None:
                workflow_query = workflow_query.where(
                    WorkflowModel.organization_id == organization_id
                )

            workflow = await session.execute(workflow_query)
            workflow = workflow.scalars().first()
            if not workflow:
                raise ValueError(f"Workflow with ID {workflow_id} not found")

            if idempotency_key is not None:
                if not organization_id or not idempotency_key or len(idempotency_key) > 200:
                    raise ValueError("Idempotent workflow creation requires an organization and bounded key")
                # Cross-worker serialization lives in Postgres. Holding a
                # Python lock would not protect a retried webhook on another worker.
                await session.execute(text("select pg_advisory_xact_lock(hashtextextended(:key, 610184))"), {
                    "key": f"workflow-run:{organization_id}:{workflow_id}:{idempotency_key}",
                })
                existing = (await session.execute(select(WorkflowRunModel).where(
                    WorkflowRunModel.workflow_id == workflow_id,
                    WorkflowRunModel.initial_context.op("->>")("execution_idempotency_key") == idempotency_key,
                ))).scalars().first()
                if existing:
                    return existing
                initial_context = {**(initial_context or {}), "execution_idempotency_key": idempotency_key}

            # Resolve which definition to bind to this run. Internal sandbox
            # rehearsals may pin an exact published or draft version; validate
            # that the definition belongs to this workflow before binding it.
            target_def = None

            if definition_id is not None and definition_version_number is not None:
                raise ValueError("Specify a definition ID or version number, not both")

            if definition_id is not None:
                if use_draft:
                    raise ValueError("An exact workflow definition cannot be combined with draft selection")
                definition_result = await session.execute(
                    select(WorkflowDefinitionModel).where(
                        WorkflowDefinitionModel.id == definition_id,
                        WorkflowDefinitionModel.workflow_id == workflow.id,
                    )
                )
                target_def = definition_result.scalars().first()
                if target_def is None:
                    raise ValueError(
                        f"Workflow definition {definition_id} not found for workflow {workflow_id}"
                    )

            elif definition_version_number is not None:
                if use_draft:
                    raise ValueError("An exact workflow version cannot be combined with draft selection")
                if definition_version_status not in {"published", "draft"}:
                    raise ValueError("An exact workflow version must be published or a candidate draft")
                version_result = await session.execute(
                    select(WorkflowDefinitionModel).where(
                        WorkflowDefinitionModel.workflow_id == workflow.id,
                        WorkflowDefinitionModel.version_number == definition_version_number,
                        WorkflowDefinitionModel.status == definition_version_status,
                    )
                )
                target_def = version_result.scalars().first()
                if target_def is None:
                    raise ValueError(
                        f"{definition_version_status.title()} workflow version {definition_version_number} is no longer available"
                    )

            if target_def is None and use_draft:
                # For test calls: prefer draft if it exists, fall back to published
                draft_result = await session.execute(
                    select(WorkflowDefinitionModel).where(
                        WorkflowDefinitionModel.workflow_id == workflow.id,
                        WorkflowDefinitionModel.status == "draft",
                    )
                )
                target_def = draft_result.scalars().first()

            if target_def is None:
                # Use the published version via released_definition_id (preferred)
                # or fall back to is_current for backward compatibility
                if workflow.released_definition_id:
                    target_def = await session.get(
                        WorkflowDefinitionModel, workflow.released_definition_id
                    )
                else:
                    pub_result = await session.execute(
                        select(WorkflowDefinitionModel).where(
                            WorkflowDefinitionModel.workflow_id == workflow.id,
                            WorkflowDefinitionModel.is_current == True,
                        )
                    )
                    target_def = pub_result.scalars().first()

            # Get the current storage backend based on ENABLE_AWS_S3 flag
            current_backend = StorageBackend.get_current_backend()

            # Use initial_context from the version if available, else from workflow.
            # Sanitised so canonical descriptor objects ({default,source,...})
            # are collapsed to their default string — storing the raw config here
            # would put a dict in front of the renderer, which JSON-dumps it and
            # the agent then reads it aloud on the call.
            default_context = sanitize_context_variables(
                target_def.template_context_variables
                if target_def and target_def.template_context_variables
                else workflow.template_context_variables
            )

            new_run = WorkflowRunModel(
                name=name,
                workflow=workflow,
                mode=mode,
                definition_id=target_def.id if target_def else None,
                initial_context=initial_context or default_context,
                gathered_context=gathered_context or {},
                logs=logs or {},
                campaign_id=campaign_id,
                queued_run_id=queued_run_id,
                storage_backend=current_backend.value,
                call_type=call_type.value,
                api_key_id=api_key_id,
                annotations=annotations or {},
            )
            session.add(new_run)
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(new_run)
        return new_run

    async def claim_dialer_agent_media(self, run_id: int, organization_id: int, call_id: str) -> bool:
        """A call-scoped, authenticated media stream may start a run only once."""
        async with self.async_session() as session:
            result = await session.execute(update(WorkflowRunModel).where(
                WorkflowRunModel.id == run_id,
                WorkflowRunModel.workflow_id.in_(select(WorkflowModel.id).where(WorkflowModel.organization_id == organization_id)),
                WorkflowRunModel.initial_context.op("->>")("dialer_original_call_id") == call_id,
                WorkflowRunModel.state == "initialized",
                WorkflowRunModel.is_completed.is_not(True),
            ).values(
                state="running",
                gathered_context=cast(func.jsonb_set(
                    func.coalesce(
                        func.nullif(cast(WorkflowRunModel.gathered_context, JSONB), literal(None, JSONB)),
                        literal({}, JSONB),
                    ),
                    array(["dialer_media_authenticated"], type_=Text),
                    literal(True, JSONB),
                    True,
                ), JSON),
            ).returning(WorkflowRunModel.id))
            claimed = result.scalar_one_or_none() is not None
            await session.commit()
            return claimed

    async def finish_unstarted_dialer_agent_run(self, run_id: int, organization_id: int, call_id: str) -> bool:
        """Release a prepared execution when its existing call ended before media."""
        async with self.async_session() as session:
            result = await session.execute(update(WorkflowRunModel).where(
                WorkflowRunModel.id == run_id,
                WorkflowRunModel.workflow_id.in_(select(WorkflowModel.id).where(WorkflowModel.organization_id == organization_id)),
                WorkflowRunModel.initial_context.op("->>")("dialer_original_call_id") == call_id,
                WorkflowRunModel.state == "initialized",
            ).values(state="completed",is_completed=True).returning(WorkflowRunModel.id))
            finished = result.scalar_one_or_none() is not None
            await session.commit()
            return finished

    async def get_all_workflow_runs(self) -> list[WorkflowRunModel]:
        async with self.async_session() as session:
            result = await session.execute(select(WorkflowRunModel))
            return result.scalars().all()

    async def get_workflow_runs_for_superadmin(
        self,
        limit: int = 50,
        offset: int = 0,
        filters: Optional[List[Dict[str, Any]]] = None,
        sort_by: Optional[str] = None,
        sort_order: str = "desc",
    ) -> tuple[list[dict], int]:
        """
        Get paginated workflow runs for superadmin with organization information.
        Returns tuple of (workflow_runs, total_count).

        Args:
            sort_by: Field to sort by ('duration', 'created_at', etc.)
            sort_order: 'asc' or 'desc'
        """
        async with self.async_session() as session:
            # Build base query with joins
            base_query = (
                select(WorkflowRunModel)
                .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
                .join(UserModel, WorkflowModel.user_id == UserModel.id)
                .outerjoin(
                    OrganizationModel,
                    UserModel.selected_organization_id == OrganizationModel.id,
                )
            )

            # Apply filters
            base_query = apply_workflow_run_filters(base_query, filters)

            # Count total with filters
            count_query = base_query.with_only_columns(func.count(WorkflowRunModel.id))
            count_result = await session.execute(count_query)
            total_count = count_result.scalar()

            # Get paginated results with filters and sorting
            order_clause = get_workflow_run_order_clause(sort_by, sort_order)
            result = await session.execute(
                base_query.options(
                    joinedload(WorkflowRunModel.workflow).joinedload(
                        WorkflowModel.user
                    ),
                    joinedload(WorkflowRunModel.workflow)
                    .joinedload(WorkflowModel.user)
                    .joinedload(UserModel.selected_organization),
                )
                .order_by(order_clause)
                .limit(limit)
                .offset(offset)
            )
            workflow_runs = result.scalars().all()

            # Format the response
            formatted_runs = []
            for run in workflow_runs:
                organization = (
                    run.workflow.user.selected_organization
                    if run.workflow.user
                    else None
                )
                formatted_runs.append(
                    {
                        "id": run.id,
                        "name": run.name,
                        "workflow_id": run.workflow_id,
                        "workflow_name": run.workflow.name if run.workflow else None,
                        "user_id": run.workflow.user_id if run.workflow else None,
                        "organization_id": organization.id if organization else None,
                        "organization_name": organization.provider_id
                        if organization
                        else None,
                        "mode": run.mode,
                        "is_completed": run.is_completed,
                        "recording_url": run.recording_url,
                        "transcript_url": run.transcript_url,
                        "usage_info": run.usage_info,
                        "cost_info": run.cost_info,
                        "initial_context": run.initial_context,
                        "gathered_context": run.gathered_context,
                        "created_at": run.created_at,
                    }
                )

            return formatted_runs, total_count

    async def get_workflow_run(
        self,
        run_id: int,
        user_id: int = None,
        organization_id: int = None,
        include_sandbox: bool = False,
    ) -> WorkflowRunModel | None:
        async with self.async_session() as session:
            query = (
                select(WorkflowRunModel)
                .options(selectinload(WorkflowRunModel.definition))
                .join(WorkflowRunModel.workflow)
            )

            if organization_id:
                # Filter by organization_id when provided
                query = query.where(
                    WorkflowRunModel.id == run_id,
                    WorkflowModel.organization_id == organization_id,
                )
            elif user_id:
                # Fallback to user_id for backwards compatibility
                query = query.where(
                    WorkflowRunModel.id == run_id,
                    WorkflowModel.user_id == user_id,
                )
            else:
                query = query.where(WorkflowRunModel.id == run_id)

            if not include_sandbox:
                query = query.where(customer_visible_workflow_run())
            result = await session.execute(query)
            return result.scalars().first()

    async def mark_wallet_debit_settled(self, run_id: int) -> None:
        """Mark a run's post-call wallet debit as settled (terminal outcome reached).

        Idempotent: safe to call repeatedly. Used by the normal completion path and by
        the reconcile_wallet_debits sweep once a run's debit succeeds or is terminal.
        """
        async with self.async_session() as session:
            await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.id == run_id)
                .values(wallet_debit_settled_at=datetime.now(UTC))
            )
            await session.commit()

    async def get_unsettled_wallet_debit_run_ids(
        self,
        grace_minutes: int = 10,
        lookback_hours: int = 24,
        limit: int = 200,
    ) -> list[int]:
        """Completed wallet runs (api_key_id IS NULL) whose post-call debit never settled.

        Bounded window: older than `grace_minutes` (so we don't race the normal post-call
        path) and newer than `lookback_hours` (keeps the sweep cheap; the migration
        backfills all pre-existing rows to settled so only post-deploy runs appear here).
        API-key runs bill postpaid via a separate, independently-reconciled path and are
        excluded. Oldest first so a persistent failure is retried before newer ones.
        """
        now = datetime.now(UTC)
        newer_than = now - timedelta(hours=lookback_hours)
        older_than = now - timedelta(minutes=grace_minutes)
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel.id)
                .where(
                    WorkflowRunModel.wallet_debit_settled_at.is_(None),
                    WorkflowRunModel.is_completed.is_(True),
                    WorkflowRunModel.api_key_id.is_(None),
                    WorkflowRunModel.created_at >= newer_than,
                    WorkflowRunModel.created_at <= older_than,
                )
                .order_by(WorkflowRunModel.created_at.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def mark_memory_settled(self, run_id: int) -> None:
        """Mark a run's post-call caller-memory extraction as settled (H4).

        Idempotent. Used by the normal completion path and by the reconcile_memory sweep
        once the memory webhook succeeds or is terminal.
        """
        async with self.async_session() as session:
            await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.id == run_id)
                .values(memory_settled_at=datetime.now(UTC))
            )
            await session.commit()

    async def get_unsettled_memory_run_ids(
        self,
        grace_minutes: int = 10,
        lookback_hours: int = 24,
        limit: int = 200,
    ) -> list[int]:
        """Completed runs whose post-call caller-memory extraction never settled (H4).

        Bounded window: older than `grace_minutes` (don't race the normal post-call path)
        and newer than `lookback_hours` (the migration backfills pre-existing rows to
        settled, so only post-deploy runs appear here). Oldest first. Runs with no caller
        number settle terminally on the first re-fire (the webhook returns True), so they
        don't linger.
        """
        now = datetime.now(UTC)
        newer_than = now - timedelta(hours=lookback_hours)
        older_than = now - timedelta(minutes=grace_minutes)
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel.id)
                .where(
                    WorkflowRunModel.memory_settled_at.is_(None),
                    WorkflowRunModel.is_completed.is_(True),
                    WorkflowRunModel.created_at >= newer_than,
                    WorkflowRunModel.created_at <= older_than,
                )
                .order_by(WorkflowRunModel.created_at.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_workflow_run_by_id(self, run_id: int) -> WorkflowRunModel | None:
        """Get workflow run by ID without user filtering - for background tasks"""
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel)
                .options(
                    joinedload(WorkflowRunModel.workflow).joinedload(WorkflowModel.user),
                    selectinload(WorkflowRunModel.definition),
                )
                .where(WorkflowRunModel.id == run_id)
            )
            return result.scalars().first()

    async def get_organization_id_by_workflow_run_id(
        self, run_id: int | None
    ) -> int | None:
        """Resolve organization_id from a workflow run via workflow.user."""
        if not run_id:
            return None
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowModel.organization_id)
                .join(
                    WorkflowRunModel, WorkflowRunModel.workflow_id == WorkflowModel.id
                )
                .where(WorkflowRunModel.id == run_id)
            )
            return result.scalar_one_or_none()

    async def get_workflow_id_by_workflow_run_id(
        self, run_id: int | None
    ) -> int | None:
        """Resolve workflow_id from a workflow run."""
        if not run_id:
            return None
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel.workflow_id).where(
                    WorkflowRunModel.id == run_id
                )
            )
            return result.scalar_one_or_none()

    async def get_workflow_runs_by_workflow_id(
        self,
        workflow_id: int,
        user_id: int = None,
        organization_id: int = None,
        limit: int = 50,
        offset: int = 0,
        filters: Optional[List[Dict[str, Any]]] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = "desc",
    ) -> tuple[list[WorkflowRunResponseSchema], int]:
        async with self.async_session() as session:
            # Build base query
            base_query = (
                select(WorkflowRunModel)
                .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
                .where(
                    WorkflowRunModel.workflow_id == workflow_id,
                    customer_visible_workflow_run(),
                )
            )

            if organization_id:
                # Filter by organization_id when provided
                base_query = base_query.where(
                    WorkflowModel.organization_id == organization_id
                )
            elif user_id:
                # Fallback to user_id for backwards compatibility
                base_query = base_query.where(WorkflowModel.user_id == user_id)

            # Apply filters
            base_query = apply_workflow_run_filters(base_query, filters)

            # Count total with filters
            count_query = base_query.with_only_columns(func.count(WorkflowRunModel.id))
            count_result = await session.execute(count_query)
            total_count = count_result.scalar()

            # Get paginated results with filters and sorting
            order_clause = get_workflow_run_order_clause(sort_by, sort_order)
            result = await session.execute(
                base_query.order_by(order_clause).limit(limit).offset(offset)
            )
            runs = [
                WorkflowRunResponseSchema.model_validate(
                    {
                        "id": run.id,
                        "workflow_id": run.workflow_id,
                        "name": run.name,
                        "mode": run.mode,
                        "created_at": run.created_at,
                        "is_completed": run.is_completed,
                        "recording_url": run.recording_url,
                        "transcript_url": run.transcript_url,
                        "cost_info": {
                            "dograh_token_usage": (
                                run.cost_info.get("dograh_token_usage")
                                if run.cost_info
                                and "dograh_token_usage" in run.cost_info
                                else round(
                                    float(run.cost_info.get("total_cost_usd", 0)) * 100,
                                    2,
                                )
                                if run.cost_info and "total_cost_usd" in run.cost_info
                                else 0
                            ),
                            "call_duration_seconds": int(
                                round(run.cost_info.get("call_duration_seconds") or 0)
                            )
                            if run.cost_info
                            else None,
                        }
                        if run.cost_info
                        else None,
                        "definition_id": run.definition_id,
                        "initial_context": run.initial_context,
                        "gathered_context": run.gathered_context,
                        "call_type": run.call_type,
                        "public_access_token": run.public_access_token,
                    }
                )
                for run in result.scalars().all()
            ]
            return runs, total_count

    async def update_workflow_run(
        self,
        run_id: int,
        is_completed: bool = False,
        recording_url: str | None = None,
        transcript_url: str | None = None,
        storage_backend: str | None = None,
        usage_info: dict | None = None,
        cost_info: dict | None = None,
        initial_context: dict | None = None,
        gathered_context: dict | None = None,
        logs: dict | None = None,
        state: str | None = None,
        annotations: dict | None = None,
    ) -> WorkflowRunModel:
        async with self.async_session() as session:
            # Use SELECT FOR UPDATE to lock the row during the update
            result = await session.execute(
                select(WorkflowRunModel)
                .where(WorkflowRunModel.id == run_id)
                .with_for_update()
            )
            run = result.scalars().first()
            if not run:
                raise ValueError(f"Workflow run with ID {run_id} not found")
            if recording_url:
                run.recording_url = recording_url
            if transcript_url:
                run.transcript_url = transcript_url
            if storage_backend:
                run.storage_backend = storage_backend
            if usage_info:
                run.usage_info = usage_info
            if cost_info:
                run.cost_info = cost_info
            if initial_context:
                run.initial_context = initial_context
            if gathered_context:
                # Lets merge the incoming gathered context keys with the existing ones
                run.gathered_context = {
                    **run.gathered_context,
                    **gathered_context,
                }
            if logs:
                # Lets merge the incoming logs key with existing ones
                run.logs = {**run.logs, **logs}
            if annotations:
                run.annotations = {**run.annotations, **annotations}
            if is_completed:
                run.is_completed = is_completed
            if state:
                run.state = state
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(run)
        return run

    async def get_workflow_run_with_context(
        self, workflow_run_id: int
    ) -> Tuple[Optional[WorkflowRunModel], Optional[int]]:
        """
        Get workflow run with all related data and return organization_id.

        Returns:
            Tuple of (workflow_run, organization_id) or (None, None) if not found
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel)
                .options(
                    selectinload(WorkflowRunModel.definition),
                    selectinload(WorkflowRunModel.workflow).options(
                        selectinload(WorkflowModel.user),
                        selectinload(WorkflowModel.current_definition),
                    ),
                )
                .where(WorkflowRunModel.id == workflow_run_id)
            )
            workflow_run = result.scalars().first()

            if not workflow_run:
                return None, None

            if not workflow_run.workflow or not workflow_run.workflow.user:
                return workflow_run, None

            organization_id = workflow_run.workflow.user.selected_organization_id
            return workflow_run, organization_id

    async def ensure_public_access_token(self, workflow_run_id: int) -> Optional[str]:
        """Generate a public access token if not exists, return existing if present (idempotent).

        Args:
            workflow_run_id: The ID of the workflow run

        Returns:
            The public access token string, or None if workflow run not found
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel).where(
                    WorkflowRunModel.id == workflow_run_id,
                    customer_visible_workflow_run(),
                )
            )
            run = result.scalars().first()
            if not run:
                return None

            # Return existing token if present
            if run.public_access_token:
                return run.public_access_token

            # Generate and persist new token
            token = str(uuid.uuid4())
            run.public_access_token = token

            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(run)

            return run.public_access_token

    async def get_workflow_run_by_public_token(
        self, token: str
    ) -> Optional[WorkflowRunModel]:
        """Lookup workflow run by public access token.

        Args:
            token: The public access token

        Returns:
            The WorkflowRunModel if found, None otherwise
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel).where(
                    WorkflowRunModel.public_access_token == token,
                    customer_visible_workflow_run(),
                )
            )
            return result.scalars().first()

    async def get_workflow_run_by_call_id(
        self, call_id: str
    ) -> Optional[WorkflowRunModel]:
        """Find workflow run by call_id stored in gathered_context.

        Args:
            call_id: The telephony call ID to search for

        Returns:
            The WorkflowRunModel if found, None otherwise
        """
        async with self.async_session() as session:
            # Use JSON text extraction to find matching call_id
            # This leverages the idx_workflow_runs_call_id index
            result = await session.execute(
                select(WorkflowRunModel)
                .options(
                    joinedload(WorkflowRunModel.workflow).joinedload(WorkflowModel.user)
                )
                .where(
                    WorkflowRunModel.gathered_context.op("->>")("call_id") == call_id
                )
                .order_by(WorkflowRunModel.created_at.desc())
                .limit(1)
            )
            return result.scalars().first()
