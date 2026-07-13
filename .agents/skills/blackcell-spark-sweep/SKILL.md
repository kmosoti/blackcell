---
name: blackcell-spark-sweep
description: Run a read-only, minimal-context BlackCell repository sweep with validated shared intent, independent evidence shards, structured worker results, and root source-checking. Use for repository inventory, code search, diff or test impact, documentation drift, API cataloguing, duplication candidates, and other naturally shardable evidence work; never use it as a parallel write workflow.
---

# BlackCell Spark Sweep

Use workers to isolate independent evidence lanes, not to multiply file-by-file overhead. Keep the
root responsible for shard design, conflicts, validation, and synthesis.

## Define The Sweep

1. Inspect enough repository truth to identify independent evidence questions.
2. Create `/tmp/blackcell-codex/<work-id>/{workers,results}` and one shared
   `change-spec.json` using `.codex/orchestration/change-spec.schema.json`.
3. Give each shard a bounded question and disjoint package, subsystem, documentation family, or
   test surface. Do not default to one worker per file.
4. Validate the change spec with `.codex/orchestration/validate_contract.py`.

Use zero workers for a small targeted inspection. Use up to four workers without asking when the
shards are genuinely independent. Use five to eight only after explicit user instruction. Every
five-to-eight-worker sweep is read-only. Never create an eight-worker write workflow.

Before creating worker packets, inspect the live `spawn_agent` schema. If it does not expose
`agent_type`, stop the worker sweep as `blocked` and name the missing selector. Do not substitute
`task_name`, generic workers, or a root-only scan for the requested independent sweep. Named agent
configuration is the normal source of model and reasoning settings; direct `model`,
`reasoning_effort`, and `service_tier` overrides require an explicit user request.

## Route Each Shard

Use `k_spark_worker` in `evidence` mode for already-localized searches, inventories, catalogs, and
drift checks. Use `k_pr_explorer` only when a shard must trace an ambiguous execution path that
targeted root inspection could not resolve. Use `k_reviewer` only for a specifically declared
consequential review packet, not ordinary sweep volume.

Create one packet per shard using `.codex/orchestration/worker-packet.schema.json`. Reference the
shared change spec and include only allowed and forbidden paths, required reads with reasons, exact
argv commands, the absolute result schema path, and result limits. Validate every packet before
spawning.

## Spawn Without Parent Turns

Every spawn must set `agent_type` explicitly to the selected named agent and set
`fork_turns = "none"` explicitly. Give each worker a unique `task_name`; a task name is not an
agent-type selector. Pass only:

```text
Read and validate the worker packet at:
  /tmp/blackcell-codex/<work-id>/workers/<worker-id>.json

Execute only that packet. Return JSON matching the declared result schema.
```

An omitted `fork_turns`, `"all"`, or a positive turn count is a workflow defect. Stop that worker
and discard its result. Do not pass conversation history, previous agent transcripts, raw logs,
broad file contents, or another shard's evidence. Workers must not spawn children or write tracked
files.

Capture `git status --porcelain=v2 --untracked-files=all` before the first spawn and after every
worker closes. Reject the sweep if the repository snapshots differ; result `changed_files` is not
proof that a read-only worker preserved the worktree.

## Collect And Synthesize

1. Wait for all requested workers. Persist each JSON return under `results/`, validate it against
   its packet, and close that worker.
2. Reject prose-only, oversized, out-of-scope, or schema-invalid results.
3. Deduplicate observations by claim and evidence location. Preserve contradictory observations
   as explicit conflicts and keep unknowns visible.
4. Source-check consequential claims in the repository before using them in the synthesis.
5. Report evidence, conflicts, and remaining unknowns without generic architecture prose or raw
   command output.
6. Delete the generated work directory only after every worker is closed and the synthesis is
   complete.
