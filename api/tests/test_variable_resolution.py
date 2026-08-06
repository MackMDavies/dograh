from api.services.workflow.variable_resolution import (
    build_memory_attr_map,
    extract_static_overrides,
    extract_variable_defaults,
    fill_if_absent,
    remap_memory_variables,
    sanitize_context_variables,
)


def test_extract_reads_bare_string_defaults():
    assert extract_variable_defaults({"first_name": "Guest", "city": ""}) == {"first_name": "Guest"}


def test_extract_reads_object_shape_default_only():
    raw = {
        "email": {"default": "", "source": "memory", "memory_attr": "email"},
        "greeting_city": {"default": "your area", "source": "static"},
    }
    # empty default is skipped (leave the slot open for memory); non-empty default kept
    assert extract_variable_defaults(raw) == {"greeting_city": "your area"}


def test_extract_handles_none_and_junk():
    assert extract_variable_defaults(None) == {}
    assert extract_variable_defaults({"x": 123}) == {}  # non-str / non-dict value ignored


def test_fill_if_absent_fills_missing_and_empty_only():
    target = {"city": "Boston", "email": "", "name": "Al"}
    fill_if_absent(target, {"city": "London", "email": "a@b.com", "age": "40", "name": ""})
    # existing non-empty 'city'/'name' preserved; empty 'email' filled; new 'age' added
    assert target == {"city": "Boston", "email": "a@b.com", "name": "Al", "age": "40"}


def test_fill_if_absent_skips_empty_source_values():
    target = {}
    fill_if_absent(target, {"a": "", "b": None, "c": "x"})
    assert target == {"c": "x"}


# --- sanitize_context_variables -------------------------------------------------


def test_sanitize_collapses_descriptor_objects_to_their_default():
    """A canonical {default,source,memory_attr} object must never reach the
    renderer as a dict — it would be JSON-dumped and spoken aloud on the call."""
    raw = {
        "email": {"default": "", "source": "memory", "memory_attr": "email"},
        "city": {"default": "Boston", "source": "static"},
        "first_name": "Mack",
    }
    assert sanitize_context_variables(raw) == {
        "email": "",
        "city": "Boston",
        "first_name": "Mack",
    }


def test_sanitize_preserves_genuine_nested_data():
    """Only variable descriptors collapse; real nested context must survive so
    dotted lookups like {{gathered_context.city}} keep working."""
    raw = {"gathered_context": {"city": "Leeds", "score": 3}, "name": "Al"}
    assert sanitize_context_variables(raw) == raw


def test_sanitize_handles_none_and_non_dict():
    assert sanitize_context_variables(None) == {}
    assert sanitize_context_variables("nope") == {}


# --- extract_static_overrides ---------------------------------------------------


def test_static_source_variables_are_returned_as_overrides():
    raw = {
        "brand": {"default": "Sysevo", "source": "static"},
        "first_name": {"default": "Guest", "source": "campaign"},
        "blank": {"default": "", "source": "static"},
    }
    # only non-empty static defaults; campaign-sourced vars are not overrides
    assert extract_static_overrides(raw) == {"brand": "Sysevo"}


def test_bare_string_variables_are_not_static_overrides():
    # bare strings are 'auto' source — they must stay overridable by campaign data
    assert extract_static_overrides({"first_name": "Guest"}) == {}


# --- memory attribute mapping ---------------------------------------------------


def test_build_memory_attr_map_maps_memory_attr_to_variable_name():
    raw = {
        "email": {"default": "", "source": "memory", "memory_attr": "best_email"},
        "city": {"default": "", "source": "campaign"},
        "name": "Al",
    }
    assert build_memory_attr_map(raw) == {"best_email": "email"}


def test_remap_memory_variables_renames_payload_keys_to_variable_names():
    fetch_result = {"best_email": "a@b.com", "caller_name": "Al"}
    remapped = remap_memory_variables(fetch_result, {"best_email": "email"})
    # renamed to the variable name, and unmapped keys still pass through
    assert remapped == {"email": "a@b.com", "caller_name": "Al"}


def test_remap_memory_variables_without_map_is_a_passthrough():
    fetch_result = {"caller_name": "Al"}
    assert remap_memory_variables(fetch_result, {}) == {"caller_name": "Al"}
