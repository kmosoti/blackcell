# RuntimeBench

RuntimeBench is the WP25 reproducible acceptance profile for the implemented runtime-v1
surfaces. It invokes the existing public and integration tests as six independently timed probes:

1. authenticated API and live-free replay;
2. five-role worker execution and restart continuity;
3. scheduler restart, lease recovery, retry, and fencing;
4. request, storage, and artifact quotas;
5. backup verification, external restore, and replay; and
6. rootless Podman health, read-only roots, and restart persistence.

Each result retains the exact direct pytest argv, declared environment overrides, pass/skip
counts, per-test call durations, subprocess wall time, and a digest of captured output. Raw test
output is never written to the report. The environment manifest retains the Git base, source and
lock fingerprints, tool versions, host shape, and rootless-container status.

Run a non-container diagnostic profile without retaining an artifact:

```bash
uv run blackcell bench runtime --repo-root .
```

A complete report requires the live rootless-container probe and an owner-only, previously absent
artifact path:

```bash
uv run blackcell bench runtime \
  --repo-root . \
  --include-podman \
  --artifact experiments/runtime_bench/wp25-recorded.json
```

The checked-in `wp25-recorded.json` is a single-host reliability baseline. Its subprocess wall
times and pytest call durations are descriptive harness measurements, not service SLOs,
throughput results, production RTO/RPO commitments, or evidence for changing runtime defaults.
