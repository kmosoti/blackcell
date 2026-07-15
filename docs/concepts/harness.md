---
node: concepts/harness
kind: historical-concept
edges:
  retired-by:
    - spec/bcp-0034-evolutionary-runtime
  replaced-by:
    - architecture
---

# Retired Prototype Harness

The July 6 dry-run harness combined repository observation, generated agent plans, a generic event
store, and deterministic latent-transition sketches. It was valuable characterization evidence,
but it created coordination and persistence paths separate from the canonical kernel.

WP26 removed the harness and its public commands after the gateway-owned Daily Operator v2,
durable role DAG, independent outcome evaluation, and live-free replay paths were accepted. No
compatibility shim or dual write remains. Historical design context survives in the superseded
BCP-0026/0027 documents and the WP26 characterization artifact.
