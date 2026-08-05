"""Runtime resolution helpers for template context variables.

Reads the canonical (WS1) template_context_variables shape — each value is
either a bare default string or an object {default?, source?, memory_attr?} —
and provides fill-if-absent merging used to layer campaign, default, and
memory values at the correct precedence.
"""
from typing import Any

# Keys that identify a canonical (WS1) variable descriptor object. A dict whose
# keys are a subset of these is configuration describing a variable, NOT call
# data — it must be collapsed to its default before reaching the renderer.
_DESCRIPTOR_KEYS = {"default", "source", "memory_attr", "type"}


def is_variable_descriptor(value: Any) -> bool:
    """True when *value* is a canonical variable descriptor object.

    Deliberately strict: a dict only counts when every key belongs to the
    descriptor schema, so genuine nested call data (e.g. ``gathered_context``)
    is never mistaken for configuration.
    """
    return (
        isinstance(value, dict)
        and bool(value)
        and set(value.keys()) <= _DESCRIPTOR_KEYS
    )


def sanitize_context_variables(raw: Any) -> dict[str, Any]:
    """Collapse any variable descriptor objects to their default string.

    Guards the renderer: an un-collapsed descriptor is a dict, and the renderer
    JSON-dumps dicts — which means the agent would read
    ``{"default": "", "source": "memory"}`` aloud on a live call.

    Non-descriptor values (plain strings, genuine nested dicts) pass through
    untouched so dotted lookups keep working.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if is_variable_descriptor(value):
            default = value.get("default", "")
            out[name] = default if isinstance(default, str) else ""
        else:
            out[name] = value
    return out


def extract_static_overrides(raw: Any) -> dict[str, str]:
    """Return {name: default} for variables explicitly pinned as ``static``.

    The editor promises a static variable "always uses the fixed default value
    you set here", so these are layered ON TOP of campaign data rather than
    underneath it. Bare-string (``auto``) variables are excluded — they must
    remain overridable by the campaign list.
    """
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        if value.get("source") != "static":
            continue
        default = value.get("default", "")
        if isinstance(default, str) and default != "":
            out[name] = default
    return out


def build_memory_attr_map(raw: Any) -> dict[str, str]:
    """Return {memory_attr: variable_name} for memory-sourced variables.

    The caller-memory hook returns values keyed by its own attribute names; this
    map lets us land them on the variable the operator actually bound them to.
    """
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        if value.get("source") != "memory":
            continue
        attr = value.get("memory_attr")
        if isinstance(attr, str) and attr and attr != name:
            out[attr] = name
    return out


def remap_memory_variables(fetch_result: dict, attr_map: dict[str, str]) -> dict:
    """Rename memory-hook payload keys onto their bound variable names.

    Unmapped keys pass through unchanged so built-ins like ``caller_name`` and
    ``caller_memory`` keep working.
    """
    if not attr_map:
        return dict(fetch_result)
    out: dict = {}
    for key, value in fetch_result.items():
        out[attr_map.get(key, key)] = value
    return out


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


def pick_template_variables(*, definition_vars: Any, workflow_vars: Any) -> dict:
    """Choose which template-variable config a call should use.

    The agent editor persists template variables on the workflow DEFINITION
    (draft or published), so the run's pinned definition is authoritative. The
    workflow row is only a legacy fallback for rows written before versioning.
    """
    if isinstance(definition_vars, dict) and definition_vars:
        return definition_vars
    if isinstance(workflow_vars, dict) and workflow_vars:
        return workflow_vars
    return {}


def build_call_context(
    *,
    initial_context: Any,
    extra_context_vars: dict | None,
    template_context_variables: Any,
) -> dict[str, Any]:
    """Assemble the template variable context for a call.

    Precedence, highest first:
      1. variables pinned ``static`` in the agent config
      2. transport extras (telephony/runtime values passed into the pipeline)
      3. ``initial_context`` — the campaign contact row for outbound calls, or
         the seeded variable defaults for a test call
      4. non-empty workflow defaults

    Caller memory is layered afterwards at call start via :func:`fill_if_absent`,
    so it only fills slots still empty here.
    """
    merged: dict[str, Any] = sanitize_context_variables(initial_context)

    if extra_context_vars:
        merged = {**merged, **extra_context_vars}

    defaults = extract_variable_defaults(template_context_variables)
    if defaults:
        merged = {**defaults, **merged}

    statics = extract_static_overrides(template_context_variables)
    if statics:
        merged = {**merged, **statics}

    return merged


def fill_if_absent(target: dict, source: dict) -> None:
    """Mutate `target`: copy each key from `source` only when `target` lacks a
    non-empty value for it AND the source value is itself non-empty.
    """
    for key, value in source.items():
        if value in (None, ""):
            continue
        if key not in target or target[key] in (None, ""):
            target[key] = value
