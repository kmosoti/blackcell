---
node: concepts/traces
kind: concept
edges:
  produced-by:
    - concepts/harness
  revises:
    - concepts/world-model
---

# Traces

Run traces are the audit surface for harness activity.

The first implementation emits small `TraceEvent` records from the dry-run
adapter. The longer-term purpose is to capture enough normalized execution state
to update beliefs, detect surprises, and replay or evaluate agent behavior across
runtime targets.
