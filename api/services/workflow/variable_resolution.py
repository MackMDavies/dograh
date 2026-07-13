"""Runtime resolution helpers for template context variables.

Reads the canonical (WS1) template_context_variables shape — each value is
either a bare default string or an object {default?, source?, memory_attr?} —
and provides fill-if-absent merging used to layer campaign, default, and
memory values at the correct precedence.
"""
from typing import Any


def extract_variable_defaults(raw: Any) -> dict[str, str]:
    """Return {name: default} for every variable with a NON-EMPTY default.

    A bare string value IS the default. An object value's default is its
    "default" field. Empty defaults are skipped so the slot stays open for a
    lower-precedence source (e.g. caller memory) to fill.
    """
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        if isinstance(value, str):
            default = value
        elif isinstance(value, dict):
            default = value.get("default", "")
        else:
            continue
        if isinstance(default, str) and default != "":
            out[name] = default
    return out


def fill_if_absent(target: dict, source: dict) -> None:
    """Mutate `target`: copy each key from `source` only when `target` lacks a
    non-empty value for it AND the source value is itself non-empty.
    """
    for key, value in source.items():
        if value in (None, ""):
            continue
        if key not in target or target[key] in (None, ""):
            target[key] = value
