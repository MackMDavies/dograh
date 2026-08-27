from types import SimpleNamespace

from api.services.workflow.pipecat_engine_context_composer import (
    compose_system_prompt_for_node,
)


def _node(**kw):
    kw.setdefault("add_global_prompt", False)
    kw.setdefault("prompt", "")
    kw.setdefault("greeting", None)
    kw.setdefault("out_edges", [])
    kw.setdefault("is_start", False)
    return SimpleNamespace(**kw)


_WF = SimpleNamespace(global_node_id=None, nodes={})


def test_directive_injected_for_unresolved_var():
    node = _node(prompt="Hi {{first_name}}, welcome to {{city}}.")
    out = compose_system_prompt_for_node(
        node=node, workflow=_WF, format_prompt=lambda s: s,
        has_recordings=False, call_context_vars={"city": "Boston"},
    )
    assert "MISSING INFORMATION" in out
    tail = out.split("MISSING INFORMATION")[1]
    assert "first_name" in tail   # unresolved -> listed
    assert "city" not in tail     # resolved -> not listed


def test_directive_covers_greeting_and_edge_speech():
    edge = SimpleNamespace(transition_speech="Transferring you now, {{agent_name}}.")
    node = _node(prompt="ok", greeting="Hello {{first_name}}", out_edges=[edge])
    out = compose_system_prompt_for_node(
        node=node, workflow=_WF, format_prompt=lambda s: s,
        has_recordings=False, call_context_vars={},
    )
    tail = out.split("MISSING INFORMATION")[1]
    assert "first_name" in tail and "agent_name" in tail


def test_no_directive_when_all_resolved():
    node = _node(prompt="Hi {{first_name}}")
    out = compose_system_prompt_for_node(
        node=node, workflow=_WF, format_prompt=lambda s: s,
        has_recordings=False, call_context_vars={"first_name": "Al"},
    )
    assert "MISSING INFORMATION" not in out


def test_no_directive_when_context_vars_omitted():
    # backward compatibility: callers that don't pass call_context_vars are unchanged
    node = _node(prompt="Hi {{first_name}}")
    out = compose_system_prompt_for_node(
        node=node, workflow=_WF, format_prompt=lambda s: s, has_recordings=False,
    )
    assert "MISSING INFORMATION" not in out
