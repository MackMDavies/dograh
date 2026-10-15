"""run_pipeline must call fire_call_live with arguments it accepts.

It once passed workflow_id and caller_number to a function that took neither. The
call raised TypeError synchronously (before create_task), so a deployment with
SYSEVO_CALL_LIVE_URL set would fail every call at pipeline start. Checked from the
source so this runs without pipecat installed.
"""
import ast
from pathlib import Path

API = Path(__file__).resolve().parents[1]


def _params(path: Path, name: str) -> set[str]:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return {a.arg for a in node.args.args + node.args.kwonlyargs}
    raise AssertionError(f"{name} not found in {path}")


def test_every_fire_call_live_call_uses_accepted_keywords():
    accepted = _params(API / "services" / "call_live_webhook.py", "fire_call_live")
    calls = 0
    for path in API.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "fire_call_live":
                calls += 1
                used = {k.arg for k in node.keywords if k.arg}
                assert used <= accepted, f"{path.name}: {sorted(used - accepted)} not accepted"
    assert calls >= 2  # started (run_pipeline) and ended (post-call)
