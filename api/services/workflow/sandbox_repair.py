"""Deterministic, bounded changes used only by approved QA repair candidates."""

import re
import hashlib
import json
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
