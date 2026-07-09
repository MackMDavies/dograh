from api.services.workflow.unresolved_variables import (
    find_unresolved_variables,
    build_unresolved_directive,
)


def test_finds_vars_with_no_value_across_texts():
    texts = ["Hi {{first_name}}, welcome to {{city}}.", "You're in {{city}} near {{landmark}}."]
    context = {"city": "Boston"}  # first_name + landmark unresolved; city resolved
    assert find_unresolved_variables(texts, context) == {"first_name", "landmark"}


def test_empty_string_value_counts_as_unresolved():
    assert find_unresolved_variables(["Hi {{first_name}}"], {"first_name": ""}) == {"first_name"}


def test_skips_fallback_dotted_and_system_vars():
    # {{x | Guest}} has a default; {{a.b}} is a nested runtime path; both skipped by
    # extract_template_variables, so neither is ever reported unresolved.
    texts = ["{{name | Guest}} {{gathered_context.city}} {{first_name}}"]
    assert find_unresolved_variables(texts, {}) == {"first_name"}


def test_all_resolved_returns_empty():
    assert find_unresolved_variables(["Hi {{first_name}}"], {"first_name": "Al"}) == set()


def test_build_directive_is_empty_when_no_names():
    assert build_unresolved_directive(set()) == ""


def test_build_directive_lists_names_sorted_and_marks_block():
    d = build_unresolved_directive({"city", "first_name"})
    assert "first_name, city" not in d  # sorted, not insertion order
    assert "city, first_name" in d
    assert "MISSING INFORMATION" in d
    assert "Never" in d and "ask the caller" in d
