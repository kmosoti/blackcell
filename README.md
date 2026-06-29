# BlackCell

BlackCell is a Python-first planning SDK and covert-themed CLI. It validates
local planning directives, materializes approved work into Linear, records
recoverable operations in an append-only chronicle, and verifies read-only
GitHub Issue echoes created by Linear's native integration.

The initial proof deliberately excludes code execution, branch and pull-request
automation, GitHub mutations, webhooks, plugins, and additional planning
backends.

## Requirements

- Python 3.14.6
- [`uv`](https://docs.astral.sh/uv/)
- A planner-scoped `LINEAR_API_KEY` for remote Linear commands
- GitHub CLI authentication or a read-only `GITHUB_TOKEN` for echo verification

## Quick start

```bash
uv sync
uv run blackcell profile validate
uv run blackcell pulse
uv run blackcell directive validate path/to/plan.json
uv run blackcell operation inspect BCP-0001
uv run blackcell operation reconcile BCP-0001
uv run blackcell operation verify BCP-0001
uv run blackcell publication preflight --stage commit
```

Text is the default format in a terminal; piped output defaults to JSON.
Use `--format text`, `--format json`, or `--format jsonl` to override it.
Set `BLACKCELL_EVENTS=jsonl` to emit redacted operation events to stderr.

## Authority boundaries

- Linear owns planning state and approval.
- GitHub owns repositories, code review, and merge authority.
- BlackCell owns local validation, canonical digests, deterministic markers,
  idempotent materialization, recovery, and anomaly detection.

BlackCell never writes GitHub resources during this proof. The Linear API key is
read from the environment and is never included in configuration, logs,
chronicle events, exceptions, or subprocess environments.

Before commit, push, or pull-request work, `publication preflight` verifies the
configured executor identity, branch namespace, commit author, push target, and
active GitHub/PR identity for the selected stage. The workflow is read-only; it
does not commit, push, create a PR, approve, or merge.
