ATP Librarian MCP — Agent Rules
===============================

Audience
--------
Agents using the existing MCP server. Do **not** start or restart the server; just call the tools.

Plan basics (no external schema needed)
---------------------------------------
- The plan is a JSON DAG (`.atp.json`) with `nodes` keyed by ID.
- Each node includes: `title`, `instruction`, `dependencies` (array of parent IDs), `status` (`LOCKED|READY|CLAIMED|COMPLETED|FAILED`).
- Optional fields: `context`, `reasoning_effort` (`minimal|low|medium|high|xhigh`), `artifacts` (paths you touched), `report` (handoff), `started_at`, `completed_at`.
- A node becomes `READY` only when all dependencies are `COMPLETED`.
- If a task is decomposed, it becomes a `SCOPE` and auto-completes after its child tasks complete.
- Choose the `CODEX` worker_id

Working rules
-------------
1) Always pass the correct ABSOLUTE `plan_path` to every tool call.
2) Never edit the plan file directly; the server handles locking and validation.
3) When you finish a task, call `atp_complete_task` with a concise **report** (2–4 sentences) that captures requirements, decisions, and next steps, and list any `artifacts` (files you modified/created). Downstream agents rely on these reports as their requirements.
4) When you claim a task, read the parent reports included in the assignment; treat them as the authoritative handoff/context you must follow.
5) If a task is too big, call `atp_decompose_task` to split it; the parent will auto-complete when children are done.

Tool quick reference
--------------------
- `atp_claim_task(plan_path, agent_id)`: Recover stale claims, then CLAIM the highest-priority READY node. Returns instruction plus parent reports.
- `atp_complete_task(plan_path, node_id, report, artifacts=[], status="DONE")`: Mark DONE/FAILED, clear lease, and READY children whose deps are satisfied.
- `atp_decompose_task(plan_path, parent_id, subtasks)`: Split a task into a subgraph; parent becomes a SCOPE and auto-closes when children finish.
- `atp_read_graph(plan_path, view_mode="full"|"local", node_id=None)`: Return full plan JSON or a neighborhood view.
- Resource: `atp://status/summary` gives a read-only snapshot for the default plan configured on the server side.
