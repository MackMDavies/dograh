from api.services.workflow.sandbox_repair import (
    CONCISENESS_GUIDANCE,
    GOAL_COMPLETION_GUIDANCE,
    append_conciseness_guidance,
    append_goal_completion_guidance,
    remove_sandbox_markers,
    workflow_snapshot_hash,
)


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
        "nodes": [
            {"id": "agent", "data": {"prompt": "Book a visit"}},
            {"id": "tool", "data": {}},
        ],
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


def test_append_conciseness_guidance_changes_only_prompted_nodes_and_is_idempotent():
    source = {
        "nodes": [
            {"id": "agent-1", "type": "agentNode", "data": {"name": "Booking", "prompt": "Collect the details."}},
            {"id": "tool-1", "type": "httpRequest", "data": {"name": "Create booking"}},
            {"id": "agent-2", "type": "endCall", "data": {"prompt": "Confirm next steps."}},
        ],
        "edges": [{"id": "edge-1", "source": "agent-1", "target": "tool-1"}],
    }
    candidate, paths = append_conciseness_guidance(source)

    assert paths == ["$.nodes[0].data.prompt", "$.nodes[2].data.prompt"]
    assert source["nodes"][0]["data"]["prompt"] == "Collect the details."
    assert candidate["nodes"][0]["data"]["prompt"].endswith(CONCISENESS_GUIDANCE)
    assert candidate["nodes"][1] == source["nodes"][1]
    unchanged, repeated_paths = append_conciseness_guidance(candidate)
    assert repeated_paths == []
    assert unchanged == candidate


def test_append_conciseness_guidance_fails_closed_without_json_nodes():
    assert append_conciseness_guidance({"graph": []}) == ({"graph": []}, [])


def test_goal_guidance_changes_only_prompted_nodes_and_is_idempotent():
    source = {
        "nodes": [
            {"data": {"prompt": "Help the caller."}},
            {"data": {"url": "https://client.example.com"}},
        ]
    }
    candidate, paths = append_goal_completion_guidance(source)
    assert paths == ["$.nodes[0].data.prompt"]
    assert candidate["nodes"][0]["data"]["prompt"].endswith(GOAL_COMPLETION_GUIDANCE)
    assert candidate["nodes"][1] == source["nodes"][1]
    assert source["nodes"][0]["data"]["prompt"] == "Help the caller."
    assert append_goal_completion_guidance(candidate) == (candidate, [])
