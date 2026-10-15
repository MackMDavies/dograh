"""Deterministic, bounded changes used only by approved QA repair candidates."""

import re
import hashlib
import json
import copy
from typing import Any


SANDBOX_MARKER_KEYS = {
    "sysevo_internal_test",
    "agent_test_run_id",
    "agent_test_sandbox_token",
    "x-sysevo-test-token",
    "sysevo_test_mode",
}
SANDBOX_MARKER_TEXT = re.compile(
    r"(?:agent-test-sandbox-execute|x-sysevo-test-token|sysevo-qa-[a-z0-9_-]+)",
    re.IGNORECASE,
)
_REMOVED = object()
CONCISENESS_GUIDANCE = (
    "During spoken dialogue, keep each reply concise and natural. Give only the information needed "
    "for the current step, ask one question at a time, and avoid repeating information. Be complete "
    "when the caller's request requires more detail."
)
GOAL_COMPLETION_GUIDANCE = (
    "Guide the caller toward the configured outcome for this agent. Ask only for information needed "
    "to complete the next step, use configured tools for actions, and confirm completion only when "
    "the tool or system result verifies it. If the action cannot be completed, explain the limitation "
    "and offer the configured next step. Do not invent commitments, availability, prices, or outcomes."
)
SUPPORTED_TEST_REPAIR_OPERATIONS = (
    "append_conciseness_guidance",
    "append_goal_completion_guidance",
    "remove_sandbox_markers",
)


def workflow_snapshot_hash(workflow_json: Any, workflow_configurations: Any, template_context_variables: Any) -> str:
    snapshot = {
        "workflow_json": workflow_json or {},
        "workflow_configurations": workflow_configurations or {},
        "template_context_variables": template_context_variables or {},
    }
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def remove_sandbox_markers(value: Any) -> tuple[Any, list[str]]:
    """Remove known temporary test markers and report every changed JSON path."""
    removed_paths: list[str] = []

    def visit(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            cleaned: dict[str, Any] = {}
            for key, child in item.items():
                child_path = f"{path}.{key}"
                marker_value_is_active = child is not False and child is not None and child != ""
                if key.lower() in SANDBOX_MARKER_KEYS and marker_value_is_active:
                    removed_paths.append(child_path)
                    continue
                next_value = visit(child, child_path)
                if next_value is not _REMOVED:
                    cleaned[key] = next_value
            return cleaned
        if isinstance(item, list):
            cleaned_list = []
            for index, child in enumerate(item):
                next_value = visit(child, f"{path}[{index}]")
                if next_value is not _REMOVED:
                    cleaned_list.append(next_value)
            return cleaned_list
        if isinstance(item, str) and SANDBOX_MARKER_TEXT.search(item):
            removed_paths.append(path)
            return _REMOVED
        return item

    result = visit(value, "$")
    return result, removed_paths


def append_conciseness_guidance(value: Any) -> tuple[Any, list[str]]:
    """Add bounded speaking guidance to prompted workflow nodes, without altering other data."""
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        return value, []
    candidate = copy.deepcopy(value)
    changed_paths: list[str] = []
    for index, node in enumerate(candidate["nodes"]):
        if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
            continue
        prompt = node["data"].get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or CONCISENESS_GUIDANCE in prompt:
            continue
        node["data"]["prompt"] = f"{prompt.rstrip()}\n\n{CONCISENESS_GUIDANCE}"
        changed_paths.append(f"$.nodes[{index}].data.prompt")
    return candidate, changed_paths


def append_goal_completion_guidance(value: Any) -> tuple[Any, list[str]]:
    """Add bounded configured-outcome guidance to prompted workflow nodes."""
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        return value, []
    candidate = copy.deepcopy(value)
    changed_paths: list[str] = []
    for index, node in enumerate(candidate["nodes"]):
        if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
            continue
        prompt = node["data"].get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or GOAL_COMPLETION_GUIDANCE in prompt:
            continue
        node["data"]["prompt"] = f"{prompt.rstrip()}\n\n{GOAL_COMPLETION_GUIDANCE}"
        changed_paths.append(f"$.nodes[{index}].data.prompt")
    return candidate, changed_paths
