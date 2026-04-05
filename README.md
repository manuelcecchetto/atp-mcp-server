ATP Librarian MCP Server
========================

This server enforces the Agent Task Protocol (.atp.json) contract so worker agents can claim, finish, and decompose tasks without corrupting the dependency graph. It exposes MCP tools that wrap deterministic updates to the plan file on disk.

Quick start (no clone, uvx)
---------------------------

Add to your MCP client config and update the plan path:

```toml
[mcp_servers.atp]
command = "uvx"
args = ["--from", "git+https://github.com/Edgeworthless/atp-mcp-server", "atp-server"]
env = { "ATP_FILE" = "/path/to/your/.atp.json", "ATP_LEASE_SECONDS" = "600" }
```

`uvx` downloads/builds on the fly. Tools still require `plan_path` per call; `ATP_FILE` is just a default.

Local install
-------------

1. `pip install -e .`
2. Run: `python3 main.py`
3. Configure your MCP client to call that command, and pass `plan_path` on every tool call.

Docker (optional)
-----------------

Build or pull, then mount your plan file:

```bash
docker build -t atp-librarian .
docker run --rm -v /path/to/.atp.json:/data/.atp.json -e ATP_FILE=/data/.atp.json atp-librarian
```

Environment knobs
-----------------

- `ATP_FILE`: path to the ATP plan file (default `.atp.json`).
- `ATP_LOCK_FILE`: override the lock file path (defaults to `<ATP_FILE>.lock`).
- `ATP_LEASE_SECONDS`: duration before a claimed task is released (default 600s).
- Every mutating tool appends an audit event to `<plan>.audit.jsonl`.
- Tools accept `plan_path` directly; the env vars mainly backstop the status resource or when clients omit the argument.

Available tools
---------------

- `atp_claim_task(plan_path, agent_id)`: Assigns the highest-priority READY node, recovers zombies, and returns dependency context. Re-enters an existing claim for the same agent.
- `atp_complete_task(plan_path, node_id, agent_id, report, artifacts=[], status="DONE")`: Marks a CLAIMED task DONE or FAILED, clears the lease, and unlocks children whose dependencies are satisfied. The caller must own the active claim.
- `atp_decompose_task(plan_path, parent_id, subtasks, agent_id=None)`: Converts a task into a SCOPE with a new subgraph. Subtasks must form a DAG, and every subtask ID must be unique across the full plan. If the parent is already `CLAIMED`, the caller must provide the matching `agent_id`. Start nodes inherit the parent’s original dependencies.
- `atp_reset_task(plan_path, node_id, agent_id, reason)`: Resets an owned FAILED task back to READY for retry.
- `atp_cancel_task(plan_path, node_id, agent_id, reason)`: Cancels an owned CLAIMED or FAILED task and leaves its dependents blocked until the graph is revised.
- `atp_read_graph(plan_path, view_mode="full" | "local", node_id=None)`: Returns the full JSON graph or a neighborhood view around `node_id`.

Resource
--------

- `atp://status/summary`: Plaintext dashboard with project status, live claims, and ready tasks for the default plan (`ATP_FILE` or `.atp.json` in the current working directory).

Graph contract
--------------

- Keep the plan as a valid task DAG: each node has `title`, `instruction`, `dependencies`, `status` (`LOCKED|READY|CLAIMED|COMPLETED|FAILED|CANCELLED`), plus optional `priority` (integer), `owner_id`, `context`, `reasoning_effort` (`minimal|low|medium|high|xhigh`), `artifacts`, `report`, timestamps. Plans at meta version `1.5` remain readable alongside `1.3` and `1.4`.
- Node statuses: `LOCKED` (waiting), `READY`, `CLAIMED`, `COMPLETED`, `FAILED`, `CANCELLED`. The server will auto-release stale CLAIMED tasks after the lease window and recompute SCOPE nodes deterministically: `CLAIMED` if any child is pending, `COMPLETED` if all children complete, `FAILED` if all terminal children include a failure, and `CANCELLED` if all terminal children are cancelled without failures.
- Reads and writes validate the whole plan DAG, so cycles are rejected on load and on mutation.
- READY task selection is sorted by `(-priority, len(dependencies), node_id)`.
- `atp_decompose_task` rejects any subtask ID already present anywhere in the plan.
- Legacy `FAILED` tasks from older plans can be adopted on the first reset/cancel call by the acting `agent_id`, so `1.3`/`1.4` plans remain operable without manual migration.
- Audit entries include actor information for completion/reset/cancel and record scope state transitions.
- Reads and writes are guarded by the same file lock, and plan writes use a `.tmp` file plus `os.replace()` for atomic replacement.
- Dependency integrity is checked on every read/write; missing node references will raise errors instead of silently mutating the file.

Testing
-------

- `uv run --with pytest pytest`
- `python3 -m compileall main.py tests/test_main.py`

MCP client config example
-------------------------

Example JSON config (Codex-compatible):

```json
{
  "mcpServers": {
    "atp-librarian": {
      "command": "python3",
      "args": ["/path/to/atp-mcp-server/main.py"],
      "cwd": "/path/to/atp-mcp-server",
      "env": {
        "ATP_FILE": "/path/to/your/project/.atp.json",
        "ATP_LEASE_SECONDS": "600"
      }
    }
  }
}
```

Tool calls should still provide `plan_path` (e.g., `/path/to/your/project/.atp.json`); `ATP_FILE` just sets a default for the status resource or when a client omits the argument.
