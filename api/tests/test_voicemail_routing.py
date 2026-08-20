"""A detected answering machine should hear the workflow's voicemail script.

Before this, a machine got a hang-up the instant it was classified — so a
workflow could carry a fully wired voicemail node and never use it once. On a
real list of decision-makers' direct lines, machines outnumbered humans nearly
four to one and every one of them got silence.

Nothing in the DTO marks the voicemail node, so it is resolved from the
graph's own wiring. These tests pin that resolution, including the cases where
it must refuse to resolve.
"""

import pytest

from api.services.workflow.dto import (
    AgentNodeData,
    EdgeDataDTO,
    EndCallNodeData,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.voicemail_routing import find_voicemail_node_id
from api.services.workflow.workflow_graph import WorkflowGraph


def _wf(edges) -> WorkflowGraph:
    return WorkflowGraph(
        ReactFlowDTO(
            nodes=[
                RFNodeDTO(
                    id="start", type="startCall", position=Position(x=0, y=0),
                    data=StartCallNodeData(
                        name="Opener", prompt="Say hello.", is_start=True,
                        extraction_enabled=False,
                    ),
                ),
                RFNodeDTO(
                    id="gate", type="agentNode", position=Position(x=0, y=100),
                    data=AgentNodeData(name="Gatekeeper", prompt="Ask.", extraction_enabled=False),
                ),
                RFNodeDTO(
                    id="vm", type="agentNode", position=Position(x=0, y=200),
                    data=AgentNodeData(name="Voicemail", prompt="Leave a message.", extraction_enabled=False),
                ),
                RFNodeDTO(
                    id="end", type="endCall", position=Position(x=0, y=300),
                    data=EndCallNodeData(name="End Call", prompt="Bye.", is_end=True, extraction_enabled=False),
                ),
            ],
            edges=edges,
        )
    )


def _edge(id_, source, target, label, condition="x"):
    return RFEdgeDTO(id=id_, source=source, target=target,
                     data=EdgeDataDTO(label=label, condition=condition))


# The voicemail node needs *an* inbound edge to satisfy graph validation; the
# negative cases below rely on it being one that says nothing about voicemail.
BASE = [
    _edge("s-g", "start", "gate", "Gatekeeper"),
    _edge("g-vm-plain", "gate", "vm", "Next"),
    _edge("vm-end", "vm", "end", "Done"),
]


class TestResolution:
    def test_finds_the_node_behind_a_voicemail_edge(self):
        wf = _wf([*BASE, _edge("s-vm", "start", "vm", "Voicemail")])
        assert find_voicemail_node_id(wf) == "vm"

    def test_matches_on_the_condition_when_the_label_does_not_say_it(self):
        wf = _wf([*BASE, _edge("s-vm", "start", "vm", "Machine", "went to voicemail / answerphone")])
        assert find_voicemail_node_id(wf) == "vm"

    @pytest.mark.parametrize("label", ["Voice Mail", "Answerphone", "Answering machine"])
    def test_accepts_the_usual_spellings(self, label):
        wf = _wf([*BASE, _edge("s-vm", "start", "vm", label)])
        assert find_voicemail_node_id(wf) == "vm"

    def test_falls_back_to_a_voicemail_edge_off_another_node(self):
        # Some workflows only wire voicemail off the gatekeeper.
        wf = _wf([*BASE, _edge("g-vm", "gate", "vm", "Voicemail")])
        assert find_voicemail_node_id(wf) == "vm"

    def test_prefers_the_edge_leaving_the_start_node(self):
        wf = _wf([
            *BASE,
            _edge("g-gate", "gate", "gate", "Voicemail"),
            _edge("s-vm", "start", "vm", "Voicemail"),
        ])
        assert find_voicemail_node_id(wf) == "vm"


class TestRefusesToResolve:
    def test_no_voicemail_edge_means_no_node(self):
        # Must fall back to the old hang-up behaviour, not guess.
        assert find_voicemail_node_id(_wf(BASE)) is None

    def test_an_end_node_is_never_the_target(self):
        # Routing to End Call would hang up without saying anything — exactly
        # the behaviour this exists to replace.
        wf = _wf([*BASE, _edge("s-end", "start", "end", "Voicemail")])
        assert find_voicemail_node_id(wf) is None

    def test_does_not_match_the_word_machine_on_its_own(self):
        # "machine learning" in a product condition is not a voicemail edge.
        wf = _wf([*BASE, _edge("s-ml", "start", "vm", "ML", "they ask about machine learning")])
        assert find_voicemail_node_id(wf) is None
