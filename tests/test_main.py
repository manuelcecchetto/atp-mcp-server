import inspect
import json
from pathlib import Path
import sys

import pytest
from jsonschema.exceptions import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def make_graph(nodes, version="1.4"):
    return {
        "meta": {
            "project_name": "ATP Test Plan",
            "version": version,
            "project_status": "ACTIVE",
        },
        "nodes": nodes,
    }


def write_plan(tmp_path: Path, graph: dict) -> Path:
    plan_path = tmp_path / "plan.atp.json"
    plan_path.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    return plan_path


def read_plan(plan_path: Path) -> dict:
    return json.loads(plan_path.read_text(encoding="utf-8"))


def read_audit(plan_path: Path) -> list[dict]:
    audit_path = Path(f"{plan_path}.audit.jsonl")
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]


def supports_agent_id_tools() -> bool:
    return "agent_id" in inspect.signature(main.atp_complete_task).parameters


def require_agent_id_tools() -> None:
    if not supports_agent_id_tools():
        raise AssertionError("ATP Safety Core Hardening runtime not merged yet.")


def call_complete_task(plan_path: Path, node_id: str, agent_id: str, report: str, artifacts=None, status="DONE"):
    require_agent_id_tools()
    return main.atp_complete_task(str(plan_path), node_id, agent_id, report, artifacts or [], status)


def call_reset_task(plan_path: Path, node_id: str, agent_id: str, reason: str):
    require_agent_id_tools()
    return main.atp_reset_task(str(plan_path), node_id, agent_id, reason)


def call_cancel_task(plan_path: Path, node_id: str, agent_id: str, reason: str):
    require_agent_id_tools()
    return main.atp_cancel_task(str(plan_path), node_id, agent_id, reason)


def test_claim_recovers_zombie_and_appends_audit_log(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "stale": {
                "title": "Recover me",
                "instruction": "Resume work",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "old-agent",
                "started_at": "2000-01-01T00:00:00+00:00",
                "lease_expires_at": "2000-01-01T00:00:01+00:00",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    message = main.atp_claim_task(str(plan_path), "new-agent")
    plan = read_plan(plan_path)
    audit = read_audit(plan_path)

    assert "TASK ASSIGNED: stale" in message
    assert plan["nodes"]["stale"]["status"] == "CLAIMED"
    assert plan["nodes"]["stale"]["worker_id"] == "new-agent"
    assert [event["action"] for event in audit] == ["zombie_recovery", "task_claimed"]
    assert audit[0]["recovered_nodes"] == ["stale"]
    assert audit[1]["node_id"] == "stale"


def test_claim_prefers_higher_priority_then_shorter_dependencies(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "dep": {
                "title": "Done",
                "instruction": "Done",
                "dependencies": [],
                "status": "COMPLETED",
            },
            "high-priority": {
                "title": "Urgent",
                "instruction": "Take first",
                "dependencies": ["dep"],
                "status": "READY",
                "priority": 10,
            },
            "same-priority-short": {
                "title": "Short",
                "instruction": "Take second",
                "dependencies": [],
                "status": "READY",
                "priority": 5,
            },
            "same-priority-long": {
                "title": "Long",
                "instruction": "Take third",
                "dependencies": ["dep"],
                "status": "READY",
                "priority": 5,
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    message = main.atp_claim_task(str(plan_path), "agent-1")
    assert "TASK ASSIGNED: high-priority" in message


def test_validate_subtasks_rejects_cycle() -> None:
    with pytest.raises(ValueError, match="cycle"):
        main.validate_subtasks(
            [
                {"id": "a", "description": "Task A", "dependencies": ["b"]},
                {"id": "b", "description": "Task B", "dependencies": ["a"]},
            ]
        )


def test_decompose_rewires_children_and_inherits_priority(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "root": {
                "title": "Done root",
                "instruction": "Already complete",
                "dependencies": [],
                "status": "COMPLETED",
            },
            "parent": {
                "title": "Big task",
                "instruction": "Split me",
                "dependencies": ["root"],
                "status": "READY",
                "priority": 3,
            },
            "downstream": {
                "title": "Downstream",
                "instruction": "Wait for parent",
                "dependencies": ["parent"],
                "status": "LOCKED",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    message = main.atp_decompose_task(
        str(plan_path),
        "parent",
        [
            {"id": "sub-a", "description": "Subtask A"},
            {"id": "sub-b", "description": "Subtask B", "dependencies": ["sub-a"]},
        ],
    )
    plan = read_plan(plan_path)
    audit = read_audit(plan_path)

    assert "Start nodes: sub-a." in message
    assert plan["nodes"]["parent"]["type"] == "SCOPE"
    assert plan["nodes"]["parent"]["scope_children"] == ["sub-a", "sub-b"]
    assert plan["nodes"]["sub-a"]["dependencies"] == ["root"]
    assert plan["nodes"]["sub-a"]["status"] == "READY"
    assert plan["nodes"]["sub-a"]["priority"] == 3
    assert plan["nodes"]["sub-b"]["dependencies"] == ["sub-a"]
    assert plan["nodes"]["downstream"]["dependencies"] == ["sub-b"]
    assert audit[-1]["action"] == "task_decomposed"


def test_decompose_claimed_parent_requires_matching_agent(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "parent": {
                "title": "Parent",
                "instruction": "Parent",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    with pytest.raises(ValueError, match="agent_id is required"):
        main.atp_decompose_task(str(plan_path), "parent", [{"id": "sub-a", "description": "Subtask"}])

    with pytest.raises(ValueError, match="claimed by"):
        main.atp_decompose_task(
            str(plan_path),
            "parent",
            [{"id": "sub-a", "description": "Subtask"}],
            agent_id="wrong-agent",
        )

    message = main.atp_decompose_task(
        str(plan_path),
        "parent",
        [{"id": "sub-a", "description": "Subtask"}],
        agent_id="agent-1",
    )
    assert "Decomposition successful" in message
    audit = read_audit(plan_path)
    assert audit[-1]["action"] == "task_decomposed"
    assert audit[-1]["agent_id"] == "agent-1"


def test_validate_graph_rejects_full_plan_cycle_contract() -> None:
    graph = make_graph(
        {
            "a": {
                "title": "A",
                "instruction": "A",
                "dependencies": ["b"],
                "status": "LOCKED",
            },
            "b": {
                "title": "B",
                "instruction": "B",
                "dependencies": ["a"],
                "status": "LOCKED",
            },
        }
    )

    with pytest.raises(ValidationError, match="cycle"):
        main.validate_graph(graph)


def test_schema_accepts_version_1_5_and_owner_id_contract() -> None:
    version_spec = main.VALIDATOR.schema["properties"]["meta"]["properties"]["version"]
    assert "1.5" in version_spec.get("enum", [])
    graph = make_graph(
        {
            "task": {
                "title": "Task",
                "instruction": "Do work",
                "dependencies": [],
                "status": "READY",
                "owner_id": "agent-1",
            }
        },
        version="1.5",
    )
    main.validate_graph(graph)


def test_decompose_rejects_existing_node_id_collision_contract(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "root": {
                "title": "Root",
                "instruction": "Root",
                "dependencies": [],
                "status": "COMPLETED",
            },
            "parent": {
                "title": "Parent",
                "instruction": "Parent",
                "dependencies": ["root"],
                "status": "READY",
            },
            "other-node": {
                "title": "Other",
                "instruction": "Other",
                "dependencies": [],
                "status": "READY",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)
    plan_before = read_plan(plan_path)
    audit_before = read_audit(plan_path)

    with pytest.raises(ValueError, match="already exist"):
        main.atp_decompose_task(
            str(plan_path),
            "parent",
            [{"id": "other-node", "description": "Collision"}],
        )
    assert read_plan(plan_path) == plan_before
    assert read_audit(plan_path) == audit_before


def test_decompose_rejects_parent_id_collision_contract(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "root": {
                "title": "Root",
                "instruction": "Root",
                "dependencies": [],
                "status": "COMPLETED",
            },
            "parent": {
                "title": "Parent",
                "instruction": "Parent",
                "dependencies": ["root"],
                "status": "READY",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)
    plan_before = read_plan(plan_path)
    audit_before = read_audit(plan_path)

    with pytest.raises(ValueError, match="already exist"):
        main.atp_decompose_task(
            str(plan_path),
            "parent",
            [{"id": "parent", "description": "Collision with parent"}],
        )
    assert read_plan(plan_path) == plan_before
    assert read_audit(plan_path) == audit_before


def test_complete_requires_claimed_and_matching_agent_contract(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "task": {
                "title": "Task",
                "instruction": "Task",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    with pytest.raises(ValueError, match="agent"):
        call_complete_task(plan_path, "task", "wrong-agent", "done")

    message = call_complete_task(plan_path, "task", "agent-1", "done")
    assert "completed" in message.lower()

    graph = make_graph(
        {
            "ready-task": {
                "title": "Ready task",
                "instruction": "Do work",
                "dependencies": [],
                "status": "READY",
                "owner_id": "agent-1",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    with pytest.raises(ValueError, match="CLAIMED"):
        call_complete_task(plan_path, "ready-task", "agent-1", "done")


def test_reset_requires_failed_and_owner_matching_contract(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "task": {
                "title": "Task",
                "instruction": "Task",
                "dependencies": [],
                "status": "FAILED",
                "owner_id": "agent-1",
                "report": "failed previously",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    with pytest.raises(ValueError, match="agent"):
        call_reset_task(plan_path, "task", "wrong-agent", "retry")

    message = call_reset_task(plan_path, "task", "agent-1", "retry")
    assert "reset to READY" in message


def test_legacy_unowned_failed_nodes_can_be_adopted_for_reset_or_cancel(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "legacy-failed": {
                "title": "Legacy failed",
                "instruction": "Recover me",
                "dependencies": [],
                "status": "FAILED",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    reset_message = call_reset_task(plan_path, "legacy-failed", "agent-1", "retry legacy task")
    assert "reset to READY" in reset_message
    plan = read_plan(plan_path)
    assert plan["nodes"]["legacy-failed"]["status"] == "READY"

    graph = make_graph(
        {
            "legacy-failed": {
                "title": "Legacy failed",
                "instruction": "Cancel me",
                "dependencies": [],
                "status": "FAILED",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    cancel_message = call_cancel_task(plan_path, "legacy-failed", "agent-1", "obsolete legacy task")
    assert "cancelled" in cancel_message
    plan = read_plan(plan_path)
    assert plan["nodes"]["legacy-failed"]["status"] == "CANCELLED"
    assert plan["nodes"]["legacy-failed"]["owner_id"] == "agent-1"


def test_cancel_requires_owned_claimed_or_failed_contract(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "claimed": {
                "title": "Claimed",
                "instruction": "Claimed",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
            "failed": {
                "title": "Failed",
                "instruction": "Failed",
                "dependencies": [],
                "status": "FAILED",
                "owner_id": "agent-1",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    with pytest.raises(ValueError, match="agent"):
        call_cancel_task(plan_path, "claimed", "wrong-agent", "stop")

    message = call_cancel_task(plan_path, "claimed", "agent-1", "stop")
    assert "cancelled" in message

    message = call_cancel_task(plan_path, "failed", "agent-1", "stop")
    assert "cancelled" in message


def test_cancel_rejects_ready_and_locked_contract(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "ready": {
                "title": "Ready",
                "instruction": "Ready",
                "dependencies": [],
                "status": "READY",
                "owner_id": "agent-1",
            },
            "locked": {
                "title": "Locked",
                "instruction": "Locked",
                "dependencies": ["ready"],
                "status": "LOCKED",
                "owner_id": "agent-1",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    with pytest.raises(ValueError, match="READY|owned"):
        call_cancel_task(plan_path, "ready", "agent-1", "stop")

    with pytest.raises(ValueError, match="LOCKED|owned"):
        call_cancel_task(plan_path, "locked", "agent-1", "stop")


def test_scope_terminal_contracts_and_reopen_on_reset(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "parent": {
                "title": "Scope",
                "instruction": "Scope",
                "dependencies": [],
                "status": "CLAIMED",
                "type": "SCOPE",
                "scope_children": ["child-a", "child-b"],
                "started_at": "2026-01-01T00:00:00+00:00",
                "owner_id": "agent-1",
            },
            "child-a": {
                "title": "A",
                "instruction": "A",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
            "child-b": {
                "title": "B",
                "instruction": "B",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
            "downstream": {
                "title": "Downstream",
                "instruction": "Downstream",
                "dependencies": ["parent"],
                "status": "LOCKED",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    call_complete_task(plan_path, "child-a", "agent-1", "ok", status="FAILED")
    call_complete_task(plan_path, "child-b", "agent-1", "ok")
    plan = read_plan(plan_path)
    assert plan["nodes"]["parent"]["status"] == "FAILED"

    call_reset_task(plan_path, "child-a", "agent-1", "retry")
    plan = read_plan(plan_path)
    assert plan["nodes"]["parent"]["status"] == "CLAIMED"
    assert "completed_at" not in plan["nodes"]["parent"]

    claim_message = main.atp_claim_task(str(plan_path), "agent-1")
    assert "TASK ASSIGNED: child-a" in claim_message
    call_complete_task(plan_path, "child-a", "agent-1", "ok")
    plan = read_plan(plan_path)
    assert plan["nodes"]["parent"]["status"] == "COMPLETED"
    assert plan["nodes"]["downstream"]["status"] == "READY"

    audit = read_audit(plan_path)
    scope_events = [event for event in audit if event["action"] == "scope_status_changed"]
    assert any(event.get("to_status") == "FAILED" for event in scope_events)
    assert any(event.get("to_status") == "CLAIMED" for event in scope_events)
    assert any(event.get("to_status") == "COMPLETED" for event in scope_events)


def test_scope_cancels_and_mixed_terminal_children_contract(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "parent": {
                "title": "Scope",
                "instruction": "Scope",
                "dependencies": [],
                "status": "CLAIMED",
                "type": "SCOPE",
                "scope_children": ["child-a", "child-b"],
                "owner_id": "agent-1",
            },
            "child-a": {
                "title": "A",
                "instruction": "A",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
            "child-b": {
                "title": "B",
                "instruction": "B",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    call_complete_task(plan_path, "child-a", "agent-1", "not needed", status="FAILED")
    call_cancel_task(plan_path, "child-b", "agent-1", "cancel")
    plan = read_plan(plan_path)
    assert plan["nodes"]["parent"]["status"] == "FAILED"

    graph = make_graph(
        {
            "parent": {
                "title": "Scope",
                "instruction": "Scope",
                "dependencies": [],
                "status": "CLAIMED",
                "type": "SCOPE",
                "scope_children": ["child-a", "child-b"],
                "owner_id": "agent-1",
            },
            "child-a": {
                "title": "A",
                "instruction": "A",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
            "child-b": {
                "title": "B",
                "instruction": "B",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    call_cancel_task(plan_path, "child-a", "agent-1", "cancel")
    call_complete_task(plan_path, "child-b", "agent-1", "ok")
    plan = read_plan(plan_path)
    assert plan["nodes"]["parent"]["status"] == "CANCELLED"


def test_audit_includes_agent_and_scope_transitions_contract(tmp_path: Path) -> None:
    require_agent_id_tools()
    graph = make_graph(
        {
            "parent": {
                "title": "Scope",
                "instruction": "Scope",
                "dependencies": [],
                "status": "CLAIMED",
                "type": "SCOPE",
                "scope_children": ["child"],
                "owner_id": "agent-1",
            },
            "child": {
                "title": "Child",
                "instruction": "Child",
                "dependencies": [],
                "status": "CLAIMED",
                "worker_id": "agent-1",
                "owner_id": "agent-1",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)

    call_complete_task(plan_path, "child", "agent-1", "done")
    audit = read_audit(plan_path)

    assert any(event.get("agent_id") == "agent-1" for event in audit)
    assert any(
        event["action"] == "scope_status_changed" and event.get("to_status") == "COMPLETED"
        for event in audit
    )


def test_rejected_decompose_leaves_plan_and_audit_unchanged_contract(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "root": {
                "title": "Root",
                "instruction": "Root",
                "dependencies": [],
                "status": "COMPLETED",
            },
            "parent": {
                "title": "Parent",
                "instruction": "Parent",
                "dependencies": ["root"],
                "status": "READY",
            },
            "other": {
                "title": "Other",
                "instruction": "Other",
                "dependencies": [],
                "status": "READY",
            },
        }
    )
    plan_path = write_plan(tmp_path, graph)
    plan_before = read_plan(plan_path)
    audit_before = read_audit(plan_path)

    with pytest.raises(ValueError, match="already exist"):
        main.atp_decompose_task(
            str(plan_path),
            "parent",
            [{"id": "other", "description": "Collision"}],
        )
    assert read_plan(plan_path) == plan_before
    assert read_audit(plan_path) == audit_before
