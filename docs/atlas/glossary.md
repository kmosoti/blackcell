---
node: atlas/glossary
kind: atlas
edges:
  defines:
    - concepts/world-model
    - concepts/nesy
    - concepts/custom-agents
---

# Glossary

- `world model`: explicit repository state made from observations, facts, beliefs, expectations, and surprises
- `NeSy`: neuro-symbolic seam; currently symbolic rules with room for learned predicates later
- `harness`: planner and trace loop that treats runtimes as interchangeable adapters
- `runtime adapter`: local or remote execution surface behind the harness
- `agent pack`: shippable set of BlackCell-flavored agents and commands rendered into a target format
- `scope`: install target for generated config, either repo-local `project` or user-local `global`
