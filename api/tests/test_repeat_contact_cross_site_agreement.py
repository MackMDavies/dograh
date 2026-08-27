"""compose_system_prompt_for_node (system prompt) and
PipecatEngine.get_node_greeting (spoken greeting) independently resolve
repeat-contact variants via the same pure resolve_repeat_contact_text().
This test proves — for one identical workflow/engine/bucket — that both
call sites actually agree, rather than relying on code inspection alone to
rule out future divergence between the two."""

from api.services.workflow.dto import (
    Position,
    ReactFlowDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_context_composer import (
    compose_system_prompt_for_node,
)
from api.services.workflow.workflow_graph import WorkflowGraph


def _identity_format_prompt(text: str) -> str:
    return text or ""


def test_prompt_composition_and_greeting_resolve_the_same_variant():
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
                    add_global_prompt=False,
                    repeat_contact_variants_enabled=True,
                    repeat_contact_prompt_no_answer="Acknowledge we tried before.",
                    repeat_contact_greeting_no_answer="Hi again {{first_name}}, sorry we missed you.",
                ),
            ),
        ],
        edges=[],
    )
    workflow = WorkflowGraph(dto)
    call_context_vars = {
        "first_name": "Jamie",
        "prior_contact_relationship_type": "no_answer",
    }

    composed_prompt = compose_system_prompt_for_node(
        node=workflow.nodes["start"],
        workflow=workflow,
        format_prompt=_identity_format_prompt,
        has_recordings=False,
        call_context_vars=call_context_vars,
    )
    assert composed_prompt == "Acknowledge we tried before."

    engine = PipecatEngine(workflow=workflow, call_context_vars=call_context_vars)
    greeting = engine.get_node_greeting("start")
    assert greeting == ("text", "Hi again Jamie, sorry we missed you.")
