---
node: guides/alpha-v2-kernel
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/alpha-operator-quickstart
    - guides/alpha-worker-configuration
    - guides/alpha-review-configuration
    - guides/alpha-verify-configuration
---

# Alpha v2 orchestration kernel

The alpha v2 kernel is the typed, host-authoritative layer for model-proposed project work. A
provider may suggest task text, dependencies, path scopes, and check identifiers selected from the
goal's host-owned catalog. It cannot supply verification argv. `compile_alpha_plan` owns admission
and rejects unknown fields, cycles, path or check expansion beyond the goal, ambiguous parallel
writers, and broken plan-version lineage before any repository action is authorized.

## Control flow

```text
AlphaGoalSpec
  -> AlphaPlanningProvider (proposal only)
  -> compile_alpha_plan (immutable PlanVersion N)
  -> AlphaV2PolicyKernel.authorize
  -> ProductionAlphaV2AttemptExecutor
  -> isolated Git worktree + inert change proposal + admitted text effects
  -> host-owned checks through Bubblewrap
  -> content-addressed verification evidence
  -> succeed | bounded repair | replan | block | escalate | terminal failure
```

Task attempts move through `pending`, `ready`, `running`, and `verifying` before reaching an
outcome. A repairable outcome may return to `ready`, but the goal and task limits cap attempts at
three. The coordinator, rather than the provider, derives progress from durable semantic evidence
digests. Two occurrences of the same normalized error signature without new evidence trip the
no-progress breaker. A repair attempt starts from the preceding failed attempt's committed head;
dependency tasks start from the admitted successful dependency head. A plan correction creates
`PlanVersion N+1` with an exact
`supersedes_plan_id`; admitted plans are never mutated in place.

## Durable state and replay

`EventBackedAlphaV2RunJournal` appends the load-bearing goal, plan, policy, attempt, verification,
and terminal events to the existing event store. `AlphaV2RunProjection` is the replay authority.
Checkpoints are non-authoritative acceleration snapshots, written every 100 events and at terminal
transitions; rehydration always applies the event tail after the checkpoint.

Every attempt gets a deterministic, unique workspace identity bound to its plan, task, and attempt.
Policy is recorded before the executor is called. A recovered run with an unresolved active
workspace escalates instead of guessing whether an external effect completed.

`AlphaWorkerProcess.from_config` composes the planner, change provider, worktree lifecycle,
Bubblewrap verifier, artifact store, event journal, checkpoints, policy kernel, and
`ProductionAlphaV2Kernel` from the same closed worker configuration and storage boundary. A plan
admitted through the existing `/api/alpha/v1/plans` endpoint with `planning_mode: "generated"`
enters this kernel when its run reaches the normal public worker queue. This does not create a
second scheduler, public transport, or state store.

## Provider boundaries

`GatewayAlphaPlanner` uses the generic gateway port and a closed JSON Schema. OpenAI/Codex and AGY
are adapters behind that port, not core dependencies. AGY is pinned to 1.1.7 and invoked with
stdin-triggered noninteractive mode plus `--mode plan --sandbox`; the explicit string-valued
`--print` flag is not used because it would expose the request in argv. AGY owns authentication for
its existing session; BlackCell accepts no credential path and never reads authentication material.
Subscription-backed AGY calls report token counts and cost as `null` when the provider does not
expose exact values.

The implementation provider remains separately replaceable. A provider response is an inert
proposal until deterministic admission, policy authorization, host-side application in an isolated
worktree, and verifier evidence accept it. The active worker configuration can select either
`agy-cli` or `codex-cli`; it never invokes Gemini CLI.

## Observability and promotion

`AlphaV2TraceObserver` maps durable event metadata to correlated spans containing run, plan, task,
workspace, and attempt identifiers. It does not export request, provider, credential, or repository
content. The production worker attaches this observer to the configured OTel recorder when export
is enabled, while the append-only event stream remains authoritative when export is disabled.
Repeated-error escalations can produce a typed `praxis-promotion-candidate/v1` containing only
durable event identifiers and digests; raw attempts are not promoted automatically.

The packaged alpha TUI presents four operator views: Runs, Task graph, Attempt detail, and Verifier
/ output. It projects the public daemon's typed run and replay contracts and does not require the
operator to parse raw logs. Run discovery uses the RFC 10008 `QUERY` method at
`/api/alpha/v1/run-query`, with a closed request media type, bounded scan/page limits, and ETag
revalidation. The query path is read-only and never calls a provider or advances a run.

Known model usage is accumulated against the admitted run budget. Provider values that are not
reported remain explicitly incomplete rather than becoming exact zeroes. Cancellation is checked
before and after a provider response, before repository effects and commits, and between sandboxed
checks. A provider subprocess already in flight is bounded by its deadline; cancellation prevents
later effects after it returns but does not claim provider-side interruption.

## Focused verification

```bash
uv run python tools/run_pytest.py -q tests/unit/test_alpha_v2_kernel.py
uv run python tools/run_pytest.py -q tests/integration/test_alpha_v2_production.py
uv run python tools/run_pytest.py -q tests/unit/test_agy_cli_model_adapter.py
uv run python tools/run_pytest.py -q tests/unit/test_alpha_tui_app.py tests/unit/test_alpha_tui_controller.py
uv run mutmut run
```

The maintained mutation target is intentionally narrow: it challenges alpha-v2 correlation and
exact-versus-unknown usage telemetry, an easy place to create misleading success metrics.
Repository-wide lint, type, and test gates remain required before publication.
