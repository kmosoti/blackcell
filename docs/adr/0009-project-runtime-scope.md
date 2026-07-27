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

# ADR 0009: Rebaseline BlackCell Around One Project Runtime

- Status: Accepted
- Date: 2026-07-22
- Amended: 2026-07-27

## Context

Runtime foundation produced useful event, persistence, policy, scheduler, API, recovery, and replay
contracts. The later alpha program proved the daemon, client, worker, provider, review, and
verification boundaries, but it also duplicated earlier runtime implementations and embedded
maturity and generation labels in executable paths and symbols.

Source-bound release manifests, SBOMs, and verification bundles also made ordinary source and lock
changes invalidate historical evidence. That coupled incremental development to reissuing release
artifacts even when no release or persisted-contract decision existed.

The durable boundary is simpler: one long-running daemon owns state and orchestration; blocking work
stays off interactive UI threads; browser updates consume an ordered event channel; and an
operating-system service manager supervises the foreground process. Kernform exposes a closed
command envelope that lets BlackCell use its evolving implementation without coupling to its
internals.

## Decision

### Keep the Python modular monolith

BlackCell extends the current Python runtime in bounded slices. Existing contracts are reused only
after characterization; names, files, and old tests alone do not make retired behavior part of the
current runtime. BlackCell does not add its own Rust workspace or PyO3 layer for project
configuration.

### Make one daemon authoritative

One foreground daemon owns persisted state, scheduling, policy, provider dispatch, recovery, and
the ordered event stream. Project, intent, plan, run, event, replay, and browser contracts share the
public `/api/v1` boundary. Execution, review, and verification workers consume the same durable
state through separately configured capabilities rather than parallel runtimes.

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

The boundary pins Kernform `0.2.0`, `kernform.command/v2`, and machine-readable output. It executes
argv without a shell, enforces timeout/output limits, validates the closed response, maps stable
exit classes, and confines accepted artifacts to the requested project root. It supports read-only
`compile`, `check`, and effectful `init`; large raw `inspect` inventories are not admitted.

The envelope's generic `result` slot is not treated as trusted merely because the outer schema is
valid. BlackCell applies pinned command-specific contracts: `check` validates the documented
source, managed, or explicit legacy-migration shape; `compile` validates the plan, catalog,
signature closure, operation identities, and repository-relative paths; `init` validates its plan
identity and bounded operation count, then requires its state path to match the canonical accepted
artifact. This keeps evolving Python/Rust implementation
details behind Kernform's public wire contract without turning an open JSON object into an implicit
integration API.

BlackCell never imports a sibling Kernform checkout or its Python/Rust internals. A later Kernform
version requires an explicit compatibility decision and contract tests.

### Retire generation-coupled executable surfaces

The feedback workflow, parallel alpha runtime, compatibility writers, and their generation-named
packages are not part of the executable architecture. The retained project runtime uses semantic
capability names and one event ledger. Historical ADRs, specifications, decisions, and experiments
may retain the terminology needed to explain prior work, but they do not create imports, routes,
writers, aliases, or compatibility authority.

### Keep one active capability map

`../../blackcell.plan.yaml` is the active architecture, capability, and verification map. It names
project, intent, plan, run, execution, review, verification, and replay as distinct authority
boundaries without assigning a maturity or speculative generation to the implementation.

Ordinary iteration uses focused deterministic checks. Protected-branch CI runs formatting, lint,
architecture fitness, the complete maintained suite with its coverage floor, and type checking.
CI does not generate, compare, or require source-bound release evidence.

## Consequences

- The daemon can evolve independently from clients while retaining one state authority.
- CLI, TUI, and web behavior cannot drift into separate orchestration implementations.
- Service lifecycle follows platform supervision rather than bespoke background-process code.
- Kernform may evolve internally without creating a second configuration implementation in
  BlackCell.
- Historical documents remain available as context, but they cannot promote a deleted execution
  path or require regenerated release artifacts.
- Breaking persisted-state or external-protocol changes remain explicit operator decisions;
  internal iteration does not acquire generation labels merely because it changes over time.

## Rejected alternatives

- preserve a parallel alpha route or runtime beside the project-work service;
- repair or rename `DailyOperatorWorkflow` and treat it as the current product;
- gate implementation behind the superseded product-proof study;
- implement the GitHub epic 75 greenfield Rust/PyO3/custom-agent design;
- embed independent schedulers or state stores in the CLI, TUI, or web UI;
- implement custom double-fork or PID-file daemonization;
- import Kernform's sibling source tree or parse its human-oriented output;
- regenerate source-bound manifests, SBOMs, or verification bundles after ordinary source changes.

## Primary references

- [Docker Engine overview](https://docs.docker.com/engine/): daemon/client separation and a
  versioned API.
- [PyRatatui async updates](https://pyratatui.github.io/pyratatui/tutorials/async/): native terminal
  rendering on the asyncio thread with cooperative background updates.
- [Litestar channels](https://docs.litestar.dev/main/usage/channels.html): broker-backed event
  delivery to WebSocket clients.
- [systemd service units](https://www.freedesktop.org/software/systemd/man/systemd.service.html):
  supervision of a foreground service process.
