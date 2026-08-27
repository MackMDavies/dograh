"""Node must expose the repeat-contact fields from StartCallNodeData, and
its repeat_contact_greetings/repeat_contact_prompts properties must return
{} whenever the feature is disabled for that node — the runtime resolver
must never see stale variant text left over from before an author toggled
the feature off."""

from api.services.workflow.dto import (
    Position,
    ReactFlowDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.workflow_graph import WorkflowGraph


def _single_start_node_workflow(**start_call_kwargs) -> WorkflowGraph:
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start",
                    prompt="default prompt",
                    is_start=True,
                    **start_call_kwargs,
                ),
            ),
        ],
        edges=[],
    )
    return WorkflowGraph(dto)


def test_node_exposes_empty_variant_dicts_when_disabled():
    workflow = _single_start_node_workflow(
        repeat_contact_variants_enabled=False,
        repeat_contact_greeting_no_answer="should be ignored while disabled",
    )
    node = workflow.nodes["start"]
    assert node.repeat_contact_greetings == {}
    assert node.repeat_contact_prompts == {}


def test_node_exposes_variant_dicts_when_enabled():
    workflow = _single_start_node_workflow(
        repeat_contact_variants_enabled=True,
        repeat_contact_greeting_spoke_directly="greeting a",
        repeat_contact_greeting_gatekeeper_screened="greeting b",
        repeat_contact_greeting_no_answer="greeting c",
        repeat_contact_prompt_spoke_directly="prompt a",
        repeat_contact_prompt_gatekeeper_screened="prompt b",
        repeat_contact_prompt_no_answer="prompt c",
    )
    node = workflow.nodes["start"]
    assert node.repeat_contact_greetings == {
        "spoke_directly": "greeting a",
        "gatekeeper_screened": "greeting b",
        "no_answer": "greeting c",
    }
    assert node.repeat_contact_prompts == {
        "spoke_directly": "prompt a",
        "gatekeeper_screened": "prompt b",
        "no_answer": "prompt c",
    }
