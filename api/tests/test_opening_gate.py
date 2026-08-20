"""The greeting must not wait on a fetch that cannot change what it says.

A caller-memory / pre-call HTTP fetch is started when the pipeline starts, and
the opening used to block on it for up to 1.5s. On an answered call that is
dead air: the callee has said "hello" and the agent says nothing.

Memory lands through `fill_if_absent`, so it can only ever fill a variable that
is *currently empty*. If nothing the opening says is empty, the wait cannot
change a single spoken word.
"""

import pytest

from api.services.pipecat.opening_gate import (
    opening_depends_on_pre_call_fetch,
    opening_texts,
)
from api.services.workflow.dto import (
    AgentNodeData,
    EdgeDataDTO,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.workflow_graph import WorkflowGraph


def _workflow(*, greeting: str | None, prompt: str) -> WorkflowGraph:
    return WorkflowGraph(
        ReactFlowDTO(
            nodes=[
                RFNodeDTO(
                    id="start",
                    type="startCall",
                    position=Position(x=0, y=0),
                    data=StartCallNodeData(
                        name="Opener",
                        prompt=prompt,
                        greeting=greeting,
                        is_start=True,
                        extraction_enabled=False,
                    ),
                ),
                RFNodeDTO(
                    id="next",
                    type="agentNode",
                    position=Position(x=0, y=200),
                    data=AgentNodeData(
                        name="Next", prompt="Carry on.", extraction_enabled=False
                    ),
                ),
            ],
            edges=[
                RFEdgeDTO(
                    id="start-next",
                    source="start",
                    target="next",
                    data=EdgeDataDTO(label="Go", condition="Ready"),
                ),
            ],
        )
    )


CAMPAIGN_ROW = {"first_name": "Mack", "company": "TesterAI"}


class TestOpeningGate:
    def test_waits_when_the_greeting_would_speak_a_blank(self):
        wf = _workflow(
            greeting="Hi, is that {{first_name}} from {{company}}?",
            prompt="You are calling a lead.",
        )
        assert opening_depends_on_pre_call_fetch(wf, {}) is True

    def test_does_not_wait_when_the_campaign_row_already_filled_them(self):
        # The outbound case: the contact row supplied both, so fill_if_absent
        # would be a no-op and the wait is pure dead air on pickup.
        wf = _workflow(
            greeting="Hi, is that {{first_name}} from {{company}}?",
            prompt="You are calling a lead.",
        )
        assert opening_depends_on_pre_call_fetch(wf, CAMPAIGN_ROW) is False

    def test_does_not_wait_for_a_static_greeting(self):
        wf = _workflow(greeting="Hello, Sam here.", prompt="Be brief.")
        assert opening_depends_on_pre_call_fetch(wf, {}) is False

    def test_waits_when_only_the_start_prompt_needs_the_value(self):
        # With no greeting the opening is an LLM generation against this
        # prompt, so an empty variable there still reaches the caller.
        wf = _workflow(greeting=None, prompt="The caller is {{first_name}}.")
        assert opening_depends_on_pre_call_fetch(wf, {}) is True

    def test_a_fallback_default_is_not_a_reason_to_wait(self):
        wf = _workflow(
            greeting="Hi {{first_name | there}}, Sam here.", prompt="Be brief."
        )
        assert opening_depends_on_pre_call_fetch(wf, {}) is False

    def test_an_empty_string_counts_as_unfilled(self):
        wf = _workflow(greeting="Hi {{first_name}}.", prompt="Be brief.")
        assert opening_depends_on_pre_call_fetch(wf, {"first_name": ""}) is True

    def test_opening_texts_covers_greeting_and_prompt_only(self):
        wf = _workflow(greeting="Hi.", prompt="Be brief.")
        assert set(opening_texts(wf)) == {"Hi.", "Be brief."}
        # Not the downstream node — its prompt is composed at transition time,
        # by which point a background merge has long since landed.
        assert "Carry on." not in opening_texts(wf)


class TestMemoryOwnVariables:
    """The memory hook's outputs live in _SYSTEM_VARIABLES on purpose — so a
    template greeting by name doesn't block a campaign launch. They are also
    exactly what the fetch supplies, so the gate has to check them itself or it
    would greet with "Hi ," on the calls the fetch exists for."""

    def test_waits_for_caller_name_even_though_it_is_a_system_variable(self):
        wf = _workflow(greeting="Hi {{caller_name}}, calling you back.", prompt="Be brief.")
        assert opening_depends_on_pre_call_fetch(wf, {}) is True

    def test_does_not_wait_once_caller_name_is_present(self):
        wf = _workflow(greeting="Hi {{caller_name}}, calling you back.", prompt="Be brief.")
        assert opening_depends_on_pre_call_fetch(wf, {"caller_name": "Mack"}) is False

    def test_waits_for_caller_memory_in_the_start_prompt(self):
        wf = _workflow(greeting="Hello.", prompt="What we know: {{caller_memory}}")
        assert opening_depends_on_pre_call_fetch(wf, {}) is True

    def test_date_builtins_are_never_a_reason_to_wait(self):
        # Also system variables, but computed at render time — they do not come
        # from the fetch, so waiting for them would be waiting forever.
        wf = _workflow(
            greeting="Morning, it's {{current_date_spoken}}.",
            prompt="Today is {{current_date}} at {{time_now}}.",
        )
        assert opening_depends_on_pre_call_fetch(wf, {}) is False
