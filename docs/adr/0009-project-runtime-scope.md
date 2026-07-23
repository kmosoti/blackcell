---
node: adr/0009-project-runtime-scope
kind: decision
edges:
  governs:
    - scope
    - charter
    - architecture
  supersedes:
    - concepts/custom-agents
---

# ADR 0009: Rebaseline BlackCell Around One Alpha Daemon

- Status: Accepted
- Date: 2026-07-22

## Context

Runtime-v1 produced useful event, persistence, policy, scheduler, API, recovery, and replay
contracts, but its `DailyOperatorV2Workflow` is not a reliable basis for the desired project-work
product. A newly added daemon submission client would have reached that workflow through the
synchronous legacy `/api/v1/runs` route and therefore exposed the wrong execution path.

Later planning attempts also conflicted with the current direction. One blocked all delivery behind
a human-use study. Other open epics split observability and adaptive scaffolding into competing
programs. GitHub epic 75 prescribed a greenfield Rust/PyO3 rewrite and repository-defined custom
agents. None matches the repository-owner decision to push the existing project quickly toward an
alpha with only `AGENTS.md` as repository contributor configuration.

The reusable runtime and UI patterns are well established: a long-running daemon owns state behind
a versioned client API; blocking work stays off an interactive UI thread; browser updates consume
an ordered event channel; and an operating-system service manager supervises the foreground
process. Kernform already exposes a closed agent-mode command envelope that lets BlackCell use its
evolving Python/Rust implementation without coupling to those internals.

## Decision

### Keep the Python modular monolith

BlackCell extends the current Python runtime in bounded slices. Existing contracts are reused only
after characterization; names or tests alone do not make legacy behavior part of the alpha.
BlackCell does not add its own Rust workspace or PyO3 layer for project configuration.

### Make one daemon authoritative

One foreground daemon owns persisted state, scheduling, policy, provider dispatch, recovery, and
the ordered event stream. The alpha contracts live under `/api/alpha/v1` so they cannot be confused
with the legacy synchronous run route.

On Linux, an optional systemd user service supervises the foreground process. Portable use starts
the process directly. BlackCell does not implement double-fork daemonization, PID-file authority,
or an embedded scheduler in any client.

### Treat CLI, TUI, and web as clients

The JSON-first CLI is the complete automation and recovery surface. A PyRatatui TUI and Litestar
web UI use the same typed client. The native terminal remains on the asyncio event-loop thread while
the controller offloads synchronous client calls and the shell schedules bounded non-blocking tasks.
WebSocket or channel consumers resume from an ordered event cursor rather than reading mutable
storage.

### Integrate Kernform through its public command contract

The first boundary pins Kernform `0.1.0`, `kernform.command/v1`, and agent-mode JSON output. It
executes argv without a shell, enforces timeout/output limits, validates the closed response, maps
stable exit classes, and confines accepted artifacts to the requested project root. It initially
supports `check` and `init`; large raw `inspect` inventories are not admitted.

The envelope's generic `result` slot is not treated as trusted merely because the outer schema is
valid. BlackCell applies pinned command-specific contracts: `check` validates its exact conformance
flags, catalog identity, mode, bounded file count, and deterministic requirement identifiers;
`init` validates its plan identity and bounded operation count, then requires its state and evidence
paths to match the canonical accepted artifacts. This keeps evolving Python/Rust implementation
details behind Kernform's public wire contract without turning an open JSON object into an implicit
integration API.

BlackCell never imports a sibling Kernform checkout or its Python/Rust internals. A later Kernform
version requires an explicit compatibility decision and contract tests.

### Retain V2 only as evidence

`DailyOperatorV2Workflow` remains readable for migration, historical replay, and extraction of
useful contracts. No new CLI, TUI, web, or daemon alpha command may invoke it. A03 defines a
separate asynchronous `/api/alpha/v1` contract whose submission ends after durably recording
`alpha.run.queued`; it never delegates to the synchronous legacy route. Provider dispatch and
execution are deferred to A04.

### Use one compact alpha program

`../../alpha.plan.yaml` is the single active program. A00 through A08 cover rebaseline, daemon and
Kernform boundaries, alpha run contracts, isolated execution, review/verification, TUI, web, and
real-project proof. The prior product-proof plan, scope-realignment plan, and GitHub epic 75 DAG are
superseded.

Ordinary iteration uses exact pytest nodes and changed-path Ruff checks. A fast repository-wide
Ruff check is the milestone gate; broad coverage and type checks remain CI or release gates.

## Consequences

- The daemon can evolve independently from clients while retaining one state authority.
- CLI, TUI, and web behavior cannot drift into separate orchestration implementations.
- Service lifecycle follows platform supervision rather than bespoke background-process code.
- Kernform may evolve internally without creating a second configuration implementation in
  BlackCell.
- Historical V2 data remains available, but passing its tests cannot promote its execution path.
- The first alpha work is contract and lifecycle work, not a broad rewrite.

## Rejected alternatives

- continue exposing `/api/v1/runs` as the alpha submission route;
- repair or rename `DailyOperatorV2Workflow` and treat it as the new product;
- gate implementation behind the superseded product-proof study;
- implement the GitHub epic 75 greenfield Rust/PyO3/custom-agent design;
- embed independent schedulers or state stores in the CLI, TUI, or web UI;
- implement custom double-fork or PID-file daemonization;
- import Kernform's sibling source tree or parse its human-oriented output;
- run full coverage on every local edit.

## Primary references

- [Docker Engine overview](https://docs.docker.com/engine/): daemon/client separation and a
  versioned API.
- [PyRatatui async updates](https://pyratatui.github.io/pyratatui/tutorials/async/): native terminal
  rendering on the asyncio thread with cooperative background updates.
- [Litestar channels](https://docs.litestar.dev/main/usage/channels.html): broker-backed event
  delivery to WebSocket clients.
- [systemd service units](https://www.freedesktop.org/software/systemd/man/systemd.service.html):
  supervision of a foreground service process.
