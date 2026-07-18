---
name: blackcell-review
description: Independently review a consequential BlackCell change through a validated, read-only k_reviewer handoff and source-checked findings. Use for architecture, security, state, concurrency, policy, replay, migration, and regression review; do not use to implement fixes or for style-only review.
---

# BlackCell Review

Review completed work independently. Do not edit tracked files or fix findings.

Before creating review artifacts, inspect the live `spawn_agent` schema. If it does not expose
`agent_type`, report the independent review as `blocked` and name the missing selector. Do not
substitute `task_name`, a generic worker, or root self-review for `k_reviewer`. Its named agent
configuration is the source of model, reasoning effort, and sandbox; direct spawn overrides require
an explicit user request.

## Define The Review

1. Inspect the request, current branch, status, diff or commit range, relevant specification, and
   tests. Establish the exact change boundary and acceptance criteria.
2. Create `/tmp/blackcell-codex/<work-id>/{workers,results}` and one `change-spec.json` using
   `.codex/orchestration/change-spec.schema.json`. Validate it before delegation.
3. Create one `review` packet using `.codex/orchestration/worker-packet.schema.json`. Assign the
   changed paths and required contract reads. Declare only bounded `focused` tests and optional
   `static` linter, type-check, or schema-check argv. A review packet must not declare a `full`
   command, a repository-wide pytest invocation, or coverage collection.
4. Capture `git status --porcelain=v2 --untracked-files=all`, then validate the packet.

Review owns defect discovery, not acceptance certification. Prefer source inspection over command
volume. Normally declare one focused pytest command using `tools/run_pytest.py`. Use exact node IDs
with `--blackcell-require-all-pass`; omit that option for a bounded file selection because the
runner intentionally rejects non-node selections in required-pass mode. Add at most the static
checks that can materially confirm a suspected regression. Leave the maintained full gate to
`blackcell-verify`,
`blackcell-change`, or `blackcell-publish`.

## Run The Independent Review

Spawn exactly one `k_reviewer` with `fork_turns = "none"`. Pass only the packet path and canonical
handoff from `AGENTS.md`. The reviewer must not receive parent turns or spawn children.

Wait for completion, persist the returned JSON under `results/`, close the reviewer, and capture
the same status again. Treat any worktree delta as a workflow defect. Validate the result against
the packet and reject prose-only or schema-invalid output.

Static Ruff and ty checks may run concurrently with one focused pytest command when the execution
surface supports parallel calls. Do not run multiple pytest processes concurrently: the current
repository has no validated xdist/sharding contract for coverage data, pytest/Hypothesis caches,
integration ports, or external runtime resources.

## Report Findings

- Source-check every consequential finding in the repository.
- Lead with actionable defects ordered by severity, with exact paths and lines.
- Include missing tests when they expose a behavioral risk.
- Preserve conflicts, unknowns, and blocked checks.
- If no findings survive source-checking, say so and state residual verification gaps.
- Do not approve, commit, publish, resolve review threads, or implement repairs.
- Delete the temporary work directory only after the reviewer is closed and synthesis is complete.
