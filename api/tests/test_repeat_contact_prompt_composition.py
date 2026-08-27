"""compose_system_prompt_for_node must use the repeat-contact prompt variant
for the START node only, matching prior_contact_relationship_type from
call_context_vars — and must leave every other node's prompt untouched."""

from api.services.workflow.dto import (
    EndCallNodeData,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    EdgeDataDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine_context_composer import (
    compose_system_prompt_for_node,
)
from api.services.workflow.workflow_graph import WorkflowGraph


def _identity_format_prompt(text: str) -> str:
    return text or ""


def _workflow_with_variants() -> WorkflowGraph:
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start",
                    prompt="default start prompt",
                    is_start=True,
                    add_global_prompt=False,
                    repeat_contact_variants_enabled=True,
                    repeat_contact_prompt_gatekeeper_screened="gatekeeper variant prompt",
                ),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=200),
                data=EndCallNodeData(
                    name="End",
                    prompt="default end prompt",
                    is_end=True,
                    add_global_prompt=False,
                ),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="start-end",
                source="start",
                target="end",
                data=EdgeDataDTO(label="End", condition="end the call"),
            ),
        ],
    )
    return WorkflowGraph(dto)


def test_start_node_uses_variant_when_bucket_matches():
    workflow = _workflow_with_variants()
    result = compose_system_prompt_for_node(
        node=workflow.nodes["start"],
        workflow=workflow,
        format_prompt=_identity_format_prompt,
        has_recordings=False,
        call_context_vars={"prior_contact_relationship_type": "gatekeeper_screened"},
    )
    assert result == "gatekeeper variant prompt"


def test_start_node_uses_default_when_bucket_has_no_variant():
    workflow = _workflow_with_variants()
    result = compose_system_prompt_for_node(
        node=workflow.nodes["start"],
        workflow=workflow,
        format_prompt=_identity_format_prompt,
        has_recordings=False,
        call_context_vars={"prior_contact_relationship_type": "no_answer"},
    )
    assert result == "default start prompt"


def test_start_node_uses_default_when_no_call_context_vars():
    workflow = _workflow_with_variants()
    result = compose_system_prompt_for_node(
        node=workflow.nodes["start"],
        workflow=workflow,
        format_prompt=_identity_format_prompt,
        has_recordings=False,
        call_context_vars=None,
    )
    assert result == "default start prompt"


def test_non_start_node_ignores_bucket_entirely():
    # The end node has no repeat-contact fields at all — this proves the
    # guard is on node.is_start, not "does this node have variants".
    workflow = _workflow_with_variants()
    result = compose_system_prompt_for_node(
        node=workflow.nodes["end"],
        workflow=workflow,
        format_prompt=_identity_format_prompt,
        has_recordings=False,
        call_context_vars={"prior_contact_relationship_type": "gatekeeper_screened"},
    )
    assert result == "default end prompt"


def test_start_call_shaped_node_with_is_start_false_ignores_variant():
    # Same StartCallNodeData shape as the real start node (so it DOES have
    # populated repeat_contact_prompts), but is_start=False — this is the
    # test that actually isolates the guard from "variants happen to be
    # empty", which test_non_start_node_ignores_bucket_entirely cannot do
    # (EndCallNodeData has no repeat_contact_* fields at all, so that test
    # would still pass even if the is_start check were removed).
    workflow = _workflow_with_variants()
    fake_non_start_node = workflow.nodes["start"]
    fake_non_start_node.is_start = False
    assert fake_non_start_node.repeat_contact_prompts.get("gatekeeper_screened") == (
        "gatekeeper variant prompt"
    )
    result = compose_system_prompt_for_node(
        node=fake_non_start_node,
        workflow=workflow,
        format_prompt=_identity_format_prompt,
        has_recordings=False,
        call_context_vars={"prior_contact_relationship_type": "gatekeeper_screened"},
    )
    assert result == "default start prompt"
