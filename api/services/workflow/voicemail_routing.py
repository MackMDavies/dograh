"""Find the node a workflow uses to talk to an answering machine.

A workflow that has bothered to write a voicemail script models it the same
way every time: an edge out of the opener (or the gatekeeper) whose label or
condition says "voicemail", pointing at the node that leaves the message.

Nothing in the DTO marks that node, so it is resolved from the graph's own
wiring rather than from a flag someone has to remember to set. A workflow with
no such edge resolves to None and keeps the old behaviour — hang up the moment
a machine is detected.
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from api.services.workflow.workflow_graph import WorkflowGraph

# "answerphone" and "answering machine" are how the same edge gets written when
# somebody avoids repeating the word; "machine" alone is too loose — it matches
# "machine learning" in a condition about the product.
_VOICEMAIL_WORDS = ("voicemail", "voice mail", "answerphone", "answering machine")


def _mentions_voicemail(text: Optional[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(word in lowered for word in _VOICEMAIL_WORDS)


def find_voicemail_node_id(workflow: "WorkflowGraph") -> Optional[str]:
    """Return the node id a voicemail should be routed to, or None.

    Prefers an edge leaving the start node — that is where "the call was
    answered by a machine" is actually decided — and falls back to any edge in
    the graph, so a workflow that only wires voicemail off its gatekeeper still
    resolves. An end node is never a valid target: routing there would end the
    call without saying anything, which is the behaviour this exists to fix.
    """
    start_id = getattr(workflow, "start_node_id", None)

    def _target_of(edges) -> Optional[str]:
        for edge in edges:
            if not (_mentions_voicemail(edge.label) or _mentions_voicemail(edge.condition)):
                continue
            target = workflow.nodes.get(edge.target)
            if target is None or getattr(target, "is_end", False):
                continue
            return edge.target
        return None

    from_start = [e for e in workflow.edges if e.source == start_id]
    return _target_of(from_start) or _target_of(list(workflow.edges))
