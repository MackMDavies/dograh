from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.workflow.test_sandbox_binding import (
    SandboxWorkflowNotFoundError,
    make_sandbox_workflow_binding,
    resolve_requested_sandbox_definition,
    validate_requested_sandbox_definition,
    validate_sandbox_execution_binding,
)
from api.services.workflow.test_sandbox_mode import resolve_sandbox_http_action

RUN_ID = "11111111-1111-4111-8111-111111111111"
SECRET = "synthetic-test-secret"


def definition():
    return SimpleNamespace(
        workflow_id=42,
        version_number=4,
        status="draft",
        workflow_json={"nodes": []},
        workflow_configurations={},
        template_context_variables={},
    )


def bound_run():
    candidate = definition()
    binding = make_sandbox_workflow_binding(
        candidate,
        workflow_id=42,
        workflow_run_id=7,
        run_id=RUN_ID,
        scenario_id="qa",
        signing_secret=SECRET,
    )
    run = SimpleNamespace(
        id=7,
        workflow_id=42,
        annotations={
            "sysevo_test_sandbox": {
                "enabled": True,
                "agent_test_run_id": RUN_ID,
                "scenario_id": "qa",
                "workflow_binding": binding,
            }
        },
    )
    return candidate, run


def test_each_turn_requires_the_same_signed_candidate_configuration():
    candidate, run = bound_run()
    validate_sandbox_execution_binding(run, candidate, SECRET)
    for field, value in [
        ("workflow_json", {"nodes": [{"prompt": "changed"}]}),
        ("workflow_configurations", {"model": "changed"}),
        ("template_context_variables", {"business": "changed"}),
    ]:
        changed = deepcopy(candidate)
        setattr(changed, field, value)
        with pytest.raises(ValueError, match="configuration changed"):
            validate_sandbox_execution_binding(run, changed, SECRET)


def test_binding_matches_the_independent_edge_verification_vector():
    _, run = bound_run()
    binding = run.annotations["sysevo_test_sandbox"]["workflow_binding"]
    assert (
        binding["configuration_hash"]
        == "3a6c5731f42967f3150ec663339ec161bf6f849595fe2bcea0127693ff5c194b"
    )
    assert (
        binding["signature"]
        == "3cd799beea0f2a6a421a64752cddc72d31dc4162a9b50c3a3e9efb754fd591a6"
    )


def test_workflow_and_version_identity_cannot_be_changed_even_with_the_same_content():
    candidate, run = bound_run()
    for field, value in [
        ("workflow_id", 99),
        ("version_number", 5),
        ("status", "published"),
    ]:
        changed = deepcopy(candidate)
        setattr(changed, field, value)
        with pytest.raises(ValueError):
            validate_sandbox_execution_binding(run, changed, SECRET)


def test_caller_cannot_forge_or_replay_a_binding_into_another_runtime_session():
    candidate, run = bound_run()
    with pytest.raises(ValueError):
        validate_sandbox_execution_binding(run, candidate, "wrong-test-secret")
    run.id = 8
    with pytest.raises(ValueError):
        validate_sandbox_execution_binding(run, candidate, SECRET)
    run.id = 7
    run.annotations["sysevo_test_sandbox"]["workflow_binding"]["signature"] = "0" * 64
    with pytest.raises(ValueError):
        validate_sandbox_execution_binding(run, candidate, SECRET)


def test_legacy_sandbox_sessions_require_attestation_but_normal_sessions_keep_working():
    candidate, run = bound_run()
    del run.annotations["sysevo_test_sandbox"]["workflow_binding"]
    with pytest.raises(ValueError, match="no server-owned"):
        validate_sandbox_execution_binding(run, candidate, SECRET)
    run.annotations = {}
    validate_sandbox_execution_binding(run, candidate, "")


def test_expected_candidate_hash_is_checked_before_runtime_creation():
    with pytest.raises(ValueError, match="configuration changed"):
        validate_requested_sandbox_definition(definition(), 42, "0" * 64)


@pytest.mark.asyncio
async def test_another_organization_cannot_read_version_configuration():
    db = SimpleNamespace(
        get_workflow=AsyncMock(return_value=None), get_workflow_versions=AsyncMock()
    )
    with pytest.raises(SandboxWorkflowNotFoundError):
        await resolve_requested_sandbox_definition(
            db,
            workflow_id=42,
            organization_id=99,
            version_number=4,
            version_kind="draft",
        )
    db.get_workflow.assert_awaited_once_with(42, organization_id=99)
    db.get_workflow_versions.assert_not_awaited()


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("save", "test_memory_save"),
        ("lookup", "test_memory_lookup"),
        ("check_availability", "check_availability"),
        ("book_appointment", "book_appointment"),
    ],
)
def test_memory_operations_use_the_run_scoped_adapter(operation, expected):
    url = (
        "https://rdiwzxtibglitvtextzs.supabase.co/functions/v1/agent-memory-lookup?action="
        + operation
    )
    assert resolve_sandbox_http_action(url, "POST") == expected
    assert resolve_sandbox_http_action(url, "GET") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.resend.com/contacts",
        "http://api.resend.com/emails",
        "https://user:password@api.resend.com/emails",
        "https://api.twilio.com/2010-04-01/Accounts/AC" + "a" * 32 + "/Calls.json",
        "https://rdiwzxtibglitvtextzs.supabase.co/functions/v1/agent-memory-lookup?action=unsupported",
    ],
)
def test_known_provider_operations_never_fall_through_to_a_generic_webhook(url):
    assert resolve_sandbox_http_action(url, "POST") is None


def test_fixed_provider_routes_and_custom_webhook_remain_available():
    assert (
        resolve_sandbox_http_action("https://api.resend.com/emails", "POST")
        == "send_test_email"
    )
    assert (
        resolve_sandbox_http_action(
            "https://api.twilio.com/2010-04-01/Accounts/AC"
            + "a" * 32
            + "/Messages.json",
            "POST",
        )
        == "send_test_sms"
    )
    assert (
        resolve_sandbox_http_action("https://client.example.com/leads", "POST")
        == "send_test_webhook"
    )
