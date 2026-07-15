---
node: targets/opencode
kind: retired-target
edges:
  retired-by:
    - spec/bcp-0034-evolutionary-runtime
---

# Retired OpenCode Agent-Pack Target

WP26 removed the tracked generated OpenCode agents and commands together with the source registry
that produced them. Blackcell no longer exposes an agent-pack render, install, drift, or doctor
surface.

This retirement does not prohibit OpenCode as a user-selected external development tool. It means
only that Blackcell does not own or generate OpenCode configuration and does not treat developer
tooling as a product runtime adapter. Provider authentication remains user-local and outside the
repository.
