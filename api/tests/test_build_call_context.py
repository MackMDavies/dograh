"""Precedence rules for assembling a call's template variable context.

Order, highest wins:
  static-pinned variables > transport extras > campaign contact row >
  workflow defaults  (caller memory fills remaining gaps later, at call start)
"""

from api.services.workflow.variable_resolution import build_call_context


def test_campaign_contact_row_beats_workflow_default():
    ctx = build_call_context(
        initial_context={"first_name": "Katie"},
        extra_context_vars=None,
        template_context_variables={"first_name": "Guest"},
    )
    assert ctx["first_name"] == "Katie"


def test_workflow_default_fills_variable_absent_from_campaign_row():
    ctx = build_call_context(
        initial_context={"first_name": "Katie"},
        extra_context_vars=None,
        template_context_variables={"first_name": "Guest", "company": "your business"},
    )
    assert ctx["company"] == "your business"


def test_transport_extras_beat_campaign_row():
    ctx = build_call_context(
        initial_context={"caller_number": "+1000"},
        extra_context_vars={"caller_number": "+1999"},
        template_context_variables={},
    )
    assert ctx["caller_number"] == "+1999"


def test_static_variable_is_not_overridden_by_campaign_row():
    """The editor promises a static variable always uses its fixed value, so a
    same-named CSV column must not silently replace it."""
    ctx = build_call_context(
        initial_context={"brand": "WrongBrand"},
        extra_context_vars=None,
        template_context_variables={"brand": {"default": "Sysevo", "source": "static"}},
    )
    assert ctx["brand"] == "Sysevo"


def test_descriptor_objects_never_survive_into_the_context():
    """A test call seeds initial_context from the raw variable config; those
    descriptor objects must be collapsed or the agent reads JSON aloud."""
    raw = {"email": {"default": "", "source": "memory", "memory_attr": "email"}}
    ctx = build_call_context(
        initial_context=raw,
        extra_context_vars=None,
        template_context_variables=raw,
    )
    assert ctx["email"] == ""


def test_memory_sourced_variable_leaves_its_slot_open_for_memory():
    ctx = build_call_context(
        initial_context={},
        extra_context_vars=None,
        template_context_variables={
            "email": {"default": "", "source": "memory", "memory_attr": "best_email"}
        },
    )
    # empty, not missing — memory fills it at call start via fill_if_absent
    assert ctx.get("email", "") == ""


def test_handles_none_initial_context():
    assert build_call_context(
        initial_context=None,
        extra_context_vars=None,
        template_context_variables={"a": "1"},
    ) == {"a": "1"}
