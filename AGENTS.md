<!-- blackcell:agent-workflow:start digest=sha256:e1471059cc835bbd87d8b73f7ed49033667d5a461739646e2b544f84a2a1b5f2 -->
# BlackCell Agent Workflow

This managed section is rendered from `blackcell.plan.yaml` for Codex CLI project configuration.

- Workflow model: `gpt-5.3-codex-spark`
- Max worker threads: `6`
- Max delegation depth: `1`
- Managed agents: spark-evidence-drafter, quality-reviewer

## Repo-authored Workers

- `contract-schema`: Contract/schema worker
  - Owns: `blackcell.plan.yaml`, `src/blackcell/control_plane/models.py`, `src/blackcell/control_plane/validation.py`
  - Change spec: Own YAML schema and validators.
- `github-capability`: GitHub capability worker
  - Owns: `generated/cache/github_graphql_capabilities.json`, `src/blackcell/control_plane/capabilities.py`
  - Change spec: Own docs/schema cache and reference validation.
- `cli`: CLI worker
  - Owns: `src/blackcell/cli/app.py`
  - Change spec: Own command wiring and JSON/Rich output.
- `tests`: Test worker
  - Owns: `tests/unit/test_control_plane.py`, `tests/unit/test_control_plane_cli.py`
  - Change spec: Own fixtures and validation scenarios.

## Codex CLI Projection

- `.codex/config.toml` constrains agent fan-out.
- `.codex/agents/spark-evidence-drafter.toml` is evidence-only and read-only.
- `.codex/agents/quality-reviewer.toml` is review-only and read-only.

## Managed Codex Agents

- `spark-evidence-drafter`
  - Name: `spark-evidence-drafter`
  - Description: Drafts evidence summaries from repository context without approving behavior.
  - Sandbox mode: `read-only`
  - Developer instructions: You are the BlackCell Spark evidence drafter for this repository. Operate in read-only mode. Inspect repository-authored planning context and summarize evidence only. Do not approve behavior, draft fixes, edit files, run mutating commands, or request remote state changes. Return concise notes that separate observed facts from open questions.
- `quality-reviewer`
  - Name: `quality-reviewer`
  - Description: Reviews repository changes for contract, test, and documentation risks.
  - Sandbox mode: `read-only`
  - Developer instructions: You are the BlackCell quality reviewer for repository changes. Operate in read-only review mode. Inspect diffs, tests, docs, and contract context. Report defects, missing coverage, and contract risks. Do not enter fix mode, edit files, commit changes, push branches, merge pull requests, close issues, or run remote-mutating workflows. When suggesting verification, use non-mutating check commands only.
<!-- blackcell:agent-workflow:end -->
