---
name: blackcell-plan
description: Ground a BlackCell request against repository truth and produce a decision-complete, read-only implementation plan. Use for runtime-v1 rebaselining, next-node selection, architecture planning, migration planning, and requests to plan before editing; do not use for implementation or repository mutation.
---

# BlackCell Plan

Plan from the checked-out repository, not from stale conversation state.

## Ground The Work

1. Read `AGENTS.md`, `blackcell.plan.yaml`, the active specification, relevant source and tests,
   the migration ledger, the current branch and status, and the recent history affecting the task.
2. Confirm whether the request belongs to repository Codex tooling or the BlackCell runtime. Keep
   those boundaries separate.
3. Map completed evidence, current gaps, dependencies, compatibility constraints, and unresolved
   product decisions. Ask only for decisions that repository inspection cannot answer.
4. Do not edit tracked files, generate implementation artifacts, commit, push, or delegate writes.

## Produce The Plan

- Choose the smallest bounded change that advances the active program.
- Specify behavior and contracts before file inventories.
- Name public interface, schema, migration, replay, and compatibility effects when applicable.
- Include focused tests, one applicable full gate from `blackcell.plan.yaml`, and acceptance
  scenarios that distinguish contract-complete, integrated, and product-accepted states.
- Record assumptions and explicit non-goals. Preserve consequential unknowns instead of inventing
  policy.
- Return one concise `<proposed_plan>` block that another engineer can implement without making
  design decisions. Do not ask whether to proceed.
