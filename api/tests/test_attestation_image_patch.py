"""Fail closed when the reviewed production-image baseline changes."""

import hashlib
import importlib.util
from pathlib import Path

import pytest


def release_patch():
    path = (
        Path(__file__).resolve().parents[2]
        / "deploy/agent-test-attestation/patch_runtime.py"
    )
    spec = importlib.util.spec_from_file_location("attestation_release_patch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_patch_seam_is_replaced_once():
    assert release_patch().replace_once("before", "before", "after") == "after"


@pytest.mark.parametrize("source", ["missing", "before before"])
def test_ambiguous_or_missing_seams_are_rejected(source):
    with pytest.raises(ValueError, match="seam changed"):
        release_patch().replace_once(source, "before", "after")


def test_a_changed_dependency_is_rejected_before_any_patch_write(tmp_path):
    module = release_patch()
    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_text("original first")
    second.write_text("unexpected dependency")
    module.EXPECTED_SOURCES = {
        "first.py": hashlib.sha256(b"original first").hexdigest(),
        "second.py": hashlib.sha256(b"expected dependency").hexdigest(),
    }
    with pytest.raises(ValueError, match="differs from inspected base"):
        module.apply_patch(tmp_path)
    assert first.read_text() == "original first"
    assert second.read_text() == "unexpected dependency"
