---
name: blackcell-change
description: Implement or debug a nontrivial BlackCell change through a validated change spec, bounded optional Spark micro-edit, focused verification, and one applicable full gate. Use for implementation, debugging, refactoring, and migration work; do not use for trivial edits or read-only repository inventory.
---

# BlackCell Change

Keep the root on Terra for judgment, synthesis, and integration. Delegate only an already-localized
micro-edit whose paths, acceptance criteria, and focused test are known.

## Build The Contract

1. Inspect the relevant source, tests, contracts, and current diff before choosing a worker.
2. Derive a stable lowercase `work_id` and create
   `/tmp/blackcell-codex/<work-id>/{workers,results}`.
3. Write the shared intent once to `change-spec.json` using
   `.codex/orchestration/change-spec.schema.json`.
4. Record the exact current `base_sha`, acceptance criteria, path scope, constraints,
   assumptions, and unresolved unknowns.
5. Validate it:

```bash
python .codex/orchestration/validate_contract.py \
  change-spec /tmp/blackcell-codex/<work-id>/change-spec.json \
  --repo-root .
```

Do not delegate an invalid or incomplete contract.

## Choose The Execution Route

Stay Terra-direct when the change spans multiple behavioral boundaries, contains unresolved design
choices, or needs architecture, state, security, concurrency, replay, policy, or migration
judgment.

Before preparing a worker packet, inspect the live `spawn_agent` schema. If it does not expose
`agent_type`, do not delegate and do not use `task_name` as a substitute; continue the optional
micro-edit at the Terra root and record the capability fallback. Named agent configuration is the
normal source of model and reasoning settings. Do not pass direct `model`, `reasoning_effort`, or
`service_tier` overrides unless the user explicitly requested one.

Use one `k_spark_worker` only when all of the following hold:

- the edit is localized and text-only or mechanically bounded;
- `allowed_paths` is nonempty and exact;
- acceptance criteria are explicit;
- one focused verification command is known;
- the worker does not need to choose an architecture.

Only one micro-edit worker may write at a time. Pause root editing while it runs.
Capture `git status --porcelain=v2 --untracked-files=all` immediately before the spawn so the root
can attribute the worker's actual worktree delta instead of trusting `changed_files` alone.

## Hand Off Minimal Context

Create `workers/<worker-id>.json` from
`.codex/orchestration/worker-packet.schema.json`. Reference the shared change spec rather than
copying it. Include only assigned paths, required reads with reasons, exact argv verification
commands, the absolute worker-result schema path, and bounded result limits.

Validate the packet:

```bash
python .codex/orchestration/validate_contract.py \
  worker-packet /tmp/blackcell-codex/<work-id>/workers/<worker-id>.json \
  --repo-root .
```

Spawn with `agent_type = "k_spark_worker"` and `fork_turns = "none"` explicitly. Give the worker a
unique `task_name`; the task name is not an agent-type selector. Pass only this handoff:

```text
Read and validate the worker packet at:
  /tmp/blackcell-codex/<work-id>/workers/<worker-id>.json

Execute only that packet. Return JSON matching the declared result schema.
```

Treat an omitted `fork_turns`, `"all"`, or a positive turn count as a workflow defect. Stop that
worker and discard its result. Never paste parent turns, user conversation, transcripts, raw logs,
or broad file contents into the packet. Do not let a worker spawn children.

## Integrate And Verify

1. Wait for the worker, persist its JSON to `results/<worker-id>.json`, and close the worker.
   Capture the same porcelain status again. Reject an undeclared delta or any path outside the
   packet, even when the result omits it.
2. Validate the result against the packet:

```bash
python .codex/orchestration/validate_contract.py \
  worker-result /tmp/blackcell-codex/<work-id>/results/<worker-id>.json \
  --packet /tmp/blackcell-codex/<work-id>/workers/<worker-id>.json \
  --repo-root .
```

3. Review the actual diff and source-check consequential observations before accepting them.
4. Run focused checks first, then one applicable full gate from `blackcell.plan.yaml`.
5. Do not rerun an unchanged failure. Diagnose it, change the hypothesis or environment, retry
   once, then use a smaller alternative or report a concrete blocker.
6. Inspect `git diff` and `git status --short`. Complete normal Git delivery only when delivery is
   part of the task.
7. Delete the work directory only after every worker is closed and the result is integrated.
