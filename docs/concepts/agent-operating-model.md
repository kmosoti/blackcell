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
verifier, and synthesizer nodes. Repository Codex collaboration is separate developer tooling
governed directly by `AGENTS.md` and `.agents/skills/`; it is not projected by the product CLI.
