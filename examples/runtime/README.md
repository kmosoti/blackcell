# Runtime examples

Validate the checked project, intent, plan, run, query, and cancellation templates from the
repository root:

```bash
bash examples/runtime/validate-contracts.sh
```

The script decodes the same closed contracts used by the CLI and verifies their cross-bindings. It
does not start a service, invoke a provider, mutate a repository, or claim end-to-end execution.

Running the daemon requires an explicit opaque token. This example intentionally does not
manufacture a credential and is not a release artifact.
