---
name: blackcell-verify
description: Independently verify a completed high-risk BlackCell change through a validated k_verifier handoff, declared checks, and acceptance evidence without tracked-file edits. Use before submission or publication when correctness, state, replay, security, migration, or policy confidence needs an independent gate; do not use to repair failures.
---

# BlackCell Verify

Verify the completed artifact against declared acceptance criteria. Do not reinterpret the target.
Do not edit tracked files.

Before creating verification artifacts, inspect the live `spawn_agent` schema. If it does not
expose `agent_type`, report the independent verification as `blocked` and name the missing selector.
Do not substitute `task_name`, a generic worker, or root self-verification for `k_verifier`. Its
named agent configuration is the source of model, reasoning effort, and sandbox; direct spawn
overrides require an explicit user request.

## Define The Verification

1. Inspect the accepted plan or change specification, current branch, status, diff or commit range,
   relevant contracts, and existing test evidence.
2. Convert each material acceptance criterion into an observable check. Include adversarial cases
   required by the active specification, not merely happy-path unit tests.
3. Create and validate `/tmp/blackcell-codex/<work-id>/change-spec.json` and one `verify` worker
   packet. Declare exact direct argv for focused checks and one applicable full gate from
   `blackcell.plan.yaml`; never declare a shell, publisher, deployment, or destructive command.
4. Capture `git status --porcelain=v2 --untracked-files=all` before delegation.

## Run The Independent Gate

Spawn exactly one `k_verifier` with `fork_turns = "none"` and the canonical minimal handoff from
`AGENTS.md`. Wait for it, persist its JSON result, close it, and capture status again. Generated
test caches are acceptable; any tracked-file delta is a workflow defect.

Validate the result against the packet. Do not rerun an unchanged failure. If a retry is justified,
change the hypothesis or environment once; otherwise report the concrete blocker.

## Report Evidence

- Source-check consequential observations and command summaries.
- Map every acceptance criterion to pass, fail, or blocked evidence.
- Distinguish contract-complete, integrated, and product-accepted evidence.
- Report the smallest next action for failures without implementing it.
- Do not commit, push, publish, approve, or repair the change.
- Delete the temporary work directory only after verification is synthesized.
