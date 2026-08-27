"""get_required_template_variables() must scan repeat-contact opening
variants too, not just the default prompt/greeting — otherwise a variable
used only inside a variant skips campaign pre-launch CSV-column validation
and only surfaces mid-campaign, on whichever calls hit that bucket."""

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


def test_variables_referenced_only_in_a_variant_are_required():
    workflow = _single_start_node_workflow(
        repeat_contact_variants_enabled=True,
        repeat_contact_greeting_gatekeeper_screened="Hi {{first_name}}, sorry to bother the office again.",
        repeat_contact_prompt_no_answer="Mention the {{appointment_type}} before opening.",
    )
    variables = workflow.get_required_template_variables()
    assert "first_name" in variables
    assert "appointment_type" in variables


def test_disabled_feature_contributes_no_variables_even_with_leftover_text():
    workflow = _single_start_node_workflow(
        repeat_contact_variants_enabled=False,
        repeat_contact_greeting_gatekeeper_screened="Hi {{stale_variable}}, ignored while disabled.",
    )
    variables = workflow.get_required_template_variables()
    assert "stale_variable" not in variables
