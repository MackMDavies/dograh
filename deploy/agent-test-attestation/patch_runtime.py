"""Apply the attestation extension to a verified existing runtime image.

The production image contains other committed releases plus unreconciled legacy
fixes. Whole-file replacements would lose those fixes. This narrowly patches the
three attestation seams, after checking every dependency against its inspected
source hash. Run only while building a new image, never against a live checkout.
"""

import argparse
import ast
import hashlib
from pathlib import Path

EXPECTED_SOURCES = {
    "api/routes/workflow.py": "95cf30b3b4ae75efbe40ddee39a3acb649585435a94ab5231298486648965f3d",
    "api/routes/workflow_text_chat.py": "ca8d60edbf4c926cd8e70f717ffc7b7ed9c03052602cd1aeb519f45e8d90ad0d",
    "api/services/workflow/text_chat_runner.py": "095b9ca30a17e16ede7fff1ea0c9f178fa05d833e0aefb7f9b628be60f8953bb",
    "api/services/workflow/test_sandbox_mode.py": "b657623f66555739a35278d95cc2830760ffe09e8da5ee4c22739f704cc28fe2",
    "api/services/workflow/sandbox_repair.py": "3a2a226262c04d58f16be8144484e1b3a995ce56be7566362148fe2949145172",
    "api/services/workflow/agent_test_repair.py": "b83bac0ccbe5ae3346add2c2c9f7edb8f3ad600a3775955a5fc19468e5560b86",
}


def replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise ValueError("The inspected runtime patch seam changed")
    return source.replace(before, after, 1)


def patched_sources(root: Path) -> dict[str, str]:
    sources = {}
    for name, expected_hash in EXPECTED_SOURCES.items():
        source = (root / name).read_text()
        if hashlib.sha256(source.encode()).hexdigest() != expected_hash:
            raise ValueError(f"Runtime source differs from inspected base: {name}")
        sources[name] = source

    name = "api/routes/workflow_text_chat.py"
    source = replace_once(
        sources[name],
        "from api.services.quota_service import check_dograh_quota\n",
        "from api.services.quota_service import check_dograh_quota\n"
        "from api.services.workflow.test_sandbox_binding import (\n"
        "    make_sandbox_workflow_binding, validate_requested_sandbox_definition,\n"
        ")\n",
    )
    source = replace_once(
        source,
        "    candidate_draft_version_number: int | None = Field(default=None, ge=1)\n",
        "    candidate_draft_version_number: int | None = Field(default=None, ge=1)\n"
        '    expected_workflow_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")\n',
    )
    source = replace_once(
        source,
        "        definition_id = selected_definition.id\n",
        "        try:\n"
        "            validate_requested_sandbox_definition(selected_definition, workflow_id, request.expected_workflow_hash)\n"
        "        except ValueError as exc:\n"
        "            raise HTTPException(status_code=409, detail=str(exc)) from exc\n"
        "        definition_id = selected_definition.id\n",
    )
    source = replace_once(
        source,
        "    elif request.published_version_number is not None or request.candidate_draft_version_number is not None:\n",
        "    elif request.published_version_number is not None or request.candidate_draft_version_number is not None or request.expected_workflow_hash is not None:\n",
    )
    source = replace_once(
        source,
        '            "sysevo_internal_test",\n',
        '            "sysevo_internal_test",\n            "sysevo_test_sandbox",\n',
    )
    source = replace_once(
        source,
        "    workflow_run = await db_client.update_workflow_run(\n",
        "    if request.sandbox_mode:\n"
        '        annotations["sysevo_test_sandbox"]["workflow_binding"] = make_sandbox_workflow_binding(\n'
        "            selected_definition, workflow_id=workflow_id, workflow_run_id=workflow_run.id,\n"
        "            run_id=test_run_id, scenario_id=scenario_id, signing_secret=expected_secret,\n"
        "            expected_hash=request.expected_workflow_hash,\n"
        "        )\n"
        "    workflow_run = await db_client.update_workflow_run(\n",
    )
    patched = {name: source}

    name = "api/services/workflow/text_chat_runner.py"
    source = replace_once(
        sources[name], "import hashlib\n", "import hashlib\nimport os\n"
    )
    source = replace_once(
        source,
        "from api.enums import WorkflowRunMode, WorkflowRunState\n",
        "from api.enums import WorkflowRunMode, WorkflowRunState\n"
        "from api.services.workflow.test_sandbox_binding import validate_sandbox_execution_binding\n",
    )
    source = replace_once(
        source,
        "    run_definition = workflow_run.definition\n",
        "    run_definition = workflow_run.definition\n"
        '    validate_sandbox_execution_binding(workflow_run, run_definition, os.getenv("SYSEVO_MEMORY_SECRET", ""))\n',
    )
    patched[name] = source

    name = "api/routes/workflow.py"
    source = replace_once(
        sources[name],
        "    return capabilities\n",
        '    capabilities["exact_candidate_hash_pinning"] = required_ready\n    return capabilities\n',
    )
    patched[name] = source
    for source in patched.values():
        ast.parse(source)
    return patched


def apply_patch(root: Path) -> None:
    # Compute and validate every edit before touching any file.
    patches = patched_sources(root)
    for name, source in patches.items():
        (root / name).write_text(source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    apply_patch(args.root)
    print("Attestation installed in the verified candidate image source.")
