---
node: spec/jepa-latent-prediction
kind: spec
edges:
  implements:
    - concepts/world-model
  informs:
    - spec/bcp-0027-latent-transition-capsules
  researched-by:
    - research/world-models
---

# JEPA-Inspired Latent Prediction

> **Superseded by BCP-0028 through BCP-0033.** This file is retained as prototype history.
> The deterministic sketch is an experimental baseline and no longer defines product
> behavior.

BlackCell's intended world-model behavior is latent state-transition prediction:

```text
raw evidence
  -> deterministic/frozen encoders
  -> structured latent state z_t
  -> action-conditioned predictor
  -> predicted next latent state z_hat_t+1
  -> actual encoded next state z_t+1
  -> latent prediction error
  -> surprise / revision / self-supervision sample
```

## V0 Contract

V0 is JEPA-inspired, not a true trained JEPA. It must:

- encode inspectable channels rather than collapse everything into one opaque
  vector;
- use deterministic/frozen feature extraction;
- predict with non-parametric transition memory and explicit confidence;
- persist enough transition samples to support future training decisions;
- store V0 transition samples in a local SQLite ledger before any remote or
  trainable pipeline exists;
- hydrate the non-parametric predictor from local ledger transitions when a
  ledger path is supplied;
- summarize local prediction quality into action-level labels: `cold`,
  `warming`, and `grounded`;
- attach those labels back to candidate predictions so planning can distinguish
  unseen actions from actions with local transition evidence;
- avoid Torch/JAX, optimizer state, checkpoints, or learned projection heads.

## Latent Capsule Channels

- `semantic`: stable digest/vector sketch over durable summaries and facts.
- `structural`: repository, graph, and fact-count features.
- `telemetry`: run/check/status scalars available at the current slice.
- `policy`: runtime, action, sandbox, and verification posture.
- `symbolic`: NeSy-derived status masks and expectation/surprise counts.

## Domain Objects

- `LatentState`: encoded representation of current world/run state.
- `LatentAction`: canonical action being considered.
- `LatentPrediction`: predicted next latent state plus confidence and evidence.
- `LatentTransition`: links from state, action, prediction, actual state, and
  outcome.
- `LatentPredictionError`: structured mismatch summary.
- `SelfSupervisionSample`: future training/evaluation sample, collected without
  requiring training now.

## Research Boundary

I-JEPA, V-JEPA, V-JEPA 2, AMI world-model framing, Graph-JEPA, time-series JEPA,
and related work justify prediction in latent representation space. They do not
mean BlackCell V0 embeds any external checkpoint or implements a trained JEPA.
