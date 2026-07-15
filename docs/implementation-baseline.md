---
node: implementation-baseline
kind: architecture-baseline
edges:
  governed-by:
    - charter
    - architecture
  informs:
    - migration-ledger
    - spec/index
---

# Evolutionary Runtime Baseline

This document freezes the starting point for Blackcell's evolutionary-runtime migration. It is
an observation, not a target architecture. Measurements are also recorded in
`experiments/baseline/wp00.json` so later work packages can compare against a stable baseline.

## Captured state

- Date: 2026-07-10
- Remote base: `main` at `1dcc0e0621bd91665fdac19b49926e7154a90126`
- Local source tree: `73857bf1a30cb0aec5cbcfdc03ff0b712c61e5aa`
- Python: 3.14.2
- SQLite: 3.50.4
- Test result: 151 passed
- Line and branch coverage: 86.49% combined, 90.10% statements, 71.36% branches
- Static checks: Ruff and ty pass

The reproducible verification commands are:

```shell
uv run pytest -q
uv run coverage run -m pytest -q
uv run coverage report
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

## Runtime generations found

The repository contains two useful but overlapping generations:

1. The repository-operator runtime in `kernel`, `domains/repository`, `context`, `control`,
   `models`, `operator`, `evaluation`, and `telemetry` implements the current end-to-end loop.
2. The earlier research prototype in `world`, `latent`, `nesy`, `harness`, `runtime`, `ledger`,
   and `agents` contains transition, constraint, simulation, and adapter experiments that must be
   migrated behind the new boundaries rather than extended in place.

The largest coordination points are `operator/service.py` and `cli/app.py`. They remain supported
facades while behavior is extracted into feature slices.

## Persistence and replay baseline

Three SQLite-backed persistence paths exist: `kernel`, `ledger`, and `latent`. The kernel event
store is the only path eligible to become the authoritative write model. Migration must not add
dual writes. The ledger and latent stores may be read through compatibility adapters until their
data is migrated or their experiments are retired.

The event kernel provides envelopes, artifacts, projections, checkpoints, atomic batches, and a
caller-owned in-transaction append used by the SQLite adapter session. The orchestration adapter
now adds content-idempotent run submission and attempt outcomes, durable leases, fencing, retry
backoff, approval decisions, recovery, and restart reconstruction without creating a second write
model. A generic transport inbox and remote worker dispatcher are not part of this local scheduler.

## Deployment baseline

The service now has framework-neutral security configuration for an explicit owner-only data root,
opaque environment-or-file credential, strict Bearer and scope checks, zero proxy trust, and
pre-storage redaction. A Litestar/msgspec `/api/v1` edge composes those checks over canonical
observation, operator run, context, replay, evaluation, event, and scheduler approval use cases,
with public liveness/readiness and an owner-only SQLite file. The `blackcell-runtime` entry point
now runs that edge through one bounded Granian ASGI worker or runs a durable scheduler worker over
the reviewed five-role Repository Operator DAG. Both modes consume the same explicit security and
data configuration; API backpressure, leases, polling, and graceful shutdown are bounded. The
canonical workflow now emits stable, correlated, pre-export-redacted spans through an opt-in
OpenTelemetry OTLP/HTTP adapter with bounded asynchronous batching and process-owned shutdown.
One multi-stage OCI image now runs both API and worker under numeric non-root identity, and the
rootless Podman Compose contract adds loopback publication, health sequencing, read-only roots and
repository access, dropped capabilities, runtime-only token injection, and durable named-volume
state. WP22b now adds a consistent SQLite-plus-artifact recovery bundle, strict independent
verification, non-destructive restore, verified-only retention, global protected-request admission,
active-storage reserve, and an exact serialized artifact ceiling. An external-copy drill proves
restore and live-free replay after source-state loss; offsite transport and host capacity monitoring
remain deployment responsibilities.

## Preserved contracts

Until a work package explicitly replaces them with tested compatibility:

- the `blackcell` CLI remains usable;
- `RepositoryOperator` remains a compatibility facade;
- recorded and deterministic model paths remain network-free;
- replay never invokes a live model or action adapter;
- human corrections append evidence instead of rewriting history;
- current public event and context contracts remain readable;
- the 151-test suite and 86% combined coverage form the initial quality floor.

## WP26 retirement delta

WP26 closed the temporary preservation boundary after a retained 106-test characterization. The
prototype `world`, `nesy`, `harness`, `latent`, generic `ledger`, and generated `agents` packages,
their public CLI commands, and the tracked OpenCode projections are removed. The legacy runtime
adapter-discovery service and predecessor Repository Operator/Daily Operator v1 writers are also
removed. `blackcell.runtime` now retains only canonical quota contracts.

The kernel database and its owned journals/projections are the only runtime write authority.
Immutable `daily-operator/v1` histories remain readable through the same live-free replay use case
as v2; their writer is not public or composed. The exact before/after evidence is recorded in
`experiments/legacy_retirement/wp26-characterization.json` and the WP26 decision.

## WP27 release-evidence delta

WP27 closes the runtime-v1 DAG with an unpublished evidence bundle under `release/runtime-v1/`.
The maintained guide and isolated recorded-model example cover the canonical product and replay
path. A deterministic CycloneDX 1.7 pre-build SBOM derives the transitive non-development Python
runtime closure from `uv.lock`; it does not inventory an unbuilt container or host.

The verification manifest records the complete declared candidate-material inventory with modes,
sizes, and SHA-256 digests, binds the SBOM and retained WP25/WP26 evidence, and stores exact argv for
locked setup, static checks, the full suite, the recorded example, and the opt-in rootless gate. Its
verifier regenerates both documents in memory and fails closed on byte or material drift. No build,
publication, signing, attestation, or vulnerability result is included.

## Known debt accepted at baseline

- overlapping runtime and persistence implementations;
- orchestration concentrated in two large modules;
- 23 SQLite `ResourceWarning` instances under strict warning reporting;
- remaining dependency debt is tracked in `architecture/dependency_debt.json`;
- no published, signed, or attested runtime image;
- no deployed OpenTelemetry collector or container telemetry composition;
- no automated offsite backup transport, encrypted bundle format, or filesystem/cgroup hard quota;
- no fault-injected power-loss claim for untested storage hardware.

No production behavior changes are included in WP00.
