---
node: targets/containers
kind: target
edges: {}
---

# Containers

BlackCell has two explicit targets in one `Containerfile`:

- `development` preserves the Git-tracked uv, compiler, Git, and ripgrep workspace used by the
  devcontainer;
- `runtime` installs only locked production dependencies and the packaged BlackCell application.

The runtime target uses the numeric `10001:10001` user, one `blackcell-runtime` entry point, and
the same image for `api` and `worker`. It contains Git for read-only repository observation but no
compiler, test dependencies, coding-agent binary, provider credential, service credential, or
container-engine socket.

## Rootless local deployment

Confirm that Podman is rootless before starting the service:

```bash
podman info --format json | python -c \
  'import json, sys; print(json.load(sys.stdin)["host"]["security"]["rootless"])'
```

`podman compose` delegates to an installed Compose provider. If that provider reports a missing
Podman socket, start the user socket with `systemctl --user start podman.socket` or run
`podman system service --time=0` in another terminal for the deployment session. On a rootless
host without a user-systemd health scheduler, the acceptance test drives the configured image
healthchecks explicitly while polling.

Supply the service token at runtime, select the Git worktree to observe, and start both services:

```bash
export BLACKCELL_API_TOKEN="$(python -c \
  'import getpass; print(getpass.getpass("BlackCell API token: "))')"
export BLACKCELL_REPOSITORY_PATH="${BLACKCELL_REPOSITORY_PATH:-$PWD}"
podman compose up --build --detach
podman compose ps
```

The token must satisfy the runtime secret policy and must not be stored in a tracked `.env` file.
The API is published only on host `127.0.0.1:8080`; `BLACKCELL_PUBLISHED_PORT` may select another
loopback port. The repository bind is read-only. Provider and Codex credentials are intentionally
not part of this deployment because the default runtime route is the network-free recorded model.

Check public readiness from the host:

```bash
python -c \
  'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/health/ready").read().decode())'
```

Both services use a read-only root filesystem, a bounded `/tmp`, all capabilities dropped, and
`no-new-privileges`. They share the Compose-managed `blackcell-data` volume at
`/var/lib/blackcell`; the runtime creates its canonical owner-only state under the `data` child.
Worker startup waits for API readiness so initial state-directory creation is serialized.

`podman compose down` removes containers and preserves the named volume. Adding `--volumes`
deletes the durable runtime state and is appropriate only for an intentional destructive reset.

The process applies the bounded request and active-storage defaults documented in
`targets/recovery.md`; deployments may override those explicit `BLACKCELL_*` values. Run an online
bundle through `podman compose exec blackcell-api blackcell-runtime recovery backup`, then copy and
verify it outside the named volume. Do not remove the volume until the external-copy restore drill
in the recovery runbook has passed.

## Acceptance gate

Static container contracts run in the normal suite. On a Linux host with rootless Podman, run the
engine-backed gate explicitly:

```bash
BLACKCELL_RUN_PODMAN_TESTS=1 uv run pytest -vv tests/integration/test_podman_runtime.py
```

The gate owns a temporary rootless API service when required, builds a uniquely tagged image,
proves API and worker health, numeric non-root execution, read-only roots, exact state modes,
credential exclusion, and persistence across API restart, then removes every resource it created.

Container files:

- `Containerfile`
- `compose.yaml`
- `.dockerignore`
- `.devcontainer/devcontainer.json`
