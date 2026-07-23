---
node: concepts/agent-operating-model
kind: historical-concept
edges:
  retired-by:
    - spec/bcp-0034-evolutionary-runtime
  replaced-by:
    - concepts/custom-agents
---

# Retired Generated-Agent Operating Model

This document formerly specified prompt sections for the generated OpenCode agent pack. WP26
retired that pack and its prototype world/NeSy/harness dependencies.

Blackcell's product roles are now typed orchestration contracts for planner, executor, reviewer,
verifier, and synthesizer nodes. Repository contributor guidance lives only in `AGENTS.md`; it is
not projected by the product CLI and does not configure the runtime.
