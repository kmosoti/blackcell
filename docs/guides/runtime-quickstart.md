---
node: guides/runtime-quickstart
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/execution
    - guides/execution-worker-configuration
    - guides/review-configuration
    - guides/verification-configuration
---

# Runtime quickstart

This is the shortest source-checkout path through BlackCell's project, intent, plan, run,
execution, review, verification, and replay contracts. The daemon owns durable state and
orchestration. The JSON-first CLI, terminal UI, and browser UI are clients of the same service.

The workflow does not build a package, create a tag, publish a release, or deploy a service. The
checked request files are templates, not a runnable project or live-provider proof.

## Prepare the checkout

BlackCell requires Git, Python 3.14, and `uv`.

```bash
uv sync --locked --all-groups
uv run blackcell --help
```

Create an owner-only data directory and provide a nonempty opaque API token. The repository root
must be the canonical absolute path to the project that runs are allowed to use.

```bash
install -d -m 700 /ABSOLUTE/OWNER/ONLY/PATH/blackcell-data
export BLACKCELL_DATA_DIR=/ABSOLUTE/OWNER/ONLY/PATH/blackcell-data
export BLACKCELL_REPOSITORY_ROOT=/ABSOLUTE/PATH/TO/PROJECT
export BLACKCELL_API_TOKEN='REPLACE_WITH_AN_OPAQUE_TOKEN'
export BLACKCELL_RUNTIME_ENDPOINT=http://127.0.0.1:8080
```

Worker configuration is opt-in and fail closed. Set only the configuration files for the
capabilities that should run:

- `BLACKCELL_EXECUTION_CONFIG_FILE`: provider, worktree, isolation, executable, and resource
  authority;
- `BLACKCELL_REVIEW_CONFIG_FILE`: a separate review-only provider and worker identity;
- `BLACKCELL_VERIFICATION_CONFIG_FILE`: deterministic verification worker identities and timing.

Each file must be an absolute canonical path outside the managed repository, owned by the service
user, and mode `0600`. With no worker configuration the daemon is API-only and accepted runs remain
queued. See the linked configuration guides before enabling a worker.

## Start the service

```bash
uv run blackcell daemon foreground
```

In another terminal using the same endpoint and token:

```bash
uv run blackcell daemon status
```

## Prepare and submit contracts

Copy the templates and replace their placeholder repository path, base commit, objective, allowed
paths, and acceptance command with values for the managed project.

```bash
mkdir -p /tmp/blackcell-runtime-requests
cp examples/runtime/requests/*.template.json /tmp/blackcell-runtime-requests/
```

Submit the closed contracts in dependency order. `POST /api/v1/runs` is asynchronous: successful
submission records the queued run before any execution worker can claim it.

```bash
uv run blackcell project register \
  --request /tmp/blackcell-runtime-requests/project.template.json
uv run blackcell intent accept \
  --request /tmp/blackcell-runtime-requests/intent.template.json
uv run blackcell plan accept \
  --request /tmp/blackcell-runtime-requests/plan.template.json
uv run blackcell run submit \
  --request /tmp/blackcell-runtime-requests/run.template.json
```

Read state and replay without model or execution side effects:

```bash
uv run blackcell run status run
uv run blackcell run query \
  --request /tmp/blackcell-runtime-requests/query.template.json
uv run blackcell events list --after 0 --limit 100
uv run blackcell run replay run
```

Cancellation is an explicit mutation with its own idempotency key:

```bash
uv run blackcell run cancel run \
  --request /tmp/blackcell-runtime-requests/cancel.template.json
```

## Open a client projection

The packaged browser client is available at `http://127.0.0.1:8080/ui`. It keeps the token in
memory and talks only to same-origin service routes. The terminal projection uses the same typed
client and stores only an owner-readable cursor:

```bash
uv run blackcell tui
```

## Restart and recovery boundary

Stop and restart the foreground daemon with the same data directory. Status, ordered events, and
replay must reconstruct from the event and artifact stores without repeating provider calls or
repository effects. An incompatible pre-cutover database is rejected without migration or
deletion; move it aside only after an operator-controlled backup and recovery decision.

For a supervised local service, use `blackcell daemon install`, `start`, `stop`, `restart`, `status`,
and `logs`. Installation consumes an existing owner-only environment file and never writes a
credential.

## Related guides

- [Execution model](execution.md)
- [Execution worker configuration](execution-worker-configuration.md)
- [Review configuration](review-configuration.md)
- [Verification configuration](verification-configuration.md)
- [Epistemic evaluation](../epistemic-evaluation.md)
