"""Detect template variables that have no value at call time and build a
system-prompt directive telling the model to reword / ask instead of speaking
a blank. Engine-level auto-reword (WS4).
"""
from typing import Iterable

from api.services.workflow.workflow_graph import extract_template_variables


def find_unresolved_variables(texts: Iterable[str], context: dict) -> set[str]:
    """Return the set of bare template variables referenced in `texts` that have
    no non-empty value in `context`.

    extract_template_variables already excludes nested/dotted paths, variables
    with a `| fallback` default, and system-injected variables — so the result
    is exactly the campaign/memory/default-sourced variables that came up empty.
    """
    referenced: set[str] = set()
    for text in texts:
        if text:
            referenced |= extract_template_variables(text)

    unresolved: set[str] = set()
    for name in referenced:
        value = context.get(name)
        if value is None or value == "":
            unresolved.add(name)
    return unresolved


def build_unresolved_directive(names: set[str]) -> str:
    """Build the system-prompt directive block for the given unresolved names.
    Returns "" when there is nothing unresolved.
    """
    if not names:
        return ""
    listed = ", ".join(sorted(names))
    return (
        "[MISSING INFORMATION — DO NOT READ ALOUD]\n"
        f"You do not have a value for: {listed}. "
        "Never say a placeholder, a blank, or an empty space where one of these "
        "would go. Reword what you say so it reads naturally without it. If one "
        "is genuinely needed to proceed, ask the caller for it politely in your "
        "own words.\n"
        "[END MISSING INFORMATION]"
    )
