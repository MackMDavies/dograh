from api.services.workflow.variable_resolution import extract_variable_defaults, fill_if_absent


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
