---
node: spec/bcp-0026-telemetry-ledger
kind: bcp
edges:
  precedes:
    - spec/bcp-0027-latent-transition-capsules
  supports:
    - spec/jepa-latent-prediction
---

# BCP-0026: Local Run/Event Ledger Foundation

Goal: introduce deterministic local run/event storage that can become the source
of truth for harness provenance and later latent transition samples. In this
document, "telemetry" means local evidence capture only; it does not imply remote
export.

## Scope

- run identifiers and event records;
- check outcomes and verification evidence;
- deterministic, idempotent local records for generic runs and events;
- enough provenance for latent prediction errors to point back to evidence.

## Non-Goals

- neural training;
- external vector or graph services;
- remote telemetry export by default.
- server, WAL, mmap, or streaming event infrastructure.

## Acceptance

- BlackCell can record a local run and its events.
- BlackCell can initialize and inspect a local SQLite ledger with `ledger init`,
  `ledger runs`, and `ledger events`.
- BlackCell can record dry-run harness traces with `harness run --ledger-db`.
- Later latent transitions can cite run/event evidence.
- Existing `world`, `nesy`, and `harness` checks still pass.
