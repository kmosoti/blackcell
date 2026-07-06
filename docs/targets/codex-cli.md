---
node: targets/codex-cli
kind: target
edges:
  legacy-of:
    - legacy/control-plane
  alternative-to:
    - targets/opencode
---

# Codex CLI Target

Codex CLI remains as legacy control-plane projection context and optional runtime
availability reporting. It is no longer the center of the root README or the
agent-pack design.

Existing managed files such as `.codex/config.toml`, `.codex/agents/*.toml`, and
managed sections in `AGENTS.md` are still handled by the old control-plane agent
workflow commands.
