---
node: architecture
kind: architecture
edges:
  implements:
    - charter
  constrained-by:
    - scientific-basis
    - epistemic-evaluation
  decided-by:
    - adr/0004-evolutionary-runtime-architecture
    - adr/0005-durable-run-and-execution-protocol
    - adr/0006-run-feedback-protocol-evolution
    - adr/0007-runtime-security-boundary
    - adr/0008-architecture-consolidation
    - adr/0009-project-runtime-scope
    - adr/0010-semantic-presentation
---

# Runtime architecture

## Shape

BlackCell is a modular monolith with inward dependency direction and an event-sourced kernel. One
foreground service owns durable project work. The CLI, terminal UI, and browser UI are projections
over that service. They do not own orchestration, providers, or persistence.

```text
interfaces -> bootstrap -> orchestration -> gateway -> kernel
                    |             |
                    +-> adapters -+
```

- `kernel` owns immutable events, artifacts, projections, SQLite setup, and integrity errors.
- `gateway` owns provider admission, route selection, budgets, output validation, and audit records.
- `orchestration` owns project-work contracts and the execution, review, verification, and replay
  state machines.
- `adapters` own bounded external processes, Git worktrees, isolation, HTTP clients, telemetry,
  recovery, and third-party command protocols.
- `bootstrap` is the only concrete composition layer.
- `interfaces` own closed HTTP, CLI, terminal, and browser contracts.

Architecture tests classify every package root, constrain dependency direction, prohibit imports
of retired implementations, and reject maturity or speculative generation labels in executable
paths and symbols.

## Process model

`blackcell-runtime daemon` supervises the API and only the workers enabled by explicit
configuration:

- `api` serves health, authenticated project-work contracts, and UI assets;
- `execution-worker` claims and executes dependency-ready plan nodes;
- `review-worker` produces independently admitted review findings;
- `verification-worker` deterministically adjudicates acceptance and epistemic evidence.

A component exit stops its siblings with bounded graceful and forced cleanup. On Linux, an optional
systemd user service supervises the same foreground process. Installation references an existing
owner-only environment file, writes no credentials, and does not start the service implicitly.

With no worker configuration the API still admits projects, intents, plans, and queued runs. It
does not infer a provider, executable, or worker identity from ambient state.

## Public contracts

The service exposes closed request and response contracts beneath `/api/v1`:

- project registration;
- intent and plan acceptance;
- asynchronous run submission;
- safe `QUERY` filtering plus status and cancellation;
- ordered event pages with monotonic cursors;
- live-free replay;
- same-origin browser tickets and read-only event streaming.

The route revision is an explicit public protocol boundary. Request bodies are bounded, duplicate
JSON keys are rejected, unknown fields fail closed, authentication precedes protected work, and
responses are typed and size limited. Credentials come only from environment or an existing
owner-only token file; they are never accepted as command arguments or emitted in errors.

The browser shell is public and data-free at `/ui`; fixed assets live at `/ui/assets`. Runtime data
requires authentication. The browser keeps credentials only in memory, uses same-origin requests,
and validates its own response bindings. The ordered event socket is an invalidation signal, not a
second state authority: clients refetch a deterministic presentation surface after its cursor
advances.

## Semantic presentation

The daemon projects canonical project-work contracts into one strict, renderer-neutral Pydantic
surface. The closed component catalog covers sections, status, metrics, key/value facts, tables,
typed forms, plan graphs, timelines, findings, epistemic evidence, verified artifacts, and explicit
source disclosure. Stable component IDs, source digests, revision cursors, and field dispositions
make meaning independently comparable across clients. Every canonical request field is either
editable, displayed, derived, or hidden with a reason; a client cannot silently omit a field such
as planning mode.

The browser uses native modules, custom elements, semantic HTML, and SVG. A plan graph always has
an adjacent data table, and raw JSON is secondary disclosure rather than the primary interface.
The native Rust terminal uses Ratatui and the same closed surface; it owns neither a projection
database nor a durable cursor. Both clients submit only the fixed host-declared action catalog.
Codex and Agy inspection remains lossless: common facets, material differences, and every leaf of
each strict tooling model are projected separately.

Presentation tokens use the Design Tokens Community Group JSON shape so visual refinement can
iterate without changing domain meaning. An A2UI-compatible export is a pure adapter for
interoperability experiments; it is not accepted as executable agent-authored UI and does not
replace the canonical presentation contract.

## Project, intent, plan, and run admission

Project registration binds the service's canonical repository root and a project-configuration
identity. Intent records the objective, constraints, assumptions, and unresolved questions. Plan
admission requires an acyclic graph, deterministic topological order, one base commit, explicit
budgets and effects, repository-relative allowed paths, and host-owned direct-argv acceptance
commands.

Repository writers must be dependency ordered. The service rejects unknown fields, cycles,
undeclared effects, path escape, ambiguous writers, inconsistent identities, and idempotency
conflicts before queueing work. Run submission durably records a queued event and returns before an
execution worker claims it.

## Execution

The execution worker uses a fenced lease and one isolated Git worktree per attempt. The host records
worktree preparation and provider-dispatch intent before crossing those effect boundaries. Provider
output is a closed text-change proposal; host code validates evidence identity, allowed paths,
operation shape, change budgets, and resulting Git state before running acceptance commands.

Acceptance commands name administrator-owned executable aliases resolved by the isolation adapter
to canonical paths. Bubblewrap and process resource limits constrain the check environment. This is
a bounded local execution contract, not a claim of arbitrary hostile-code containment.

If a process stops after recorded provider dispatch and before a durable result, the run requires
explicit reconciliation. The worker cannot prove whether the external process accepted the call and
does not repeat it automatically. Worktree cleanup uses the same durable intent/result pattern.

## Review

Review starts only from a digest-checked successful execution event. A separate worker owns the
review lease and invokes one review-only provider through a bounded context. It has no execution,
worktree, shell, acceptance, or publication port.

Review output is admitted only when its schema, context digest, evidence citations, finding links,
and complete epistemic matrix are valid. Structural admission is not approval; it proves only that
the proposal is bounded and tied to admitted evidence.

## Verification

Verification is deterministic and has no model provider. It binds the accepted plan, terminal
execution evidence, admitted review, and artifact digests into a row-oriented matrix. Every
acceptance criterion and every epistemic dimension has one outcome and reason code. Concerns fail;
missing evidence remains unknown and makes the result inconclusive; not-applicable requires an
explicit reason.

This stage guards against acceptance gaps, unsupported claims, counterevidence omission, causal
overreach, scope drift, and false certainty. It does not guarantee that host-authored criteria or
tests are themselves correct, so the final human acceptance boundary remains explicit.

## Replay

Replay folds the exact immutable event sequence and verifies every referenced artifact. It checks
project, intent, plan, run, attempt, review, and verification identities and digests without calling
a provider, executing a command, or changing a repository. Missing, malformed, reordered, or
tampered evidence fails closed.

## Persistence and recovery

SQLite uses one kernel database for events, artifacts, checkpoints, and idempotency. Initialization
is transactional. A fresh database is created directly at the current persisted schema. Any
nonempty database with another schema is rejected by a read-only preflight before WAL setup or any
other mutation. BlackCell does not migrate or delete incompatible state.

Backup creates an immutable bundle containing the database, artifact inventory, digests, schema,
and event high-water mark. Restore verifies the bundle before a non-destructive cutover. Retention
applies only to verified bundles.

## Telemetry and quotas

Telemetry records bounded semantic event attributes and sanitizes secret-bearing content before
export. The exporter endpoint, queue, batch, delay, and timeout are explicitly configured. Request
rate, storage ceilings, artifact limits, and mutation reserve are enforced at admission; quota
failure cannot bypass authorization or corrupt existing state.

## Project configuration

Kernform is an isolated third-party command boundary. BlackCell probes the supported installed
package, invokes agent-mode JSON through argv-only subprocesses, validates the closed wire envelope,
bounds execution and output, and never imports Kernform internals. Exact package and wire revisions
remain visible because interoperability depends on them.

## Continuous verification

CI runs formatting, linting, architecture fitness, the full pytest coverage gate, static type
checking, Rust formatting/lint/tests, browser contract tests, three-engine Playwright flows with
automated accessibility checks, and a clean installed-wheel smoke test. One synthetic review
scenario carries renderer-independent semantic expectations into Python, browser, and terminal
checks. Ratatui buffers are deterministic snapshots; browser traces and screenshots are retained
briefly only when the UI gate fails.

CI does not generate source-bound evidence that ordinary incremental work must continually
reissue, and no real run trace or screenshot is committed. Executable naming and retired-import
guards run on every change, so old architectural labels and paths cannot silently return.
