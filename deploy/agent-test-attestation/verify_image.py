"""No-network preflight of the patched production-layout runtime image."""

import asyncio
import copy
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["SYSEVO_MEMORY_SECRET"] = "synthetic-attestation-preflight"

from fastapi import HTTPException

from api.routes import workflow_text_chat as route
from api.services.workflow.agent_test_repair import hash_workflow_snapshot
from api.services.workflow.test_sandbox_binding import (
    validate_sandbox_execution_binding,
)


async def verify() -> None:
    secret = os.environ["SYSEVO_MEMORY_SECRET"]
    candidate = SimpleNamespace(
        workflow_id=42,
        id=4,
        version_number=4,
        status="draft",
        workflow_json={"nodes": []},
        workflow_configurations={},
        template_context_variables={},
    )
    candidate_hash = hash_workflow_snapshot(candidate.workflow_json, {}, {})
    run = SimpleNamespace(id=7, workflow_id=42, annotations={})
    user = SimpleNamespace(id=3, selected_organization_id=9)
    payload = {
        "candidate_draft_version_number": 4,
        "sandbox_mode": True,
        "annotations": {
            "agent_test_run_id": "11111111-1111-4111-8111-111111111111",
            "scenario_id": "qa",
            "sysevo_test_sandbox": {"enabled": False},
        },
    }

    async def update_run(_run_id, *, annotations):
        run.annotations = annotations
        return run

    with (
        patch.object(
            route.db_client, "get_workflow_organization_id", AsyncMock(return_value=9)
        ),
        patch.object(
            route.db_client,
            "get_workflow_versions",
            AsyncMock(return_value=[candidate]),
        ),
        patch.object(
            route.db_client, "create_workflow_run", AsyncMock(return_value=run)
        ) as create_run,
        patch.object(
            route.db_client, "update_workflow_run", AsyncMock(side_effect=update_run)
        ),
        patch.object(
            route.db_client,
            "ensure_workflow_run_text_session",
            AsyncMock(return_value=SimpleNamespace()),
        ),
        patch.object(route, "_ensure_text_chat_quota", AsyncMock()) as quota,
        patch.object(route, "initialize_text_chat_session", AsyncMock()),
        patch.object(
            route, "_execute_pending_turn_response", AsyncMock(return_value=run)
        ),
    ):
        rejected = route.CreateTextChatSessionRequest(
            **payload, expected_workflow_hash="0" * 64
        )
        try:
            await route.create_text_chat_session(42, rejected, user, secret)
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("An untested candidate hash was accepted")
        create_run.assert_not_awaited()
        quota.assert_not_awaited()
        accepted = route.CreateTextChatSessionRequest(
            **payload, expected_workflow_hash=candidate_hash
        )
        result = await route.create_text_chat_session(42, accepted, user, secret)
        assert result is run
        binding = run.annotations["sysevo_test_sandbox"]["workflow_binding"]
        assert binding["configuration_hash"] == candidate_hash
        assert run.annotations["sysevo_test_sandbox"]["enabled"] is True
        validate_sandbox_execution_binding(run, candidate, secret)
        altered = copy.deepcopy(candidate)
        altered.workflow_json = {"nodes": [{"data": {"prompt": "changed"}}]}
        try:
            validate_sandbox_execution_binding(run, altered, secret)
        except ValueError:
            pass
        else:
            raise AssertionError("A changed draft retained its attestation")
    print(
        "Candidate image passed early hash rejection, server-owned signing and drift checks; no external actions executed."
    )


if __name__ == "__main__":
    asyncio.run(verify())
