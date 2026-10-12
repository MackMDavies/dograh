"""Trusted test-sandbox markers shared by workflow routes and tool execution."""

import hmac
from typing import Any

SANDBOX_ANNOTATION = "sysevo_test_sandbox"


def sandbox_secret_matches(supplied: str | None, expected: str | None) -> bool:
    """Compare the internal sandbox credential without leaking timing information."""
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


def build_run_annotations(
    defaults: dict[str, Any],
    requested: dict[str, Any] | None,
    *,
    sandbox_authorized: bool,
) -> dict[str, Any]:
    """Prevent caller annotations from forging the server-owned sandbox marker."""
    annotations = {**defaults, **(requested or {})}
    annotations.pop(SANDBOX_ANNOTATION, None)
    if sandbox_authorized:
        requested_run_id = (requested or {}).get("agent_test_run_id")
        requested_scenario_id = (requested or {}).get("scenario_id")
        marker: dict[str, Any] = {"enabled": True, "source": "agent_testing_engine"}
        if isinstance(requested_run_id, str) and len(requested_run_id) == 36:
            marker["agent_test_run_id"] = requested_run_id
        if isinstance(requested_scenario_id, str) and len(requested_scenario_id) <= 100:
            marker["scenario_id"] = requested_scenario_id
        annotations[SANDBOX_ANNOTATION] = marker
    return annotations


def is_sandbox_test_run(workflow_run: Any) -> bool:
    annotations = getattr(workflow_run, "annotations", None)
    if not isinstance(annotations, dict):
        return False
    marker = annotations.get(SANDBOX_ANNOTATION)
    return isinstance(marker, dict) and marker.get("enabled") is True


def sandbox_test_run_context(workflow_run: Any) -> dict[str, str] | None:
    annotations = getattr(workflow_run, "annotations", None)
    if not isinstance(annotations, dict):
        return None
    marker = annotations.get(SANDBOX_ANNOTATION)
    if not isinstance(marker, dict) or marker.get("enabled") is not True:
        return None
    run_id = marker.get("agent_test_run_id")
    scenario_id = marker.get("scenario_id")
    if not isinstance(run_id, str) or len(run_id) != 36 or not isinstance(scenario_id, str):
        return None
    return {"agent_test_run_id": run_id, "scenario_id": scenario_id}
