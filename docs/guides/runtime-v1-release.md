---
node: guides/runtime-v1-release
kind: guide
edges:
  governed-by:
    - charter
    - architecture
    - spec/bcp-0034-evolutionary-runtime
  supported-by:
    - targets/containers
    - targets/recovery
---

# Runtime-v1 Release Evidence

Runtime-v1 is evidence-complete and unpublished. The supported product path is the Repository
Operator composed over Daily Operator v2, the canonical kernel database, and live-free historical
replay. The evidence bundle under `../../release/runtime-v1/` makes that candidate inspectable; it
does not publish a package or image.

## Prerequisites

- Python 3.14 and `uv`;
- Git for repository observation and the isolated example;
- Linux rootless Podman only for the optional API/worker container gate;
- an operator-supplied opaque API token only when starting the service boundary.

Install the locked development and verification environment:

```bash
uv sync --locked --all-groups
```

## Credential-free recorded walkthrough

The maintained example creates its own temporary Git repository and keeps the kernel database and
artifacts inside that repository. It uses the deterministic recorded model, performs no network
request, and deletes its temporary state on exit.

```bash
bash examples/runtime-v1/recorded-operator.sh
```

For an existing repository, the corresponding JSON-first product commands are:

```bash
uv run blackcell operator run --model recorded --repo .
uv run blackcell operator state --repo .
uv run blackcell operator context --repo .
uv run blackcell operator replay --repo .
uv run blackcell events list --repo .
```

Replay reads and verifies recorded protocol state. It never invokes a live model or repeats an
affordance.

## Rootless service boundary

The same image contract runs the API and durable worker as numeric uid/gid `10001:10001`, with a
read-only root filesystem, dropped capabilities, loopback host publication, a read-only repository
mount, and a persistent owner-only state volume. Supply the token at process start; do not write it
to this repository.

```bash
export BLACKCELL_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export BLACKCELL_REPOSITORY_PATH="$PWD"
podman compose up --build
```

The complete live rootless acceptance is opt-in:

```bash
BLACKCELL_RUN_PODMAN_TESTS=1 uv run pytest -q tests/integration/test_podman_runtime.py
```

WP25 retains the observed six-probe single-host result. Its timings are reliability evidence, not
throughput, capacity, SLO, or production recovery targets.

## Recovery boundary

Follow `../targets/recovery.md` for verified SQLite-plus-artifact bundles, independent verification,
non-destructive restore, and admission limits. Offsite transport, encryption, automatic cutover,
hard filesystem quotas, and untested power-loss behavior remain deployment responsibilities.

## Reproduce the evidence

The read-only verifier rebuilds both generated JSON documents in memory, recomputes every declared
material hash and mode, derives the locked runtime dependency graph again, and compares canonical
bytes:

```bash
uv run python tools/release_evidence.py verify --repo-root .
```

The exact setup, quality, full-suite, example, and rootless commands are arrays in
`../../release/runtime-v1/verification-manifest.json`. The manifest stores digests and concise
boundaries rather than raw logs, secrets, absolute paths, or variable wall-clock output.

Regeneration is intentional and write-capable:

```bash
uv run python tools/release_evidence.py generate --repo-root .
```

## SBOM scope and non-claims

`blackcell-runtime-v1.cdx.json` is a CycloneDX 1.7 pre-build SBOM. It contains BlackCell and the
transitive closure of its non-development Python dependencies in `uv.lock`, with explicit
dependency relationships. It excludes development-only packages and does not pretend to inventory
an image that was not built.

WP27 does not build or publish a wheel, source distribution, container image, tag, GitHub release,
signature, provenance attestation, or vulnerability report. Those actions require separate tools,
fresh evidence, and explicit delivery authorization.
