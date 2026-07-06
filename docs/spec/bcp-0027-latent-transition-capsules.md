---
node: spec/bcp-0027-latent-transition-capsules
kind: bcp
edges:
  depends-on:
    - spec/bcp-0026-telemetry-ledger
  implements:
    - spec/jepa-latent-prediction
---

# BCP-0027: Latent Transition Capsules

Goal: make BlackCell capable of encoding state, predicting next state, measuring
error, and revising from surprise.

## Scope

- latent domain models;
- deterministic/frozen encoder pipeline;
- transition-memory predictor baseline;
- latent prediction error metrics;
- local SQLite self-supervision sample ledger;
- CLI commands for encode, predict, error inspection, recording, ledger summary,
  and stats.

## Non-Goals

- trainable neural predictor;
- V-JEPA checkpoint integration;
- GPU-bound predictor training;
- opaque single-vector-only state.

## Acceptance

- A dry-run harness execution can produce `z_t`, an action, `z_hat_next`,
  `z_next`, prediction error, and a self-supervision sample.
- `blackcell latent record` can persist a simulated transition locally and
  `blackcell latent ledger` can summarize stored capsules.
- `blackcell latent predict --db <path>` can use stored transitions as
  non-parametric memory and only raise confidence for matching actions/states.
- `blackcell latent stats --db <path>` can summarize action-level sample counts,
  semantic error, surprise counts, and confidence labels.
- `blackcell latent predict --db <path>` includes those labels in prediction
  output so cold actions remain explicit planning risks.
- `blackcell harness run --runtime dry-run --latent-db <path>` records a latent
  transition and includes a compact latent summary in the run trace.
- `blackcell harness run --runtime dry-run --latent-db <path> --show-stats`
  folds action-level latent stats into the same run trace.
- `blackcell harness run --runtime dry-run --latent off|summary|record|stats`
  selects the latent harness policy explicitly.
- The predictor reports confidence and sample count.
- The implementation is explicitly labeled non-parametric and JEPA-inspired.
