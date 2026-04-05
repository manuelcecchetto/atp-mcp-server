from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from filelock import FileLock
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ATP Librarian")

LEASE_SECONDS = int(os.environ.get("ATP_LEASE_SECONDS", "600"))
SCHEMA_PATH = Path(__file__).with_name("atp_schema.json")
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


def _load_validator() -> Draft7Validator:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Missing schema file at {SCHEMA_PATH}. Ensure atp_schema.json is present."
        ) from exc
    return Draft7Validator(schema)


VALIDATOR = _load_validator()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        return None


def resolve_paths(plan_path: Optional[str]) -> Tuple[Path, Path]:
    plan = Path(plan_path or os.environ.get("ATP_FILE", ".atp.json")).expanduser()
    if not plan.is_absolute():
        plan = Path.cwd() / plan
    lock_env = os.environ.get("ATP_LOCK_FILE")
    lock = Path(lock_env).expanduser() if lock_env else Path(f"{plan}.lock")
    return plan, lock


DEFAULT_PLAN_FILE, DEFAULT_LOCK_FILE = resolve_paths(None)


def ensure_dependencies_exist(graph: Dict, exception_cls: type[Exception] = ValueError) -> None:
    nodes = graph.get("nodes", {})
    missing: List[Tuple[str, str]] = []
    for node_id, node in nodes.items():
        for dep in node.get("dependencies", []):
            if dep not in nodes:
                missing.append((node_id, dep))
    if missing:
        pairs = ", ".join(f"{nid}->{dep}" for nid, dep in missing)
        raise exception_cls(f"Graph references missing dependencies: {pairs}")


def ensure_acyclic(
    adjacency: Dict[str, List[str]], label: str, exception_cls: type[Exception] = ValueError
) -> None:
    visited: Dict[str, str] = {}
    verb = "contain" if label.endswith("s") else "contains"

    for root_id in adjacency:
        if visited.get(root_id) is not None:
            continue
        visited[root_id] = "VISITING"
        stack: List[Tuple[str, Any]] = [(root_id, iter(adjacency.get(root_id, [])))]
        while stack:
            node_id, deps_iter = stack[-1]
            try:
                dep_id = next(deps_iter)
            except StopIteration:
                visited[node_id] = "VISITED"
                stack.pop()
                continue

            state = visited.get(dep_id)
            if state == "VISITING":
                raise exception_cls(f"{label} {verb} a cycle.")
            if state is None:
                visited[dep_id] = "VISITING"
                stack.append((dep_id, iter(adjacency.get(dep_id, []))))


def validate_graph(graph: Dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(graph), key=lambda err: err.path)
    if errors:
        error = errors[0]
        raise ValidationError(f"{list(error.path)}: {error.message}")
    ensure_dependencies_exist(graph, ValidationError)
    adjacency = {
        node_id: list(node.get("dependencies", []))
        for node_id, node in graph.get("nodes", {}).items()
    }
    ensure_acyclic(adjacency, "Graph", ValidationError)


def load_graph(plan_file: Path) -> Dict:
    if not plan_file.exists():
        raise FileNotFoundError(
            f"No ATP graph found at {plan_file}. Create a file matching atp_schema.json."
        )
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    validate_graph(data)
    return data


def save_graph(graph: Dict, plan_file: Path) -> None:
    validate_graph(graph)
    payload = json.dumps(graph, indent=2) + "\n"
    tmp_file = plan_file.with_name(f"{plan_file.name}.tmp")
    try:
        with tmp_file.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_file, plan_file)
    finally:
        if tmp_file.exists():
            tmp_file.unlink(missing_ok=True)


def audit_log_path(plan_file: Path) -> Path:
    return Path(f"{plan_file}.audit.jsonl")


def append_audit_events(plan_file: Path, events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    with audit_log_path(plan_file).open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def build_audit_event(action: str, now: datetime, **payload: Any) -> Dict[str, Any]:
    return {
        "action": action,
        "timestamp": isoformat(now),
        **payload,
    }


@contextmanager
def graph_lock(
    plan_path: Optional[str] = None, *, resolved: Optional[Tuple[Path, Path]] = None
) -> Iterable[Path]:
    plan_file, lock_file = resolved or resolve_paths(plan_path)
    lock = FileLock(str(lock_file))
    with lock:
        yield plan_file


def read_graph_locked(
    plan_path: Optional[str] = None, *, resolved: Optional[Tuple[Path, Path]] = None
) -> Dict:
    with graph_lock(plan_path, resolved=resolved) as plan_file:
        return load_graph(plan_file)


@contextmanager
def locked_graph(plan_path: Optional[str]) -> Iterable[Tuple[Dict, Path, List[Dict[str, Any]]]]:
    with graph_lock(plan_path) as plan_file:
        graph = load_graph(plan_file)
        audit_events: List[Dict[str, Any]] = []
        yield graph, plan_file, audit_events
        save_graph(graph, plan_file)
        append_audit_events(plan_file, audit_events)


def dependencies_satisfied(nodes: Dict[str, Dict], dependencies: List[str]) -> bool:
    return all(nodes[dep]["status"] == "COMPLETED" for dep in dependencies)


def find_children(nodes: Dict[str, Dict], node_id: str) -> List[str]:
    return [nid for nid, node in nodes.items() if node_id in node.get("dependencies", [])]


def refresh_ready_nodes(graph: Dict) -> List[str]:
    unblocked: List[str] = []
    nodes = graph["nodes"]
    for node_id, node in nodes.items():
        if node.get("type") == "SCOPE":
            continue
        if node["status"] != "LOCKED":
            continue
        if dependencies_satisfied(nodes, node.get("dependencies", [])):
            node["status"] = "READY"
            unblocked.append(node_id)
    return unblocked


def release_zombie_claims(graph: Dict, now: datetime) -> List[str]:
    revived: List[str] = []
    nodes = graph["nodes"]
    for node_id, node in nodes.items():
        if node.get("type") == "SCOPE":
            continue
        if node["status"] != "CLAIMED":
            continue
        deadline = parse_iso(node.get("lease_expires_at"))
        started_at = parse_iso(node.get("started_at"))
        if deadline is None and started_at is not None:
            deadline = started_at + timedelta(seconds=LEASE_SECONDS)
        if deadline is not None and now > deadline:
            node["status"] = "READY"
            clear_worker(node)
            revived.append(node_id)
    return revived


def desired_scope_status(nodes: Dict[str, Dict], node_id: str, node: Dict) -> str:
    children = node.get("scope_children", [])
    missing = [child for child in children if child not in nodes]
    if missing:
        raise ValueError(f"Scope {node_id} references missing children: {', '.join(missing)}")
    if not children:
        return "CLAIMED"

    child_statuses = [nodes[child]["status"] for child in children]
    if any(status in {"LOCKED", "READY", "CLAIMED"} for status in child_statuses):
        return "CLAIMED"
    if all(status == "COMPLETED" for status in child_statuses):
        return "COMPLETED"
    if all(status in TERMINAL_STATUSES for status in child_statuses):
        if any(status == "FAILED" for status in child_statuses):
            return "FAILED"
        if any(status == "CANCELLED" for status in child_statuses):
            return "CANCELLED"
    return "CLAIMED"


def recompute_scope_statuses(
    graph: Dict,
    now: datetime,
    audit_events: Optional[List[Dict[str, Any]]] = None,
    *,
    agent_id: Optional[str] = None,
    trigger: Optional[str] = None,
) -> List[Tuple[str, str, str]]:
    transitions: List[Tuple[str, str, str]] = []
    nodes = graph["nodes"]

    changed = True
    while changed:
        changed = False
        for node_id, node in nodes.items():
            if node.get("type") != "SCOPE":
                continue
            current_status = node["status"]
            target_status = desired_scope_status(nodes, node_id, node)
            if current_status == target_status:
                continue

            node["status"] = target_status
            clear_worker(node)
            if target_status == "CLAIMED":
                node.pop("completed_at", None)
            else:
                node["completed_at"] = isoformat(now)

            transitions.append((node_id, current_status, target_status))
            if audit_events is not None:
                payload: Dict[str, Any] = {
                    "scope_id": node_id,
                    "from_status": current_status,
                    "to_status": target_status,
                }
                if agent_id is not None:
                    payload["agent_id"] = agent_id
                if trigger is not None:
                    payload["trigger"] = trigger
                audit_events.append(build_audit_event("scope_status_changed", now, **payload))
            changed = True

    return transitions


def extend_lease(node: Dict, now: datetime) -> None:
    node["lease_expires_at"] = isoformat(now + timedelta(seconds=LEASE_SECONDS))


def clear_worker(node: Dict) -> None:
    # Remove worker assignment for schema compatibility (worker_id is an optional string).
    node.pop("worker_id", None)
    node["lease_expires_at"] = None


def reset_runtime_fields(node: Dict) -> None:
    clear_worker(node)
    node.pop("owner_id", None)
    node.pop("started_at", None)
    node.pop("completed_at", None)
    node["artifacts"] = []
    node["report"] = ""


def current_owner(node: Dict) -> Optional[str]:
    return node.get("owner_id") or node.get("worker_id")


def assert_owned_by_agent(
    node: Dict, node_id: str, agent_id: str, *, adopt_unowned_terminal: bool = False
) -> None:
    owner_id = current_owner(node)
    if owner_id is None and adopt_unowned_terminal and node.get("status") in TERMINAL_STATUSES:
        node["owner_id"] = agent_id
        return
    if owner_id != agent_id:
        owner_text = owner_id or "unowned"
        raise ValueError(f"Node {node_id} is owned by {owner_text}; agent_id={agent_id} cannot mutate it.")


def assert_claimed_by_agent(node: Dict, node_id: str, agent_id: str) -> None:
    worker_id = node.get("worker_id")
    if worker_id != agent_id:
        worker_text = worker_id or "unassigned"
        raise ValueError(f"Node {node_id} is claimed by {worker_text}; agent_id={agent_id} cannot mutate it.")


def format_scope_transitions(transitions: List[Tuple[str, str, str]]) -> str:
    return ", ".join(f"{node_id}[{to_status}]" for node_id, _, to_status in transitions)


def format_dependency_context(nodes: Dict[str, Dict], node: Dict) -> str:
    lines: List[str] = []
    for dep_id in node.get("dependencies", []):
        dep_node = nodes[dep_id]
        report = dep_node.get("report") or "(no handoff provided)"
        lines.append(f"- From {dep_id} ({dep_node['status']}): {report}")
    if not lines:
        lines.append("- No parent context; follow the instruction directly.")
    return "\n".join(lines)


def format_assignment(node_id: str, node: Dict, nodes: Dict[str, Dict]) -> str:
    context = format_dependency_context(nodes, node)
    static_context = node.get("context")
    static_block = f"\nSTATIC CONTEXT:\n{static_context}\n" if static_context else ""
    return (
        f"TASK ASSIGNED: {node_id} - {node['title']}\n"
        f"STATUS: {node['status']}\n"
        f"INSTRUCTION:\n{node['instruction']}\n"
        f"{static_block}"
        f"CONTEXT FROM DEPENDENCIES:\n{context}\n"
        "INSTRUCTION: If this requires more than one file or touches multiple systems, "
        "call 'atp_decompose_task' to break it down."
    )


def project_active(graph: Dict) -> bool:
    return graph.get("meta", {}).get("project_status") == "ACTIVE"


def claim_ready_nodes(graph: Dict, agent_id: str, now: datetime) -> Tuple[Optional[str], str]:
    nodes = graph["nodes"]

    # Re-entry: return the node already claimed by this agent.
    for node_id, node in nodes.items():
        if node.get("worker_id") == agent_id and node["status"] == "CLAIMED":
            node["owner_id"] = agent_id
            extend_lease(node, now)
            return node_id, format_assignment(node_id, node, nodes)

    ready_nodes = [
        (node_id, node)
        for node_id, node in nodes.items()
        if node["status"] == "READY" and node.get("type") != "SCOPE"
    ]
    if not ready_nodes:
        return None, "NO_TASKS_AVAILABLE: All tasks are blocked, claimed, or the project is finished."

    ready_nodes.sort(
        key=lambda item: (
            -int(item[1].get("priority", 0)),
            len(item[1].get("dependencies", [])),
            item[0],
        )
    )
    node_id, node = ready_nodes[0]
    node["status"] = "CLAIMED"
    node["worker_id"] = agent_id
    node["owner_id"] = agent_id
    node["started_at"] = isoformat(now)
    extend_lease(node, now)
    return node_id, format_assignment(node_id, node, nodes)


@mcp.tool()
def atp_claim_task(plan_path: str, agent_id: str) -> str:
    """
    Requests the next available task. Handles zombie recovery and re-entry.
    """
    now = utc_now()
    with locked_graph(plan_path) as (graph, _, audit_events):
        if not project_active(graph):
            status = graph.get("meta", {}).get("project_status")
            return f"Project is not ACTIVE (status={status}). Resume the project before claiming work."

        revived = release_zombie_claims(graph, now)
        unblocked = refresh_ready_nodes(graph)
        scope_transitions = recompute_scope_statuses(
            graph, now, audit_events, agent_id=agent_id, trigger="claim"
        )
        scope_unblocked = refresh_ready_nodes(graph)
        node_id, message = claim_ready_nodes(graph, agent_id, now)

        if revived:
            audit_events.append(
                build_audit_event("zombie_recovery", now, recovered_nodes=revived)
            )
        if node_id:
            audit_events.append(
                build_audit_event(
                    "task_claimed",
                    now,
                    agent_id=agent_id,
                    node_id=node_id,
                    recovered_nodes=revived,
                    newly_ready=unblocked,
                    scope_updates=[
                        {"scope_id": scope_id, "from_status": old_status, "to_status": new_status}
                        for scope_id, old_status, new_status in scope_transitions
                    ],
                    ready_after_scope=scope_unblocked,
                )
            )

    if node_id:
        return message

    extra = []
    if revived:
        extra.append(f"Recovered stale tasks: {', '.join(revived)}.")
    if unblocked:
        extra.append(f"Newly READY: {', '.join(unblocked)}.")
    if scope_transitions:
        extra.append(f"Scope updates: {format_scope_transitions(scope_transitions)}.")
    if scope_unblocked:
        extra.append(f"READY after scope updates: {', '.join(scope_unblocked)}.")
    suffix = " " + " ".join(extra) if extra else ""
    return message + suffix


def normalize_completion_status(status: str) -> str:
    status_map = {
        "DONE": "COMPLETED",
        "COMPLETED": "COMPLETED",
        "FAILED": "FAILED",
    }
    normalized = status_map.get(status.upper())
    if not normalized:
        raise ValueError("status must be one of DONE or FAILED")
    return normalized


@mcp.tool()
def atp_complete_task(
    plan_path: str,
    node_id: str,
    agent_id: str,
    report: str,
    artifacts: Optional[List[str]] = None,
    status: str = "DONE",
) -> str:
    """
    Marks a node as DONE or FAILED and unlocks dependent tasks.
    """
    now = utc_now()
    new_status = normalize_completion_status(status)

    with locked_graph(plan_path) as (graph, _, audit_events):
        nodes = graph["nodes"]
        if node_id not in nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        node = nodes[node_id]
        if node.get("type") == "SCOPE":
            raise ValueError("Scope nodes close automatically once their children are done.")
        if node["status"] != "CLAIMED":
            raise ValueError(f"Node {node_id} must be CLAIMED before completion; current status={node['status']}.")
        assert_claimed_by_agent(node, node_id, agent_id)

        node["owner_id"] = agent_id
        node["status"] = new_status
        node["report"] = report.strip()
        node["artifacts"] = artifacts or []
        node["completed_at"] = isoformat(now)
        clear_worker(node)

        scope_transitions = recompute_scope_statuses(
            graph, now, audit_events, agent_id=agent_id, trigger="complete"
        )
        ready_after_parent = refresh_ready_nodes(graph)
        newly_ready = list(dict.fromkeys(ready_after_parent))
        audit_events.append(
            build_audit_event(
                "task_completed",
                now,
                agent_id=agent_id,
                node_id=node_id,
                status=new_status,
                artifacts=node["artifacts"],
                report=node["report"],
                newly_ready=newly_ready,
                scope_updates=[
                    {"scope_id": scope_id, "from_status": old_status, "to_status": new_status}
                    for scope_id, old_status, new_status in scope_transitions
                ],
            )
        )

    scope_suffix = (
        f" Scope updates: {format_scope_transitions(scope_transitions)}."
        if scope_transitions
        else ""
    )
    if new_status == "FAILED":
        return f"Task {node_id} marked as FAILED. Dependent tasks remain blocked.{scope_suffix}"
    if newly_ready:
        return f"Task {node_id} completed.{scope_suffix} Newly READY: {', '.join(newly_ready)}."
    return f"Task {node_id} completed.{scope_suffix} No downstream tasks were unblocked."


def validate_subtasks(subtasks: List[Dict]) -> None:
    ids = [task["id"] for task in subtasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Subtask IDs must be unique within the decomposition request.")
    for task in subtasks:
        if "description" not in task:
            raise ValueError(f"Subtask {task['id']} is missing description.")
        deps = task.get("dependencies", [])
        unknown = [dep for dep in deps if dep not in ids]
        if unknown:
            raise ValueError(
                f"Subtask {task['id']} has dependencies that are not in the subgraph: {', '.join(unknown)}"
            )
        priority = task.get("priority")
        if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool)):
            raise ValueError(f"Subtask {task['id']} priority must be an integer.")
    if not subtasks:
        raise ValueError("Provide at least one subtask.")

    adjacency: Dict[str, List[str]] = {task["id"]: task.get("dependencies", []) for task in subtasks}
    ensure_acyclic(adjacency, "Subtasks")


def graft_subgraph(
    graph: Dict, parent_id: str, subtasks: List[Dict], now: datetime
) -> Tuple[List[str], List[str]]:
    nodes = graph["nodes"]
    parent = nodes[parent_id]

    original_dependencies = parent.get("dependencies", [])
    original_children = find_children(nodes, parent_id)

    validate_subtasks(subtasks)

    new_ids = [task["id"] for task in subtasks]
    collisions = sorted(task_id for task_id in new_ids if task_id in nodes)
    if collisions:
        raise ValueError(
            f"Subtask IDs already exist in the plan and cannot be reused: {', '.join(collisions)}"
        )
    subgraph_dependencies = {task["id"]: task.get("dependencies", []) for task in subtasks}
    dependents: Dict[str, List[str]] = {tid: [] for tid in new_ids}
    for node_id, deps in subgraph_dependencies.items():
        for dep in deps:
            dependents.setdefault(dep, []).append(node_id)

    start_nodes = [tid for tid, deps in subgraph_dependencies.items() if not deps]
    end_nodes = [tid for tid in new_ids if not dependents.get(tid)]

    for task in subtasks:
        task_id = task["id"]
        deps = list(task.get("dependencies", []))
        if task_id in start_nodes:
            deps = list(dict.fromkeys(original_dependencies + deps))
        node_payload = {
            "title": task.get("title") or task["description"],
            "instruction": task.get("instruction") or task["description"],
            "dependencies": deps,
            "status": "LOCKED",
            "artifacts": [],
            "priority": task.get("priority", parent.get("priority", 0)),
        }
        if task.get("context"):
            node_payload["context"] = task["context"]
        nodes[task_id] = node_payload

    parent["type"] = "SCOPE"
    parent["status"] = "CLAIMED"
    parent["scope_children"] = new_ids
    if parent.get("worker_id"):
        parent["owner_id"] = parent["worker_id"]
    clear_worker(parent)
    parent.setdefault("started_at", isoformat(now))

    for child_id in original_children:
        child = nodes[child_id]
        deps = [dep for dep in child.get("dependencies", []) if dep != parent_id]
        child["dependencies"] = list(dict.fromkeys(deps + end_nodes))

    refresh_ready_nodes(graph)
    return start_nodes, end_nodes


@mcp.tool()
def atp_decompose_task(
    plan_path: str, parent_id: str, subtasks: List[Dict], agent_id: Optional[str] = None
) -> str:
    """
    Decomposes a task into a new subgraph and converts the parent into a scope.
    """
    now = utc_now()
    with locked_graph(plan_path) as (graph, _, audit_events):
        nodes = graph["nodes"]
        if parent_id not in nodes:
            raise ValueError(f"Node {parent_id} does not exist.")
        parent = nodes[parent_id]
        if parent.get("type") == "SCOPE":
            raise ValueError(f"Node {parent_id} is already a scope.")
        if parent["status"] not in {"CLAIMED", "READY"}:
            raise ValueError(f"Node {parent_id} must be CLAIMED or READY to decompose.")
        if parent["status"] == "CLAIMED":
            if not agent_id:
                raise ValueError("agent_id is required to decompose a CLAIMED node.")
            assert_claimed_by_agent(parent, parent_id, agent_id)

        start_nodes, end_nodes = graft_subgraph(graph, parent_id, subtasks, now)
        scope_transitions = recompute_scope_statuses(
            graph, now, audit_events, agent_id=agent_id, trigger="decompose"
        )
        refresh_ready_nodes(graph)
        audit_events.append(
            build_audit_event(
                "task_decomposed",
                now,
                agent_id=agent_id,
                parent_id=parent_id,
                subtask_ids=[task["id"] for task in subtasks],
                start_nodes=start_nodes,
                end_nodes=end_nodes,
                scope_updates=[
                    {"scope_id": scope_id, "from_status": old_status, "to_status": new_status}
                    for scope_id, old_status, new_status in scope_transitions
                ],
            )
        )

    scope_suffix = (
        f" Scope updates: {format_scope_transitions(scope_transitions)}."
        if scope_transitions
        else " Scope updates: none."
    )
    return (
        "Decomposition successful. Parent converted to SCOPE and will close after children finish. "
        f"Start nodes: {', '.join(start_nodes)}. End nodes: {', '.join(end_nodes)}. "
        f"You are released; call 'atp_claim_task' to continue. "
        f"{scope_suffix}"
    )


def summarize_status(graph: Dict) -> str:
    nodes = graph.get("nodes", {})
    counts: Dict[str, int] = {
        "LOCKED": 0,
        "READY": 0,
        "CLAIMED": 0,
        "COMPLETED": 0,
        "FAILED": 0,
        "CANCELLED": 0,
    }
    claimed: List[str] = []
    for node_id, node in nodes.items():
        status = node["status"]
        counts[status] = counts.get(status, 0) + 1
        if status == "CLAIMED":
            claimed.append(f"{node_id} ({node.get('worker_id') or 'unassigned'})")
    headline = (
        f"ATP Project Status: {graph.get('meta', {}).get('project_name', 'Unknown')} "
        f"({graph.get('meta', {}).get('project_status', 'UNKNOWN')})"
    )
    lines = [
        headline,
        (
            f"READY: {counts['READY']} | CLAIMED: {counts['CLAIMED']} | COMPLETED: {counts['COMPLETED']} "
            f"| FAILED: {counts['FAILED']} | CANCELLED: {counts['CANCELLED']}"
        ),
    ]
    if claimed:
        lines.append("Claimed tasks:")
        lines.extend([f"- {entry}" for entry in claimed])
    ready = [nid for nid, node in nodes.items() if node["status"] == "READY"]
    if ready:
        lines.append("Ready to start:")
        lines.extend([f"- {nid}: {nodes[nid]['title']}" for nid in ready])
    return "\n".join(lines)


@mcp.resource("atp://status/summary")
def status_summary() -> str:
    """
    Lightweight status dashboard for humans/agents.
    """
    try:
        graph = read_graph_locked(resolved=(DEFAULT_PLAN_FILE, DEFAULT_LOCK_FILE))
    except FileNotFoundError as exc:
        return str(exc)
    except ValidationError as exc:
        return f"Graph failed validation: {exc}"
    return summarize_status(graph)


def render_local_view(graph: Dict, center: str) -> str:
    nodes = graph.get("nodes", {})
    if center not in nodes:
        raise ValueError(f"Node {center} does not exist.")
    node = nodes[center]
    parents = node.get("dependencies", [])
    children = find_children(nodes, center)
    lines = [
        f"{center}: {node['title']} [{node['status']}]",
        f"Instruction: {node['instruction']}",
        f"Dependencies: {', '.join(parents) if parents else 'None'}",
        f"Children: {', '.join(children) if children else 'None'}",
    ]
    for dep_id in parents:
        parent = nodes[dep_id]
        lines.append(f"- Parent {dep_id} [{parent['status']}]: {parent.get('report') or 'no report'}")
    for child_id in children:
        child = nodes[child_id]
        lines.append(f"- Child {child_id} [{child['status']}]: {child['title']}")
    return "\n".join(lines)


@mcp.tool()
def atp_read_graph(plan_path: str, view_mode: str = "full", node_id: Optional[str] = None) -> str:
    """
    Returns either the full graph JSON or a narrow neighborhood view.
    """
    graph = read_graph_locked(plan_path)
    if view_mode == "full":
        return json.dumps(graph, indent=2)
    if view_mode == "local":
        if not node_id:
            raise ValueError("node_id is required for local view.")
        return render_local_view(graph, node_id)
    raise ValueError("view_mode must be 'full' or 'local'.")


@mcp.tool()
def atp_reset_task(plan_path: str, node_id: str, agent_id: str, reason: str) -> str:
    """
    Resets a FAILED node back to READY so it can be claimed again.
    """
    now = utc_now()
    with locked_graph(plan_path) as (graph, _, audit_events):
        nodes = graph["nodes"]
        if node_id not in nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        node = nodes[node_id]
        if node.get("type") == "SCOPE":
            raise ValueError("Scope nodes cannot be reset manually.")
        if node["status"] != "FAILED":
            raise ValueError(f"Node {node_id} must be FAILED to reset; current status={node['status']}.")
        assert_owned_by_agent(node, node_id, agent_id, adopt_unowned_terminal=True)

        node["status"] = "READY"
        reset_runtime_fields(node)
        scope_transitions = recompute_scope_statuses(
            graph, now, audit_events, agent_id=agent_id, trigger="reset"
        )
        audit_events.append(
            build_audit_event(
                "task_reset",
                now,
                agent_id=agent_id,
                node_id=node_id,
                from_status="FAILED",
                to_status="READY",
                reason=reason.strip(),
                scope_updates=[
                    {"scope_id": scope_id, "from_status": old_status, "to_status": new_status}
                    for scope_id, old_status, new_status in scope_transitions
                ],
            )
        )

    scope_suffix = (
        f" Scope updates: {format_scope_transitions(scope_transitions)}."
        if scope_transitions
        else ""
    )
    return f"Task {node_id} reset to READY.{scope_suffix} Reason recorded: {reason.strip() or '(none)'}."


@mcp.tool()
def atp_cancel_task(plan_path: str, node_id: str, agent_id: str, reason: str) -> str:
    """
    Cancels a node and leaves its dependents blocked until the graph is changed.
    """
    now = utc_now()
    with locked_graph(plan_path) as (graph, _, audit_events):
        nodes = graph["nodes"]
        if node_id not in nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        node = nodes[node_id]
        if node.get("type") == "SCOPE":
            raise ValueError("Scope nodes cannot be cancelled manually.")
        if node["status"] not in {"CLAIMED", "FAILED"}:
            raise ValueError(
                f"Node {node_id} can be cancelled only from CLAIMED or FAILED; current status={node['status']}."
            )
        assert_owned_by_agent(node, node_id, agent_id, adopt_unowned_terminal=True)
        if node["status"] == "CLAIMED":
            assert_claimed_by_agent(node, node_id, agent_id)

        previous_status = node["status"]
        node["status"] = "CANCELLED"
        node["owner_id"] = agent_id
        clear_worker(node)
        node["completed_at"] = isoformat(now)
        scope_transitions = recompute_scope_statuses(
            graph, now, audit_events, agent_id=agent_id, trigger="cancel"
        )
        audit_events.append(
            build_audit_event(
                "task_cancelled",
                now,
                agent_id=agent_id,
                node_id=node_id,
                from_status=previous_status,
                to_status="CANCELLED",
                reason=reason.strip(),
                scope_updates=[
                    {"scope_id": scope_id, "from_status": old_status, "to_status": new_status}
                    for scope_id, old_status, new_status in scope_transitions
                ],
            )
        )

    scope_suffix = (
        f" Scope updates: {format_scope_transitions(scope_transitions)}."
        if scope_transitions
        else ""
    )
    return (
        f"Task {node_id} cancelled.{scope_suffix} Dependents remain blocked until the graph is updated. "
        f"Reason recorded: {reason.strip() or '(none)'}."
    )


if __name__ == "__main__":
    mcp.run()
