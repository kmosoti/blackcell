---
node: concepts/world-model
kind: concept
edges:
  produces:
    - concepts/nesy
  informs:
    - concepts/harness
  researched-by:
    - research/world-models
---

# World Model

BlackCell uses a compact world model for software work rather than treating the
repository as unstructured prompt context.

## Entities

- `Observation`: raw evidence gathered from the repository or runtime surface
- `Fact`: typed symbolic statement derived from observation
- `Belief`: a higher-level interpretation supported by facts
- `Expectation`: a predicted or desired state
- `Surprise`: a mismatch between what should be true and what was observed

## Current Slice

The current implementation observes:

- repository structure such as `README.md`, `docs`, `src`, `tests`, and `pyproject.toml`
- git workspace cleanliness
- availability of optional runtimes

This is intentionally minimal. The next steps can extend the model to include:

- file ownership zones
- issue or task bindings
- verification outcomes
- contradiction tracking
- provenance and confidence updates over time

## Intended Latent Prediction Behavior

The world model is expected to grow into a JEPA-inspired latent transition layer.
The first usable slice encodes current world state into an inspectable latent
capsule, predicts next latent state for candidate actions using transition
memory, then compares the prediction with the next observed capsule. The error
becomes surprise, revision, and self-supervision evidence.

This is intended product behavior, but V0 is non-training-first and does not
claim to be a true trained JEPA.
