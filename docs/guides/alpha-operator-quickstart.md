---
node: guides/alpha-operator-quickstart
kind: guide
edges:
  governed-by:
    - charter
    - scope
    - architecture
    - adr/0009-project-runtime-scope
  complements:
    - guides/alpha-worker-configuration
    - guides/alpha-review-configuration
    - guides/alpha-verify-configuration
---

# Alpha Operator Quickstart

This is the shortest source-checkout path through BlackCell's actual alpha CLI, daemon, browser,
execution, review, verification, and replay contracts. It is not an installation or alpha-release
claim. The only measured end-to-end host is Linux x86_64 under WSL2 with Bubblewrap 0.9.0; the
public PyRatatui client is locked and measured on that same host, not yet across the platform matrix.

The daemon owns orchestration and durable state. The CLI and browser call the same
`/api/alpha/v1` service. Never submit new alpha work through `/api/v1/runs` or
`DailyOperatorV2Workflow`; those surfaces exist only for historical migration and replay.

## 1. Prepare the source checkout and project

BlackCell currently requires Python 3.14, `uv`, Git, and a pinned Kernform 0.1.0 executable. A
repository-writing run additionally requires Linux user namespaces, Bubblewrap, `prlimit`, and
every executable alias named by the accepted plan.

From the BlackCell checkout, install the locked development environment:

```bash
uv sync --locked --all-groups
```

Choose exactly one canonical Git project. The daemon will reject a project registration whose
`root` differs from `BLACKCELL_REPOSITORY_ROOT`:

```bash
export BLACKCELL_REPOSITORY_ROOT=/ABSOLUTE/PATH/TO/PROJECT
export BLACKCELL_KERNFORM_EXECUTABLE=/ABSOLUTE/PATH/TO/kernform
uv run blackcell project check \
  --path "$BLACKCELL_REPOSITORY_ROOT" \
  --kernform "$BLACKCELL_KERNFORM_EXECUTABLE" \
  > /tmp/blackcell-kernform-check.json
```

The result must report `kernform_version` `0.1.0` and a `success` status. Copy its
`result_digest` into `project.template.json` as `configuration_digest`. Registration durably binds
that operator-supplied digest; the daemon does not silently import or rerun Kernform.

Record the exact Git base for the plan:

```bash
git -C "$BLACKCELL_REPOSITORY_ROOT" rev-parse HEAD
```

## 2. Prepare owner-only runtime state and authentication

Use an absolute data directory outside the project and an absolute owner-only token file. The token
must contain at least 32 diverse visible ASCII characters. Do not check either path into Git.

```bash
export BLACKCELL_DATA_DIR=/ABSOLUTE/OWNER/ONLY/PATH/blackcell-alpha
export BLACKCELL_API_TOKEN_FILE=/ABSOLUTE/OWNER/ONLY/PATH/blackcell-api-token
install -d -m 700 "$BLACKCELL_DATA_DIR"
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
  > "$BLACKCELL_API_TOKEN_FILE"
chmod 600 "$BLACKCELL_API_TOKEN_FILE"
export BLACKCELL_RUNTIME_ENDPOINT=http://127.0.0.1:8080
```

Keep `BLACKCELL_API_TOKEN` unset when using `BLACKCELL_API_TOKEN_FILE`; configuring both is an
error. Plain HTTP is accepted only for a loopback endpoint. This quickstart does not configure TLS
or remote access; do not expose the daemon directly to another host.

## 3. Choose API-only or full processing

With no alpha worker configuration, `blackcell daemon foreground` starts the API only. Project,
intent, and plan admission works, and a submitted run remains durably `queued`. This is useful for
checking contracts without granting model or execution authority.

For a complete run, configure all three optional processes before starting the daemon:

- [execution worker](alpha-worker-configuration.md): CODE provider, worktree, Bubblewrap, aliases,
  and resource limits;
- [review worker](alpha-review-configuration.md): separate REVIEW provider and fenced identities;
- [verification worker](alpha-verify-configuration.md): deterministic evidence adjudication with no
  model provider.

Each configuration file must be a canonical owner-owned mode-`0600` regular file outside the
project. Execution, review, and verification worker/supervisor identities must be distinct. The
daemon validates every enabled child before it starts the API, so an invalid optional configuration
cannot leave a silently degraded process set.

## 4. Start one foreground daemon

In the first terminal, export the runtime and optional worker variables, then run:

```bash
uv run blackcell daemon foreground
```

The foreground process supervises the API and only the explicitly configured alpha children. Stop
it with `Ctrl-C`. Do not start separate schedulers against different data roots.

In a second terminal with the same token-file and endpoint variables, inspect readiness:

```bash
uv run blackcell daemon status
```

The command reports runtime liveness/readiness together with systemd-user state. An unavailable or
uninstalled user service does not make a healthy foreground runtime unready.

The packaged browser client is now available at `http://127.0.0.1:8080/alpha`. On loopback, enter
the same token value and load the project, intent, plan, and run JSON files. The browser retains the
token, selected file content, and reconnect cursor only in memory. It uses the same HTTP contracts
and a single-use WebSocket ticket; it is not a second scheduler.

The packaged terminal client uses the same endpoint and token environment and stores only an
owner-only event cursor under `$BLACKCELL_DATA_DIR/alpha-tui-cursors`:

```bash
uv run blackcell alpha tui
```

Use `1` through `4` to select project, intent, plan, or run; `w` to edit an absolute request-file
path; and `Ctrl-W` to submit it. Use `i` to edit the selected run ID, `s` for status, `p` for replay,
`x` for cancellation, `r` for ordered-event refresh, `c` to reconnect, and `q` to quit. `Esc`
cancels input and `Ctrl-U` clears it. The native terminal stays on the asyncio thread while the
shared controller offloads HTTP and bounded file work. There is no credential field, scheduler,
provider, worktree, or persistence port in the TUI.

## 5. Materialize the request templates

Copy the checked templates to an untracked working directory:

```bash
mkdir -p /tmp/blackcell-alpha-requests
cp examples/alpha/requests/*.template.json /tmp/blackcell-alpha-requests/
```

Before submission, replace every repository-specific value:

1. In `project.template.json`, replace `/ABSOLUTE/PATH/TO/PROJECT` with the exact canonical
   `BLACKCELL_REPOSITORY_ROOT` and replace the all-zero configuration digest with the
   `result_digest` from `blackcell project check`.
2. In `intent.template.json`, replace the objective, constraints, assumptions, and unresolved
   questions with the real bounded intent.
3. In `plan.template.json`, replace the all-zero `base_commit`, node objective, allowed paths,
   budget, executable aliases, argv-only acceptance check, and expected exit code. Every writer must
   be dependency-ordered, and every accepted executable alias must exist in the execution-worker
   configuration. An alias is 1 through 64 ASCII characters, begins alphanumerically, and otherwise
   contains only letters, digits, `.`, `_`, `+`, or `-`. Node timeouts are bounded from 1 through 600
   seconds so every accepted value is executable by the acceptance runner. Plan admission reserves
   the complete review-evidence shape
   across all nodes: one outcome per node, four artifacts per check, and up to three source/effect
   artifacts per maximum changed file must fit the closed 128-item context limit.
4. Keep the project, intent, plan, and run identifiers cross-linked. If an identifier changes,
   update every later request. Use new idempotency keys when the content changes.

The checked-in files are contract-valid templates, not a runnable project or live-provider proof.
The daemon will reject the unchanged root and base placeholders.

## 6. Admit and run the workflow

Submit the files in dependency order:

```bash
uv run blackcell alpha project register \
  --request /tmp/blackcell-alpha-requests/project.template.json
uv run blackcell alpha intent accept \
  --request /tmp/blackcell-alpha-requests/intent.template.json
uv run blackcell alpha plan accept \
  --request /tmp/blackcell-alpha-requests/plan.template.json
uv run blackcell alpha run submit \
  --request /tmp/blackcell-alpha-requests/run.template.json
```

All commands emit JSON and fail locally if a request file is a symlink, oversized, malformed, or
contract-invalid. Repeating byte-equivalent content with the same identity is idempotent; changing
content under an existing identity is a conflict.

Inspect durable state without calling a provider or repeating effects:

```bash
uv run blackcell alpha run status alpha-run
uv run blackcell alpha events list --after 0 --limit 100
uv run blackcell alpha run replay alpha-run
```

Replay may report execution complete while review or verification is not started. A verified
terminal outcome requires the execution, review, and verification workers to consume the same event
and artifact root.

Request cooperative cancellation with the checked cancel contract:

```bash
uv run blackcell alpha run cancel alpha-run \
  --request /tmp/blackcell-alpha-requests/cancel.template.json
```

Cancellation does not erase partial evidence or unsafe-to-delete worktrees. A request observed by an
active acceptance check is acknowledged under the active fence; an idle queued run can terminate
immediately.

## 7. Restart and recover

Stop the foreground daemon, retain `BLACKCELL_DATA_DIR`, export the same configuration, and start
the same command again. Startup replays durable state and reconciles only operations whose external
outcome is unambiguous. A provider dispatch recorded without a terminal result is never called
again automatically; the run becomes `reconciliation-required` and retains its evidence/worktree.

After restart, use `status`, `events list --after CURSOR`, and `replay` to inspect the authoritative
state. Do not infer success from a retained branch or checkout alone.

## Current alpha boundary

This source quickstart does not prove release readiness. The deterministic acceptance snapshot and
one redacted live-provider failure proof are under [`release/alpha/`](../../release/alpha/). The
live attempt reached the real Codex route but failed the closed proposal-domain boundary before
provider metrics or a proposal artifact could be stored; it was not retried. Still unmeasured are a
contract-valid maintained-project live change, human usability, successful-attempt token and
provider-latency values, external billing, complete CI coverage/type gates, native non-WSL Linux,
macOS, Windows, and hosts without usable user namespaces. Bubblewrap currently has no
BlackCell-installed seccomp filter or cgroup-v2 controller limits. No package, tag, release, or
deployment follows from completing this guide.
