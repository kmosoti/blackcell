---
node: concepts/custom-agents
kind: historical-concept
edges:
  retired-by:
    - spec/bcp-0034-evolutionary-runtime
---

# Retired Generated Agent Pack

The July 6 source-owned agent registry and generated OpenCode prompt pack were retired in WP26.
They depended on the prototype world, NeSy, harness, and adapter surfaces and were not part of the
Blackcell runtime or model gateway.

Repository contributor guidance now lives only in `AGENTS.md`. It does not configure the Blackcell
product runtime, and no Blackcell CLI command projects or installs coding-assistant configuration.

Historical role names and generated prompt bodies are intentionally not kept as executable
artifacts. The pre-retirement inventory is retained in
`../../experiments/legacy_retirement/wp26-characterization.json`.
