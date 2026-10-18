"""Server-owned configuration attestations for isolated agent-test text turns."""

import hashlib
import hmac
import json
import re
from typing import Any
from uuid import UUID

from api.services.workflow.sandbox_repair import workflow_snapshot_hash
from api.services.workflow.test_sandbox_mode import (
    SANDBOX_ANNOTATION,
    is_sandbox_test_run,
)


class SandboxWorkflowNotFoundError(ValueError):
    pass


async def resolve_requested_sandbox_definition(
    db: Any,
    *,
    workflow_id: int,
    organization_id: int,
    version_number: int,
    version_kind: str,
    expected_hash: str | None = None,
) -> Any:
    """Resolve ownership before reading any version configuration."""
    workflow = await db.get_workflow(workflow_id, organization_id=organization_id)
    if workflow is None:
        raise SandboxWorkflowNotFoundError("Workflow not found")
    versions = await db.get_workflow_versions(workflow_id, limit=None)
    definition = next(
        (
            v
            for v in versions
            if v.version_number == version_number and v.status == version_kind
        ),
        None,
    )
    if definition is None:
        raise ValueError("The requested workflow version is no longer available")
    validate_requested_sandbox_definition(definition, workflow_id, expected_hash)
    return definition


def validate_sandbox_test_identity(run_id: str, scenario_id: str) -> str:
    try:
        canonical_run_id = str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("A valid agent test run identity is required") from exc
    if not isinstance(scenario_id, str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,100}", scenario_id
    ):
        raise ValueError("A valid agent test scenario identity is required")
    return canonical_run_id


def validate_requested_sandbox_definition(
    definition: Any, workflow_id: int, expected_hash: str | None = None
) -> str:
    if (
        getattr(definition, "workflow_id", None) != workflow_id
        or type(getattr(definition, "version_number", None)) is not int
        or definition.version_number < 1
        or getattr(definition, "status", None) not in {"draft", "published"}
    ):
        raise ValueError("The requested sandbox workflow definition is not valid")
    digest = workflow_snapshot_hash(
        definition.workflow_json,
        definition.workflow_configurations,
        definition.template_context_variables,
    )
    if expected_hash is not None and (
        not isinstance(expected_hash, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_hash)
        or not hmac.compare_digest(expected_hash, digest)
    ):
        raise ValueError(
            "The requested candidate configuration changed before the sandbox session"
        )
    return digest


def make_sandbox_workflow_binding(
    definition: Any,
    *,
    workflow_id: int,
    workflow_run_id: int,
    run_id: str,
    scenario_id: str,
    signing_secret: str,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    digest = validate_requested_sandbox_definition(
        definition, workflow_id, expected_hash
    )
    if not signing_secret or type(workflow_run_id) is not int or workflow_run_id < 1:
        raise ValueError(
            "A runtime session identity and internal signing credential are required"
        )
    canonical_run_id = validate_sandbox_test_identity(run_id, scenario_id)
    binding = {
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "workflow_version": definition.version_number,
        "version_kind": definition.status,
        "configuration_hash": digest,
        "agent_test_run_id": canonical_run_id,
        "scenario_id": scenario_id,
    }
    encoded = json.dumps(
        binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    binding["signature"] = hmac.new(
        signing_secret.encode("utf-8"), encoded, hashlib.sha256
    ).hexdigest()
    return binding


def validate_sandbox_execution_binding(
    workflow_run: Any, definition: Any, signing_secret: str
) -> None:
    """Run before each model/tool pipeline, not just when a session is created."""
    if not is_sandbox_test_run(workflow_run):
        return
    marker = workflow_run.annotations.get(SANDBOX_ANNOTATION, {})
    binding = marker.get("workflow_binding")
    if not isinstance(binding, dict):
        raise ValueError(
            "The sandbox session has no server-owned configuration attestation"
        )
    actual = make_sandbox_workflow_binding(
        definition,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.id,
        run_id=marker.get("agent_test_run_id"),
        scenario_id=marker.get("scenario_id"),
        expected_hash=binding.get("configuration_hash"),
        signing_secret=signing_secret,
    )
    supplied_signature = binding.get("signature")
    if (
        not isinstance(supplied_signature, str)
        or not hmac.compare_digest(actual["signature"], supplied_signature)
        or actual != binding
    ):
        raise ValueError(
            "The sandbox session no longer matches its attested workflow configuration"
        )
