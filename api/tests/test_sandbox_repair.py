from api.services.workflow.sandbox_repair import remove_sandbox_markers, workflow_snapshot_hash


def test_remove_known_sandbox_markers_and_preserve_client_values():
    source = {
        "nodes": [
            {"id": "agent", "data": {"prompt": "Book a visit", "sysevo_test_mode": True}},
            {"id": "tool", "data": {"url": "https://qa.example.test/agent-test-sandbox-execute"}},
        ],
        "enabled": True,
    }

    cleaned, paths = remove_sandbox_markers(source)

    assert cleaned == {
        "nodes": [{"id": "agent", "data": {"prompt": "Book a visit"}}],
        "enabled": True,
    }
    assert paths == ["$.nodes[0].data.sysevo_test_mode", "$.nodes[1].data.url"]


def test_marker_cleanup_is_idempotent():
    cleaned, first_paths = remove_sandbox_markers({"node": {"agent_test_run_id": "run-id"}})
    unchanged, second_paths = remove_sandbox_markers(cleaned)

    assert first_paths == ["$.node.agent_test_run_id"]
    assert unchanged == cleaned
    assert second_paths == []


def test_workflow_hash_covers_graph_and_all_configuration_snapshots():
    graph = {"nodes": [{"id": "a"}]}
    original = workflow_snapshot_hash(graph, {"notifications": False}, {"company": "Acme"})

    assert original == workflow_snapshot_hash(graph, {"notifications": False}, {"company": "Acme"})
    assert original != workflow_snapshot_hash(graph, {"notifications": True}, {"company": "Acme"})
    assert original != workflow_snapshot_hash(graph, {"notifications": False}, {"company": "Other"})
