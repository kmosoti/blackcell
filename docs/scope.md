---
node: scope
kind: scope
edges:
  governed-by:
    - adr/0009-project-runtime-scope
  constrains:
    - charter
    - architecture
    - evaluation-methodology
---

# BlackCell Alpha Scope

## Product definition

BlackCell is a **CLI-first, project-scoped agentic framework** for turning a software request into
explicit intent, bounded evidence, a dependency-safe plan, isolated execution, review, verified
outcomes, and replayable records.

The alpha is local-first. One long-running daemon owns project state, scheduling, persistence,
policy, provider dispatch, recovery, and the ordered event stream. The CLI is the complete
automation surface. The PyRatatui TUI and Litestar web UI are clients of the same versioned service;
they are not additional runtimes.

[`../alpha.plan.yaml`](../alpha.plan.yaml) is the active delivery program.

## Current boundary

The repository contains substantial runtime-v1 foundations: immutable events and artifacts,
SQLite persistence, typed policies, scheduler leases and fencing, an HTTP/process boundary,
recovery, and live-free replay. Those contracts may be reused after focused characterization.

The current `RepositoryOperator` and `DailyOperatorV2Workflow` do not constitute the alpha project
loop. They are retained only as historical migration and replay evidence. In particular, the
legacy synchronous `/api/v1/runs` route is not an alpha submission path. The A03 core instead uses
closed `/api/alpha/v1` contracts to persist projects, intents, plans, and queued runs, then exposes
status, a resumable ordered event cursor, and live-free replay. It does not yet dispatch a provider,
claim work, execute a command, or mutate a repository by itself. An opt-in alpha worker now claims
dependency-ready nodes from the same ledger, but the daemon composes it only when an external
owner-only closed configuration explicitly fixes the provider and Bubblewrap boundaries. With no
such configuration the daemon is API-only and runs remain queued; it never falls back to the
historical V2 worker.

## Alpha contract

The target loop has explicit authority-bearing stages:

1. **Project configuration** checks or initializes a project through a pinned Kernform contract.
2. **Intent** records the requested outcome, constraints, assumptions, and unresolved questions.
3. **Evidence** binds repository facts and omissions to stable identities and budgets.
4. **Plan** defines a typed acyclic graph, allowed effects, recovery rules, and acceptance checks.
5. **Verification before execution** rejects invalid dependencies, authority expansion, and
   untestable outcomes.
6. **Execution** runs only approved work in a recoverable isolated worktree.
7. **Review** searches for correctness, regression, policy, replay, and specification-gaming
   defects without changing the acceptance contract.
8. **Outcome verification** maps declared results to source and runtime evidence.
9. **Replay** reconstructs accepted state without invoking live providers or repeating effects.

Models propose and synthesize inside this loop. They never become the state store, policy engine,
executor, approver, or outcome authority.

## Service and client boundary

The daemon runs in the foreground. On Linux, an optional systemd user service supervises that
process; other platforms use a documented foreground command until a native supervisor adapter is
earned. BlackCell does not implement double-fork daemonization or treat a PID file as authority.

All clients use one typed API under the `/api/alpha/v1` namespace and one ordered event stream:

- the CLI provides complete JSON-first automation and recovery commands;
- the PyRatatui TUI keeps native rendering on the event-loop thread and schedules controller work
  through bounded non-blocking tasks;
- the web UI consumes ordered updates through Litestar channels and WebSockets;
- no client imports daemon persistence or embeds another scheduler.

The initial HTTP slice registers the daemon's canonical project root, accepts explicit constraints,
assumptions, and unresolved questions, validates plan dependencies, budgets, effects, and checks,
and durably queues a run with HTTP 202. Event pages use the immutable ledger's global position as
their cursor and omit legacy payloads. Replay verifies the referenced event identities and digests
before rebuilding the accepted project, intent, plan, and queued status without live calls.

## Kernform boundary

Kernform is the project configuration and scaffolding provider. BlackCell invokes its installed
CLI with argv only; it does not import a sibling checkout or depend on Kernform's internal Python
or Rust implementation.

The first adapter accepts exactly Kernform `0.1.0` and `kernform.command/v1`, probes with
`kernform --agent --version`, and invokes `check` or `init` with
`kernform --agent --format json`. It enforces wall-clock and output budgets, validates the closed
response envelope, maps stable exit classes, and persists the version plus request/result digests.
Raw `inspect` output is deferred because a large repository inventory can exceed the alpha
adapter's bounded output contract.

## Delivery boundary

The alpha sequence is deliberately compact:

`rebaseline -> daemon and Kernform -> alpha contracts -> isolated execution -> review and verify
-> TUI and web -> real-project proof`

The former intent/review issues and observability, scaffolding, and greenfield-rewrite epics
described older overlapping or conflicting programs. After the local contract passed, they were
closed as not planned in favor of native alpha epic
[#91](https://github.com/kmosoti/blackcell/issues/91). Their useful scope is absorbed into A01,
A03, A05, A07, and A08; speculative scaffold search is deferred beyond alpha.

## Fast verification

Ordinary development runs exact affected pytest nodes and Ruff only on changed Python paths. One
fast repository-wide Ruff check is the milestone gate. CI owns broad regression coverage and type
checking. A local full suite is reserved for release/publication or a change whose risk cannot be
bounded by focused evidence.

## Non-goals

The alpha does not authorize:

- a greenfield rewrite of the accepted Python runtime;
- a BlackCell-owned Rust or PyO3 configuration layer;
- repository-local named-agent, model-selection, or Codex orchestration configuration;
- distributed queues, Kubernetes, or a visual workflow builder;
- online self-rewriting, automatic production deployment, or ambient provider authority;
- claims that legacy V2 behavior is the alpha project workflow.

## Promotion rule

A work package advances only when its declared focused checks pass and its dependencies are
terminal. A model result, human preference, benchmark headline, or legacy test pass cannot waive a
typed contract, replay, recovery, or outcome-verification failure.
