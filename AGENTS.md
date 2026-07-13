# BlackCell Codex Workflow

## Boundary

Repository `.codex/`, `.agents/skills/`, and this file configure Codex as a developer tool. They
do not configure the BlackCell runtime, model gateway, OpenCode compatibility pack, or product
orchestration. Do not project these files from `blackcell.plan.yaml` or `src/blackcell/agents`.

Explore repository truth before asking a discoverable question. Preserve existing contracts and
unrelated work. Use one writer and bounded workers; zero workers is the default for trivial work.

## Repository workflow

On mobile, select **Custom** manually when the checked-in model, reasoning, and developer setup is
required. The mobile collaboration-mode picker is client-owned; repository config does not choose
its default mode. Do not add a speculative collaboration-mode key to `.codex/config.toml`.

Use the repository skills as the command-like lifecycle:

| Intent | Invocation | Effect |
| --- | --- | --- |
| Ground and plan | `/plan`, then `$blackcell-plan` when explicit routing is useful | Read repository truth and return a decision-complete plan without edits. |
| Implement an approved plan | `Implement the proposed plan` or `$blackcell-change` | Use the validated change workflow; a separate implementation skill is not needed. |
| Map independent evidence | `$blackcell-spark-sweep` | Run bounded read-only evidence shards. |
| Review consequential work | `$blackcell-review` | Hand one validated read-only packet to `k_reviewer`. |
| Verify completed high-risk work | `$blackcell-verify` | Hand declared acceptance checks to `k_verifier` without tracked edits. |
| Commit and push | `$blackcell-publish` | Gate, commit selected paths, and normally push `agent/runtime-v1`. |

`/plan` is a built-in Codex mode and cannot be overridden by repository prompts. An approved plan
does not execute automatically; the next implementation request triggers `blackcell-change`.

## Delegation

Before nontrivial delegation, create and validate one shared change spec under
`/tmp/blackcell-codex/<work-id>/change-spec.json`. Create one validated packet per independent
worker under `workers/`, and persist returned JSON under `results/` before validating it.

The project explicitly enables MultiAgentV2 and exposes its spawn metadata. A configuration change
does not alter an already-started thread's tool schema; start a fresh Codex session before relying
on newly exposed fields. Before delegation, confirm that the live `spawn_agent` schema exposes
`agent_type`. Named agent files are the normal source of worker model, reasoning effort, sandbox,
and instructions. Do not set the direct `model`, `reasoning_effort`, or `service_tier` spawn fields
unless the user explicitly requests that override.

If `agent_type` is absent, never substitute `task_name`, a generic worker, or root self-review.
Optional delegation stays on the Terra root and records the capability fallback. A requested Spark
sweep or any workflow requiring independent review or verification stops as `blocked` and names
the missing selector.

Every spawn must:

- set `agent_type` explicitly to `k_spark_worker`, `k_pr_explorer`, `k_reviewer`, or
  `k_verifier`; a similar-looking `task_name` does not select the custom agent;
- pass `fork_turns = "none"` explicitly;
- pass only the worker-packet path and one short imperative;
- reuse an existing worker for a small follow-up and close it after collecting the result.

Omitting `fork_turns`, using `"all"`, or using a positive turn count is a workflow defect.
`fork_turns = "none"` removes parent conversation turns; it does not remove base system, tool,
custom-agent, or repository instructions. Workers must not spawn children.

Canonical handoff:

```text
Read and validate the worker packet at:
  /tmp/blackcell-codex/<work-id>/workers/<worker-id>.json

Execute only that packet. Return JSON matching the declared result schema.
```

Do not copy user conversations, previous agent transcripts, raw logs, or broad file contents into
packets. Pass paths, symbols, invariants, acceptance criteria, assigned evidence, and exact argv
verification commands. Worker verification argv is limited to direct tests, linters, schema
checks, and read-only repository inspection; never declare a shell, nested publisher, deployment,
or destructive command. Source-check consequential worker findings before synthesis.

Use at most four workers without confirmation only when the work has independent shards. Five to
eight workers require explicit user instruction and must be read-only. Permit only one micro-edit
worker at a time; pause root editing while it writes. Never run an eight-worker write workflow.
Capture `git status --porcelain=v2 --untracked-files=all` immediately before spawning. Capture it
again after workers close; any repository delta from a read-only worker is a workflow defect, and
every writer delta must match its validated result and allowed paths.

## Routing

- Keep normal root work, synthesis, integration, and Spark fallback on Terra high.
- Use `k_spark_worker` first only for already-localized text evidence or one localized micro-edit.
- Use `k_pr_explorer` when an ambiguous execution path survives targeted root inspection.
- Use `k_reviewer` for consequential architecture, security, state, concurrency, policy, replay,
  or migration changes.
- Use `k_verifier` to independently verify completed high-risk work without tracked-file edits.
- Use Sol medium only as a deliberate root override for intentionally Sol-based ordinary
  implementation. Use Sol high through `k_reviewer` or an explicit critical root session. Leave
  Sol xhigh unconfigured. Sol Ultra always requires an explicit user choice for exceptional
  full-repository architecture, review, or migration work.

## Authorization

Reversible completion work that advances the requested task is authorized: tracked workspace
edits, generated caches, focused tests, dependency setup, reversible local Git operations, normal
commits and non-force pushes, and ordinary GitHub delivery when delivery is part of the request.

Require the user for irreversible actions, including destructive history or ref deletion, force
push, pull-request merge, release or package publication, user-data deletion, secrets/access
changes, production effects, and destructive migrations without a tested rollback. Never hide a
gated action inside `uv run`, a shell wrapper, a repository script, an alias, or an alternate Git
refspec. Prefix rules are not a substitute for this invariant.

## Completion

Do not rerun an unchanged failure. Diagnose it, alter the hypothesis or environment, retry once,
then use a smaller alternative or report a concrete blocker. Run focused verification first, then
one applicable full gate from `blackcell.plan.yaml`. Finish with diff and status inspection, leave
unrelated untracked files untouched, delete Codex-created packet directories only after all
workers close, and stop when the requested outcome is complete.
