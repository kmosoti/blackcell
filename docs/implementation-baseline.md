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
with public liveness/readiness and an owner-only SQLite file. Granian process lifecycle, the
non-root image, OTel export, persistent-volume deployment, and recovery evidence remain measured
work, not assumed capabilities.

## Preserved contracts

Until a work package explicitly replaces them with tested compatibility:

- the `blackcell` CLI remains usable;
- `RepositoryOperator` remains a compatibility facade;
- recorded and deterministic model paths remain network-free;
- replay never invokes a live model or action adapter;
- human corrections append evidence instead of rewriting history;
- current public event and context contracts remain readable;
- the 151-test suite and 86% combined coverage form the initial quality floor.

## Known debt accepted at baseline

- overlapping runtime and persistence implementations;
- orchestration concentrated in two large modules;
- 23 SQLite `ResourceWarning` instances under strict warning reporting;
- no architectural dependency tests;
- no first-class model gateway;
- no Litestar/Granian operator API;
- no production-shaped rootless Podman image;
- no durable multi-agent DAG scheduler, leases, or fencing tokens.

No production behavior changes are included in WP00.
