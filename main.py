from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from filelock import FileLock
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ATP Librarian")

LEASE_SECONDS = int(os.environ.get("ATP_LEASE_SECONDS", "600"))
SCHEMA_PATH = Path(__file__).with_name("atp_schema.json")


def _load_validator() -> Draft7Validator:
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
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


def ensure_dependencies_exist(graph: Dict) -> None:
    nodes = graph.get("nodes", {})
    missing: List[Tuple[str, str]] = []
    for node_id, node in nodes.items():
        for dep in node.get("dependencies", []):
            if dep not in nodes:
                missing.append((node_id, dep))
    if missing:
        pairs = ", ".join(f"{nid}->{dep}" for nid, dep in missing)
        raise ValueError(f"Graph references missing dependencies: {pairs}")


def validate_graph(graph: Dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(graph), key=lambda err: err.path)
    if errors:
        error = errors[0]
        raise ValidationError(f"{list(error.path)}: {error.message}")
    ensure_dependencies_exist(graph)


def load_graph(plan_file: Path) -> Dict:
    if not plan_file.exists():
        raise FileNotFoundError(
            f"No ATP graph found at {plan_file}. Create a file matching atp_schema.json."
        )
    data = json.loads(plan_file.read_text())
    validate_graph(data)
    return data


def save_graph(graph: Dict, plan_file: Path) -> None:
    validate_graph(graph)
    plan_file.write_text(json.dumps(graph, indent=2) + "\n")


def compute_graph_version(graph: Dict) -> str:
    snapshot = copy.deepcopy(graph)
    meta = snapshot.get("meta")
    if isinstance(meta, dict):
        meta.pop("graph_version", None)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def graph_with_runtime_meta(graph: Dict) -> Dict:
    snapshot = copy.deepcopy(graph)
    snapshot.setdefault("meta", {})["graph_version"] = compute_graph_version(graph)
    return snapshot


@contextmanager
def locked_graph(plan_path: str) -> Iterable[Dict]:
    plan_file, lock_file = resolve_paths(plan_path)
    lock = FileLock(str(lock_file))
    with lock:
        graph = load_graph(plan_file)
        yield graph
        save_graph(graph, plan_file)


def dependencies_satisfied(nodes: Dict[str, Dict], dependencies: List[str]) -> bool:
    return all(nodes[dep]["status"] == "COMPLETED" for dep in dependencies)


def node_is_closed(node: Dict) -> bool:
    return node.get("future_state") in {"CLOSED", "SUPERSEDED"}


def node_is_mutable_future(node: Dict) -> bool:
    return node.get("type") != "SCOPE" and not node_is_closed(node) and node["status"] in {"READY", "LOCKED"}


def find_children(nodes: Dict[str, Dict], node_id: str) -> List[str]:
    return [nid for nid, node in nodes.items() if node_id in node.get("dependencies", [])]


def refresh_ready_nodes(graph: Dict) -> List[str]:
    unblocked: List[str] = []
    nodes = graph["nodes"]
    for node_id, node in nodes.items():
        if node.get("type") == "SCOPE":
            continue
        if node_is_closed(node):
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


def maybe_complete_scopes(graph: Dict, now: datetime) -> List[str]:
    closed: List[str] = []
    nodes = graph["nodes"]
    for node_id, node in nodes.items():
        if node.get("type") != "SCOPE":
            continue
        if node["status"] in {"COMPLETED", "FAILED"}:
            continue
        children = node.get("scope_children", [])
        missing = [child for child in children if child not in nodes]
        if missing:
            raise ValueError(f"Scope {node_id} references missing children: {', '.join(missing)}")
        if children and all(nodes[child]["status"] == "COMPLETED" for child in children):
            node["status"] = "COMPLETED"
            node["completed_at"] = isoformat(now)
            clear_worker(node)
            closed.append(node_id)
    return closed


def extend_lease(node: Dict, now: datetime) -> None:
    node["lease_expires_at"] = isoformat(now + timedelta(seconds=LEASE_SECONDS))


def clear_worker(node: Dict) -> None:
    # Remove worker assignment for schema compatibility (worker_id is an optional string).
    node.pop("worker_id", None)
    node["lease_expires_at"] = None


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
            extend_lease(node, now)
            return node_id, format_assignment(node_id, node, nodes)

    ready_nodes = [
        (node_id, node)
        for node_id, node in nodes.items()
        if node["status"] == "READY" and node.get("type") != "SCOPE" and not node_is_closed(node)
    ]
    if not ready_nodes:
        return None, "NO_TASKS_AVAILABLE: All tasks are blocked, claimed, or the project is finished."

    ready_nodes.sort(key=lambda item: (len(item[1].get("dependencies", [])), item[0]))
    node_id, node = ready_nodes[0]
    node["status"] = "CLAIMED"
    node["worker_id"] = agent_id
    node["started_at"] = isoformat(now)
    extend_lease(node, now)
    return node_id, format_assignment(node_id, node, nodes)


@mcp.tool()
def atp_claim_task(plan_path: str, agent_id: str) -> str:
    """
    Requests the next available task. Handles zombie recovery and re-entry.
    """
    now = utc_now()
    with locked_graph(plan_path) as graph:
        if not project_active(graph):
            status = graph.get("meta", {}).get("project_status")
            return f"Project is not ACTIVE (status={status}). Resume the project before claiming work."

        revived = release_zombie_claims(graph, now)
        unblocked = refresh_ready_nodes(graph)
        closed_scopes = maybe_complete_scopes(graph, now)
        scope_unblocked = refresh_ready_nodes(graph)
        node_id, message = claim_ready_nodes(graph, agent_id, now)

    if node_id:
        return message

    extra = []
    if revived:
        extra.append(f"Recovered stale tasks: {', '.join(revived)}.")
    if unblocked:
        extra.append(f"Newly READY: {', '.join(unblocked)}.")
    if closed_scopes:
        extra.append(f"Scopes completed: {', '.join(closed_scopes)}.")
    if scope_unblocked:
        extra.append(f"READY after scope closure: {', '.join(scope_unblocked)}.")
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
    report: str,
    artifacts: Optional[List[str]] = None,
    status: str = "DONE",
) -> str:
    """
    Marks a node as DONE or FAILED and unlocks dependent tasks.
    """
    now = utc_now()
    new_status = normalize_completion_status(status)

    with locked_graph(plan_path) as graph:
        nodes = graph["nodes"]
        if node_id not in nodes:
            raise ValueError(f"Node {node_id} does not exist.")
        node = nodes[node_id]
        if node.get("type") == "SCOPE":
            raise ValueError("Scope nodes close automatically once their children are done.")
        if node["status"] not in {"CLAIMED", "READY"}:
            raise ValueError(f"Node {node_id} is not in progress; current status={node['status']}.")

        node["status"] = new_status
        node["report"] = report.strip()
        node["artifacts"] = artifacts or []
        node["completed_at"] = isoformat(now)
        clear_worker(node)

        if new_status == "FAILED":
            return f"Task {node_id} marked as FAILED. Dependent tasks remain blocked."

        unblocked = refresh_ready_nodes(graph)
        closed_scopes = maybe_complete_scopes(graph, now)
        ready_after_parent = refresh_ready_nodes(graph)
        newly_ready = list(dict.fromkeys(unblocked + closed_scopes + ready_after_parent))

    if newly_ready:
        return f"Task {node_id} completed. Newly READY: {', '.join(newly_ready)}."
    return f"Task {node_id} completed. No downstream tasks were unblocked."


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
    if not subtasks:
        raise ValueError("Provide at least one subtask.")

    adjacency: Dict[str, List[str]] = {task["id"]: task.get("dependencies", []) for task in subtasks}
    visited: Dict[str, str] = {}

    def dfs(node_id: str) -> None:
        visited[node_id] = "VISITING"
        for dep in adjacency.get(node_id, []):
            state = visited.get(dep)
            if state == "VISITING":
                raise ValueError("Subtasks contain a cycle.")
            if state is None:
                dfs(dep)
        visited[node_id] = "VISITED"

    for node_id in adjacency:
        if visited.get(node_id) is None:
            dfs(node_id)


def graft_subgraph(
    graph: Dict, parent_id: str, subtasks: List[Dict], now: datetime
) -> Tuple[List[str], List[str]]:
    nodes = graph["nodes"]
    parent = nodes[parent_id]

    original_dependencies = parent.get("dependencies", [])
    original_children = find_children(nodes, parent_id)

    validate_subtasks(subtasks)

    new_ids = [task["id"] for task in subtasks]
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
        }
        if task.get("context"):
            node_payload["context"] = task["context"]
        nodes[task_id] = node_payload

    parent["type"] = "SCOPE"
    parent["status"] = "CLAIMED"
    parent["scope_children"] = new_ids
    clear_worker(parent)
    parent.setdefault("started_at", isoformat(now))

    for child_id in original_children:
        child = nodes[child_id]
        deps = [dep for dep in child.get("dependencies", []) if dep != parent_id]
        child["dependencies"] = list(dict.fromkeys(deps + end_nodes))

    refresh_ready_nodes(graph)
    return start_nodes, end_nodes


@mcp.tool()
def atp_decompose_task(plan_path: str, parent_id: str, subtasks: List[Dict]) -> str:
    """
    Decomposes a task into a new subgraph and converts the parent into a scope.
    """
    now = utc_now()
    with locked_graph(plan_path) as graph:
        nodes = graph["nodes"]
        if parent_id not in nodes:
            raise ValueError(f"Node {parent_id} does not exist.")
        parent = nodes[parent_id]
        if parent.get("type") == "SCOPE":
            raise ValueError(f"Node {parent_id} is already a scope.")
        if parent["status"] not in {"CLAIMED", "READY"}:
            raise ValueError(f"Node {parent_id} must be CLAIMED or READY to decompose.")

        start_nodes, end_nodes = graft_subgraph(graph, parent_id, subtasks, now)
        closed_scopes = maybe_complete_scopes(graph, now)
        refresh_ready_nodes(graph)

    return (
        "Decomposition successful. Parent converted to SCOPE and will close after children finish. "
        f"Start nodes: {', '.join(start_nodes)}. End nodes: {', '.join(end_nodes)}. "
        f"You are released; call 'atp_claim_task' to continue. "
        f"Scopes closed during this operation: {', '.join(closed_scopes) if closed_scopes else 'none'}."
    )


def claimed_node_count(graph: Dict) -> int:
    return sum(1 for node in graph.get("nodes", {}).values() if node["status"] == "CLAIMED")


def normalize_future_status(value: Optional[str]) -> str:
    if value is None:
        return "LOCKED"
    status = value.upper()
    if status not in {"LOCKED", "READY"}:
        raise ValueError("Future patch node status must be LOCKED or READY.")
    return status


def ensure_patch_ids_unique(add_nodes: List[Dict]) -> None:
    ids = [node["id"] for node in add_nodes]
    if len(ids) != len(set(ids)):
        raise ValueError("Patch add_nodes entries must have unique IDs.")


def ensure_dependencies_known(
    dependencies: List[str], known_ids: set[str], field_name: str, node_id: str
) -> None:
    unknown = [dep for dep in dependencies if dep not in known_ids]
    if unknown:
        raise ValueError(
            f"{field_name} for {node_id} references unknown dependencies: {', '.join(unknown)}"
        )


def recompute_future_readiness(graph: Dict) -> None:
    nodes = graph["nodes"]
    for node in nodes.values():
        if node.get("type") == "SCOPE" or node_is_closed(node):
            continue
        if node["status"] in {"READY", "LOCKED"}:
            node["status"] = (
                "READY" if dependencies_satisfied(nodes, node.get("dependencies", [])) else "LOCKED"
            )


def ensure_no_active_dependency_on_closed_node(graph: Dict) -> None:
    nodes = graph["nodes"]
    closed_ids = {node_id for node_id, node in nodes.items() if node_is_closed(node)}
    if not closed_ids:
        return
    offenders: List[str] = []
    for node_id, node in nodes.items():
        if node_id in closed_ids or node["status"] in {"COMPLETED", "FAILED"}:
            continue
        for dep in node.get("dependencies", []):
            if dep in closed_ids:
                offenders.append(f"{node_id}->{dep}")
    if offenders:
        raise ValueError(
            "Future patch leaves active nodes depending on closed nodes: "
            + ", ".join(sorted(offenders))
        )


def apply_future_patch_to_graph(
    graph: Dict, patch: Dict, reason: str, actor_id: str, now: datetime
) -> Dict[str, int]:
    nodes = graph["nodes"]
    add_nodes = patch.get("add_nodes", []) or []
    update_nodes = patch.get("update_nodes", []) or []
    close_nodes = patch.get("close_nodes", []) or []
    rewire_edges = patch.get("rewire_edges", []) or []

    ensure_patch_ids_unique(add_nodes)
    new_ids = {node["id"] for node in add_nodes}
    collisions = sorted(node_id for node_id in new_ids if node_id in nodes)
    if collisions:
        raise ValueError(
            "Future patch add_nodes contains IDs already present in graph: " + ", ".join(collisions)
        )

    known_ids = set(nodes.keys()) | new_ids
    patch_timestamp = isoformat(now)

    for payload in add_nodes:
        node_id = payload["id"]
        dependencies = list(payload.get("dependencies", []))
        ensure_dependencies_known(dependencies, known_ids, "dependencies", node_id)
        nodes[node_id] = {
            "title": payload["title"],
            "instruction": payload["instruction"],
            "dependencies": dependencies,
            "status": normalize_future_status(payload.get("status")),
            "reasoning_effort": payload.get("reasoning_effort", "medium"),
            "future_patch_reason": reason,
            "future_patched_at": patch_timestamp,
            "future_patched_by": actor_id,
        }
        if payload.get("context"):
            nodes[node_id]["context"] = payload["context"]

    for payload in update_nodes:
        node_id = payload["id"]
        if node_id not in nodes:
            raise ValueError(f"Cannot update missing node {node_id}.")
        node = nodes[node_id]
        if not node_is_mutable_future(node):
            raise ValueError(f"Node {node_id} is not a mutable future node.")
        for field in ("title", "instruction", "reasoning_effort"):
            if field in payload:
                node[field] = payload[field]
        if "context" in payload:
            if payload["context"] in (None, ""):
                node.pop("context", None)
            else:
                node["context"] = payload["context"]
        if "status" in payload:
            node["status"] = normalize_future_status(payload["status"])
        node["future_patch_reason"] = reason
        node["future_patched_at"] = patch_timestamp
        node["future_patched_by"] = actor_id

    for payload in rewire_edges:
        node_id = payload.get("id") or payload.get("node_id")
        if not node_id or node_id not in nodes:
            raise ValueError(f"Cannot rewire missing node {node_id}.")
        node = nodes[node_id]
        if not node_is_mutable_future(node):
            raise ValueError(f"Node {node_id} is not a mutable future node.")
        dependencies = list(payload.get("dependencies", []))
        ensure_dependencies_known(dependencies, known_ids, "rewire dependencies", node_id)
        node["dependencies"] = dependencies
        node["future_patch_reason"] = reason
        node["future_patched_at"] = patch_timestamp
        node["future_patched_by"] = actor_id

    for payload in close_nodes:
        if isinstance(payload, str):
            node_id = payload
            future_state = "SUPERSEDED"
            superseded_by = None
        else:
            node_id = payload["id"]
            future_state = payload.get("future_state", "SUPERSEDED")
            superseded_by = payload.get("superseded_by")
        if node_id not in nodes:
            raise ValueError(f"Cannot close missing node {node_id}.")
        node = nodes[node_id]
        if not node_is_mutable_future(node):
            raise ValueError(f"Node {node_id} is not a mutable future node.")
        node["future_state"] = future_state
        node["future_patch_reason"] = reason
        node["future_patched_at"] = patch_timestamp
        node["future_patched_by"] = actor_id
        if superseded_by:
            node["superseded_by"] = superseded_by

    recompute_future_readiness(graph)
    ensure_no_active_dependency_on_closed_node(graph)
    refresh_ready_nodes(graph)

    return {
        "add_nodes": len(add_nodes),
        "update_nodes": len(update_nodes),
        "close_nodes": len(close_nodes),
        "rewire_edges": len(rewire_edges),
    }


@mcp.tool()
def atp_apply_future_patch(
    plan_path: str,
    expected_graph_version: str,
    patch: Dict,
    reason: str,
    actor_id: str,
) -> str:
    """
    Applies a bounded future-graph patch when no nodes are currently claimed.
    """
    now = utc_now()
    with locked_graph(plan_path) as graph:
        current_graph_version = compute_graph_version(graph)
        if current_graph_version != expected_graph_version:
            raise ValueError(
                "Graph version mismatch. "
                f"expected_graph_version={expected_graph_version}, current_graph_version={current_graph_version}"
            )
        if claimed_node_count(graph) != 0:
            raise ValueError("Future patch apply requires zero_claimed nodes.")

        candidate = copy.deepcopy(graph)
        summary = apply_future_patch_to_graph(candidate, patch, reason.strip(), actor_id, now)
        validate_graph(candidate)
        graph.clear()
        graph.update(candidate)
        next_graph_version = compute_graph_version(graph)

    return (
        "Future patch applied. "
        f"graph_version: {expected_graph_version} -> {next_graph_version}. "
        f"add_nodes={summary['add_nodes']}, update_nodes={summary['update_nodes']}, "
        f"close_nodes={summary['close_nodes']}, rewire_edges={summary['rewire_edges']}."
    )


def summarize_status(graph: Dict) -> str:
    nodes = graph.get("nodes", {})
    counts: Dict[str, int] = {"LOCKED": 0, "READY": 0, "CLAIMED": 0, "COMPLETED": 0, "FAILED": 0}
    claimed: List[str] = []
    closed_count = 0
    for node_id, node in nodes.items():
        if node_is_closed(node):
            closed_count += 1
            continue
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
        f"Graph version: {compute_graph_version(graph)}",
        f"READY: {counts['READY']} | CLAIMED: {counts['CLAIMED']} | COMPLETED: {counts['COMPLETED']} | FAILED: {counts['FAILED']}",
    ]
    if closed_count:
        lines.append(f"Closed future nodes: {closed_count}")
    if claimed:
        lines.append("Claimed tasks:")
        lines.extend([f"- {entry}" for entry in claimed])
    ready = [nid for nid, node in nodes.items() if node["status"] == "READY" and not node_is_closed(node)]
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
        graph = load_graph(DEFAULT_PLAN_FILE)
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
        f"Graph version: {compute_graph_version(graph)}",
        f"{center}: {node['title']} [{node['status']}]",
        f"Instruction: {node['instruction']}",
        f"Dependencies: {', '.join(parents) if parents else 'None'}",
        f"Children: {', '.join(children) if children else 'None'}",
    ]
    if node_is_closed(node):
        lines.append(f"Future state: {node.get('future_state')}")
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
    graph = load_graph(resolve_paths(plan_path)[0])
    if view_mode == "full":
        return json.dumps(graph_with_runtime_meta(graph), indent=2)
    if view_mode == "local":
        if not node_id:
            raise ValueError("node_id is required for local view.")
        return render_local_view(graph, node_id)
    raise ValueError("view_mode must be 'full' or 'local'.")


if __name__ == "__main__":
    mcp.run()
