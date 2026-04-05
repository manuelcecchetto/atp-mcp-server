import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def make_graph(nodes):
    return {
        "meta": {
            "project_name": "ATP Test Plan",
            "version": "1.4",
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


def test_complete_task_requires_claimed_status(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "ready-task": {
                "title": "Ready only",
                "instruction": "Do the thing",
                "dependencies": [],
                "status": "READY",
            }
        }
    )
    plan_path = write_plan(tmp_path, graph)

    with pytest.raises(ValueError, match="must be CLAIMED"):
        main.atp_complete_task(str(plan_path), "ready-task", "done")


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


def test_reset_and_cancel_tools_update_status_and_audit(tmp_path: Path) -> None:
    graph = make_graph(
        {
            "flaky": {
                "title": "Flaky",
                "instruction": "Retry me",
                "dependencies": [],
                "status": "FAILED",
                "report": "Previous attempt failed",
                "artifacts": ["log.txt"],
                "started_at": "2026-01-01T00:00:00+00:00",
                "completed_at": "2026-01-01T00:10:00+00:00",
            },
            "obsolete": {
                "title": "Obsolete",
                "instruction": "Cancel me",
                "dependencies": [],
                "status": "READY",
            },
        }
    )
    plan_path = write_plan(tmp_path, copy.deepcopy(graph))

    reset_message = main.atp_reset_task(str(plan_path), "flaky", "retry after fix")
    cancel_message = main.atp_cancel_task(str(plan_path), "obsolete", "no longer needed")
    plan = read_plan(plan_path)
    audit = read_audit(plan_path)

    assert "reset to READY" in reset_message
    assert "cancelled" in cancel_message
    assert plan["nodes"]["flaky"]["status"] == "READY"
    assert plan["nodes"]["flaky"]["report"] == ""
    assert plan["nodes"]["flaky"]["artifacts"] == []
    assert plan["nodes"]["obsolete"]["status"] == "CANCELLED"
    assert [event["action"] for event in audit[-2:]] == ["task_reset", "task_cancelled"]
