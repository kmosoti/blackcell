---
node: spec/bcp-0026-telemetry-ledger
kind: bcp
edges:
  precedes:
    - spec/bcp-0027-latent-transition-capsules
  supports:
    - spec/jepa-latent-prediction
---

# BCP-0026: Telemetry and Ledger Foundation

Goal: introduce durable run/event storage that can become the source of truth for
latent transition samples.

## Scope

- run identifiers and event records;
- check outcomes and verification evidence;
- deterministic, idempotent local records for V0 latent samples;
- enough provenance for latent prediction errors to point back to evidence.

## Non-Goals

- neural training;
- external vector or graph services;
- remote telemetry export by default.

## Acceptance

- BlackCell can record a local run and its events.
- Later latent transitions can cite run/event evidence.
- Existing `world`, `nesy`, and `harness` checks still pass.
