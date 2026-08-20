"""Decides whether the greeting has to wait on an in-flight pre-call fetch.

A caller-memory (or generic HTTP) pre-call fetch is kicked off when the
pipeline starts, and the greeting used to block on it for up to
``_PRE_CALL_GREETING_WAIT_S``. On pickup that wait is dead air — the callee has
just said "hello" and the agent says nothing.

The wait only buys anything when the opening actually *says* something the
fetch could still fill in. Memory lands via ``fill_if_absent``, so a variable
that already has a value cannot be changed by it; and a variable the opening
never references cannot change what is spoken. When neither applies, the
greeting can start immediately and the fetch can land in the background.
"""

from typing import TYPE_CHECKING

from api.services.workflow.unresolved_variables import find_unresolved_variables
from api.services.workflow.workflow_graph import (
    PRE_CALL_FETCH_VARIABLES,
    extract_template_variables,
)

if TYPE_CHECKING:
    from api.services.workflow.workflow_graph import WorkflowGraph


def opening_texts(workflow: "WorkflowGraph") -> list[str]:
    """The raw templates that decide what the agent says when the call opens."""
    start = workflow.nodes.get(workflow.start_node_id)
    if start is None:
        return []
    # The greeting is spoken immediately; the prompt drives the opening
    # generation when there is no greeting, and stays the system prompt for
    # the whole of the start node either way.
    return [t for t in (start.greeting, start.prompt) if t]


def opening_depends_on_pre_call_fetch(
    workflow: "WorkflowGraph", call_context_vars: dict
) -> bool:
    """True when the opening references a variable that is still empty.

    Two passes, because they cover different sets of names.

    ``find_unresolved_variables`` handles the ordinary ones: it already
    discounts variables carrying a ``| fallback`` default, dotted runtime
    paths, and system-injected names, so what it returns is the campaign /
    default-sourced values that came up blank.

    But the memory hook's own outputs — ``caller_name``, ``caller_memory`` and
    friends — are *in* that system-injected set, deliberately, so that a
    template greeting by name doesn't block a campaign launch. Those are
    precisely what this fetch supplies, so skipping them here would let the
    agent open with "Hi ," on the very calls the fetch exists to serve. Check
    them explicitly instead.
    """
    texts = opening_texts(workflow)

    if find_unresolved_variables(texts, call_context_vars):
        return True

    referenced: set[str] = set()
    for text in texts:
        referenced |= extract_template_variables(text, include_system=True)

    for name in referenced & PRE_CALL_FETCH_VARIABLES:
        value = call_context_vars.get(name)
        if value is None or value == "":
            return True

    return False
