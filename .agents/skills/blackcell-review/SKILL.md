---
name: blackcell-review
description: Independently review a consequential BlackCell change through a validated, read-only k_reviewer handoff and source-checked findings. Use for architecture, security, state, concurrency, policy, replay, migration, and regression review; do not use to implement fixes or for style-only review.
---

# BlackCell Review

Review completed work independently. Do not edit tracked files or fix findings.

## Define The Review

1. Inspect the request, current branch, status, diff or commit range, relevant specification, and
   tests. Establish the exact change boundary and acceptance criteria.
2. Create `/tmp/blackcell-codex/<work-id>/{workers,results}` and one `change-spec.json` using
   `.codex/orchestration/change-spec.schema.json`. Validate it before delegation.
3. Create one `review` packet using `.codex/orchestration/worker-packet.schema.json`. Assign the
   changed paths and required contract reads. Declare only read-only inspection and direct test,
   linter, type-check, or schema-check argv.
4. Capture `git status --porcelain=v2 --untracked-files=all`, then validate the packet.

## Run The Independent Review

Spawn exactly one `k_reviewer` with `fork_turns = "none"`. Pass only the packet path and canonical
handoff from `AGENTS.md`. The reviewer must not receive parent turns or spawn children.

Wait for completion, persist the returned JSON under `results/`, close the reviewer, and capture
the same status again. Treat any worktree delta as a workflow defect. Validate the result against
the packet and reject prose-only or schema-invalid output.

## Report Findings

- Source-check every consequential finding in the repository.
- Lead with actionable defects ordered by severity, with exact paths and lines.
- Include missing tests when they expose a behavioral risk.
- Preserve conflicts, unknowns, and blocked checks.
- If no findings survive source-checking, say so and state residual verification gaps.
- Do not approve, commit, publish, resolve review threads, or implement repairs.
- Delete the temporary work directory only after the reviewer is closed and synthesis is complete.
