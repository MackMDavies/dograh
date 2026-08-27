"""PipecatEngine.get_node_greeting must use the repeat-contact greeting
variant for the START node only, matching prior_contact_relationship_type
from _call_context_vars."""

from api.services.workflow.dto import (
    Position,
    ReactFlowDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph


def _workflow_with_greeting_variants() -> WorkflowGraph:
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start",
                    prompt="default prompt",
                    greeting="default greeting {{first_name}}",
                    is_start=True,
                    repeat_contact_variants_enabled=True,
                    repeat_contact_greeting_no_answer="Hi again {{first_name}}, sorry we missed you.",
                ),
            ),
        ],
        edges=[],
    )
    return WorkflowGraph(dto)


def test_start_node_greeting_uses_variant_when_bucket_matches():
    workflow = _workflow_with_greeting_variants()
    engine = PipecatEngine(
        workflow=workflow,
        call_context_vars={
            "first_name": "Jamie",
            "prior_contact_relationship_type": "no_answer",
        },
    )
    result = engine.get_node_greeting("start")
    assert result == ("text", "Hi again Jamie, sorry we missed you.")


def test_start_node_greeting_uses_default_when_bucket_has_no_variant():
    workflow = _workflow_with_greeting_variants()
    engine = PipecatEngine(
        workflow=workflow,
        call_context_vars={
            "first_name": "Jamie",
            "prior_contact_relationship_type": "spoke_directly",
        },
    )
    result = engine.get_node_greeting("start")
    assert result == ("text", "default greeting Jamie")


def test_start_node_greeting_uses_default_for_first_time_contact():
    workflow = _workflow_with_greeting_variants()
    engine = PipecatEngine(
        workflow=workflow,
        call_context_vars={
            "first_name": "Jamie",
            "prior_contact_relationship_type": "none",
        },
    )
    result = engine.get_node_greeting("start")
    assert result == ("text", "default greeting Jamie")


def test_non_start_node_with_same_shape_ignores_variant():
    # Same StartCallNodeData shape as the real start node (so it DOES have
    # a populated repeat_contact_greetings map), but is_start=False — this
    # isolates the guard itself. A bug that removed the `if node.is_start`
    # check (or applied the resolver unconditionally) would slip past the
    # three tests above, since they only ever exercise the start node.
    workflow = _workflow_with_greeting_variants()
    fake_non_start_node = workflow.nodes["start"]
    fake_non_start_node.is_start = False
    assert (
        fake_non_start_node.repeat_contact_greetings.get("no_answer")
        == "Hi again {{first_name}}, sorry we missed you."
    )
    engine = PipecatEngine(
        workflow=workflow,
        call_context_vars={
            "first_name": "Jamie",
            "prior_contact_relationship_type": "no_answer",
        },
    )
    result = engine.get_node_greeting("start")
    assert result == ("text", "default greeting Jamie")
