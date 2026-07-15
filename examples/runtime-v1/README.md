# Runtime-v1 examples

Run the isolated, credential-free Repository Operator example from the repository root:

```bash
bash examples/runtime-v1/recorded-operator.sh
```

It creates a temporary Git repository, executes one recorded-model Daily Operator v2 run, projects
state, and performs live-free historical replay. Its database and artifacts stay below the
temporary repository and are deleted when the example exits.

Verify the retained release evidence without rewriting it:

```bash
uv run python tools/release_evidence.py verify --repo-root .
```

The rootless API/worker deployment requires an explicit opaque token and is documented in
`docs/guides/runtime-v1-release.md`; the example intentionally does not manufacture a credential.
