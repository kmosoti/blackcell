---
node: spec/index
kind: spec-entry
edges:
  maps:
    - spec/jepa-latent-prediction
    - spec/bcp-0026-telemetry-ledger
    - spec/bcp-0027-latent-transition-capsules
  informs:
    - concepts/world-model
    - concepts/harness
---

# BlackCell Spec

This section turns generated planning dumps into durable, reviewable BlackCell
specification notes. The source v0.4 dump is not committed; this curated spec is
the repository reference.

## Current Sequence

1. `BCP-0026`: telemetry and durable ledger foundation.
2. `BCP-0027`: JEPA-inspired latent transition capsules.

The latent layer is intended BlackCell behavior, but the first implementation is
non-training-first: deterministic/frozen encoders plus transition memory before
any learned predictor.
