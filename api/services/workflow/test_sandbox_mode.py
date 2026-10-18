"""Trusted test-sandbox markers shared by workflow routes and tool execution."""

import hmac
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

SANDBOX_ANNOTATION = "sysevo_test_sandbox"


def resolve_sandbox_http_action(url: str, method: str) -> str | None:
    """Classify intercepted operations without ever contacting the configured URL."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return None
        method = method.upper()
        if (
            parsed.hostname == "rdiwzxtibglitvtextzs.supabase.co"
            and parsed.path == "/functions/v1/agent-memory-lookup"
        ):
            if method != "POST":
                return None
            action = parse_qs(parsed.query).get("action", [""])[0]
            return {
                "save": "test_memory_save",
                "lookup": "test_memory_lookup",
                "check_availability": "check_availability",
                "book_appointment": "book_appointment",
            }.get(action)
        if parsed.hostname == "api.resend.com":
            return (
                "send_test_email"
                if parsed.path == "/emails" and method == "POST"
                else None
            )
        if parsed.hostname == "api.twilio.com":
            return (
                "send_test_sms"
                if method == "POST"
                and re.fullmatch(
                    r"/2010-04-01/Accounts/AC[0-9a-fA-F]{32}/Messages\.json",
                    parsed.path,
                )
                else None
            )
        return (
            "send_test_webhook"
            if parsed.hostname and method in {"POST", "PUT", "PATCH"}
            else None
        )
    except (ValueError, TypeError, AttributeError):
        return None


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
    # The marker is server-owned; any enabled marker must suppress side effects.
    # Callers that need run/scenario identity validate it separately.
    annotations = getattr(workflow_run, "annotations", None)
    if not isinstance(annotations, dict):
        return False
    marker = annotations.get(SANDBOX_ANNOTATION)
    return isinstance(marker, dict) and marker.get("enabled") is True


def sandbox_post_call_policy(workflow_run: Any) -> dict[str, Any] | None:
    """Return a safe, non-identifying block record for post-call side effects.

    Sandbox runs must not trigger tenant webhooks, integration handlers, caller
    memory, billing notifications, or client live-call events. The run itself
    can retain this summary as evidence that those paths were held.
    """
    if not is_sandbox_test_run(workflow_run):
        return None
    context = sandbox_test_run_context(workflow_run)
    return {
        "status": "blocked",
        "reason": "post_call_side_effects_are_disabled_for_agent_tests"
        if context
        else "sandbox_marker_has_incomplete_run_identity",
        "agent_test_run_id": context["agent_test_run_id"] if context else None,
        "scenario_id": context["scenario_id"] if context else None,
        "blocked_surfaces": [
            "workflow_webhooks",
            "completion_handlers",
            "caller_memory",
            "wallet_debit",
            "client_live_call_notifications",
        ],
    }


def sandbox_test_run_context(workflow_run: Any) -> dict[str, str] | None:
    annotations = getattr(workflow_run, "annotations", None)
    if not isinstance(annotations, dict):
        return None
    marker = annotations.get(SANDBOX_ANNOTATION)
    if not isinstance(marker, dict) or marker.get("enabled") is not True:
        return None
    run_id = marker.get("agent_test_run_id")
    scenario_id = marker.get("scenario_id")
    if not isinstance(run_id, str) or not isinstance(scenario_id, str):
        return None
    try:
        if str(uuid.UUID(run_id)) != run_id.lower():
            return None
    except (ValueError, AttributeError):
        return None
    if not scenario_id or len(scenario_id) > 100:
        return None
    return {"agent_test_run_id": run_id, "scenario_id": scenario_id}
