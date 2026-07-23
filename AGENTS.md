# BlackCell Engineering Guide

## Product boundary

BlackCell is a CLI-first, project-scoped agentic framework for turning software requests into
explicit intent, dependency-safe work, isolated execution, durable evidence, review, and verified
outcomes. Preserve the existing Python runtime and its event, replay, recovery, policy, API, and
worker contracts unless an accepted project decision changes them.

Keep business rules behind typed application and domain boundaries. The CLI, future TUI, and web
client are projections over the same versioned service contracts; they do not own orchestration or
persistence rules. CLI output is JSON by default. Model providers are replaceable adapters without
ambient repository or publication authority.

Kernform integration belongs behind a pinned, typed project-generation and conformance boundary.
Do not import an unpinned sibling checkout at runtime or couple BlackCell behavior to Kernform's
implementation language.

## Repository guidance

This `AGENTS.md` is the repository's only coding-assistant configuration. Do not add repository-local
`.codex/`, `.agents/`, model-selection, named-agent, or orchestration configuration. Product adapters
that invoke Codex or another provider are BlackCell runtime code and are not developer-tool config.

Inspect the current branch, status, relevant source, tests, and public contracts before editing.
Preserve unrelated dirty work. Prefer small, typed, reversible changes and avoid dependency or
framework growth without a concrete product need.

Use Python 3.14 and `uv`. Use `uv run python tools/run_pytest.py` instead of invoking pytest
directly so repository test invariants and the owner-write umask are applied consistently.

## Fast development loop

Optimize the local loop for fast feedback:

1. Run the exact affected test nodes first:

   ```text
   uv run python tools/run_pytest.py path/to/test.py::test_name -q --blackcell-require-all-pass
   ```

2. Run Ruff only on changed Python paths while iterating:

   ```text
   uv run ruff check path/to/changed.py path/to/test_changed.py
   uv run ruff format --check path/to/changed.py path/to/test_changed.py
   ```

3. When a change crosses several core surfaces, use this small smoke set before handoff:

   ```text
   uv run python tools/run_pytest.py \
     tests/unit/test_cli_output.py::test_output_renderer_serializes_runtime_types \
     tests/unit/test_cli_output.py::test_bench_list_jsonl_outputs_one_record_per_line \
     tests/unit/test_cli_output.py::test_bench_list_renders_rich_when_requested \
     tests/unit/test_run_grammar_v2.py::test_run_start_requires_typed_protocol_version \
     tests/unit/test_orchestration_scheduler.py::test_submit_is_content_idempotent_and_reconstructs_after_restart \
     tests/unit/test_http_api.py::test_health_routes_are_public_and_openapi_is_not_exposed \
     tests/unit/test_service_auth.py::test_bearer_authentication_returns_one_typed_principal \
     -q --blackcell-require-all-pass
   ```

Do not run coverage or the complete pytest suite during ordinary iteration. CI owns broad regression
coverage; run the full suite locally only for an explicitly requested release/publication gate or
when focused evidence cannot bound the change. Run one pytest process at a time.

## Scope and delivery

Do not weaken tests, public contracts, replay compatibility, policy checks, or expected outputs to
obtain a pass. Do not touch secrets, credentials, lockfiles, or unrelated generated artifacts unless
the task explicitly requires them. Commit, push, merge, release, and deployment remain separate
actions and require explicit user direction.
