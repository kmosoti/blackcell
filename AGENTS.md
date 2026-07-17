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
| Commit and push | `$blackcell-publish` | Gate, commit selected paths, and push the active program branch declared in `blackcell.plan.yaml`. |

`/plan` is a built-in Codex mode and cannot be overridden by repository prompts. An approved plan
does not execute automatically; the next implementation request triggers `blackcell-change`.

Use the authenticated local `gh` CLI and its API surface for repository GitHub metadata. Prefer
native `gh issue`, `gh project`, and `gh api` operations and their readback output; do not route
BlackCell issue relationships, Project fields, or development branches through a connector.

## Hyper-critical multi-dimensional planning

Architecture, migration, runtime, release, and multi-issue work must be planned across five
reconciled planes. Do not reduce this to a linear checklist.

1. **Strategic** — state the product objective, success definition, preserved invariants, and
   explicit non-goals.
2. **Logistics** — backward-map from final acceptance, record dependencies, merge order,
   capacity limits, review checkpoints, and bounded delivery increments.
3. **Human** — name responsibilities for the program owner, implementer, reviewer, verifier, and
   affected operators; do not invent assignees or approval authority.
4. **Risk** — map authority, temporal, persistence, replay, recovery, policy, compatibility,
   security, and delivery constraints; record stop conditions that split work rather than hiding
   a scope expansion.
5. **Assessment** — define binary acceptance rules, advisory measures, direct evidence sources,
   verification commands, and feedback intervals.

Use backward mapping before choosing an implementation sequence. For every bounded work package,
record best-case, nominal, and failure scenarios. At each review checkpoint, test the plan against
new evidence, revise only the affected package, and preserve the reason for the change.

For a multi-issue program, the plan must also declare and validate a delivery-metadata map before
implementation: assignee, labels, Project/status/type, parent and sub-issue order, blocking graph,
and one integration branch. Planning reads current GitHub state and names the exact mutations and
readback checks; planning itself does not materialize remote metadata. If the available client
cannot perform a required native mutation, record it as blocked rather than substituting issue-body
text, task lists, or a similarly named branch for the missing relationship.

Plan from the active project and branch, not from a similarly named historical release or prior
delivery branch. Historical artifacts may inform constraints, but they do not become current
evidence unless the checked-out project explicitly carries and validates them.

## Delegation

Before nontrivial delegation, create and validate one shared change spec under
`/tmp/blackcell-codex/<work-id>/change-spec.json`. Create one validated packet per independent
worker under `workers/`, and persist returned JSON under `results/` before validating it.

The project explicitly enables MultiAgentV2, exposes its spawn metadata, and routes its tools
through the `agents` namespace. A configuration change does not alter an already-started thread's
tool schema. Start a fresh Codex session to expose `functions.agents__spawn_agent` and the related
V2 tools, then confirm that the live spawn schema includes `agent_type`. Named agent files are the
normal source of worker model, reasoning effort, sandbox, and instructions. Do not set the direct
`model`, `reasoning_effort`, or `service_tier` spawn fields unless the user explicitly requests
that override.

MultiAgentV2 collaboration tools are direct-model-only. Keep `non_code_mode_only = true` and do
not call `tools.agents__*` from inside `functions.exec`; Codex 0.144.1 does not preserve encrypted
message arguments through that nested path. Reconsider this restriction only after an upgraded
client passes both direct and nested encrypted-handoff controls.

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

- Keep normal root work, synthesis, integration, and Spark fallback on the checked-in Sol high
  default.
- Use `k_spark_worker` first only for already-localized text evidence or one localized micro-edit.
- Use `k_pr_explorer` when an ambiguous execution path survives targeted root inspection.
- Use `k_reviewer` for consequential architecture, security, state, concurrency, policy, replay,
  or migration changes.
- Use `k_verifier` to independently verify completed high-risk work without tracked-file edits.
- Use Sol high for ordinary root implementation and critical architecture work. Leave Sol xhigh
  unconfigured. Sol Ultra always requires an explicit user choice for exceptional full-repository
  architecture, review, or migration work.

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
