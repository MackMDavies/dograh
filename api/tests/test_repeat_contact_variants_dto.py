"""StartCallNodeData must accept the new repeat-contact fields, default them
safely, and expose them through the auto-generated NodeSpec so the workflow
builder UI can render them."""

from api.services.workflow.dto import StartCallNodeData
from api.services.workflow.node_specs import all_specs


def test_start_call_node_data_defaults_repeat_contact_fields_safely():
    data = StartCallNodeData(name="Start", prompt="Hi there")
    assert data.repeat_contact_variants_enabled is False
    assert data.repeat_contact_greeting_spoke_directly is None
    assert data.repeat_contact_greeting_gatekeeper_screened is None
    assert data.repeat_contact_greeting_no_answer is None
    assert data.repeat_contact_prompt_spoke_directly is None
    assert data.repeat_contact_prompt_gatekeeper_screened is None
    assert data.repeat_contact_prompt_no_answer is None


def test_start_call_node_data_accepts_repeat_contact_fields():
    data = StartCallNodeData(
        name="Start",
        prompt="Hi there",
        repeat_contact_variants_enabled=True,
        repeat_contact_greeting_gatekeeper_screened="Hi again, is the owner in today?",
        repeat_contact_prompt_no_answer="Open by acknowledging we tried before.",
    )
    assert data.repeat_contact_variants_enabled is True
    assert (
        data.repeat_contact_greeting_gatekeeper_screened
        == "Hi again, is the owner in today?"
    )
    assert (
        data.repeat_contact_prompt_no_answer
        == "Open by acknowledging we tried before."
    )


def test_start_call_node_spec_exposes_repeat_contact_properties():
    spec = next(s for s in all_specs() if s.name == "startCall")
    prop_names = {p.name for p in spec.properties}
    assert "repeat_contact_variants_enabled" in prop_names
    assert "repeat_contact_greeting_spoke_directly" in prop_names
    assert "repeat_contact_greeting_gatekeeper_screened" in prop_names
    assert "repeat_contact_greeting_no_answer" in prop_names
    assert "repeat_contact_prompt_spoke_directly" in prop_names
    assert "repeat_contact_prompt_gatekeeper_screened" in prop_names
    assert "repeat_contact_prompt_no_answer" in prop_names
