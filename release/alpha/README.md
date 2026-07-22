# BlackCell alpha acceptance snapshot

This is an unpublished, partial candidate—not an alpha tag proposal.

The machine-readable [acceptance manifest](acceptance-manifest.json) binds the current local proof
to exact source and pytest nodes. On 2026-07-22, eight behavior nodes passed together in 11.72 seconds
on WSL2 x86_64 with Bubblewrap 0.9.0. They cover the deterministic success path, restart, cancellation,
provider-ambiguous recovery, review and verification, cursor resumption, provider failure, and
read-only historical V2 replay. The public PyRatatui client and its locked runtime dependency are
now covered by separate focused tests.

The success fixture uses a temporary two-node Git project and a recorded provider. Its 100 input
tokens, 20 output tokens, 10 ms, and 1 micro-USD values are fixture metadata, not live measurements.
The supplementary [live-provider proof](live-provider-proof.json) records exactly one production
BlackCell CODE-provider attempt against the BlackCell repository. Dispatch completed, but the
provider result failed the closed proposal-domain boundary with
`invalid-alpha-change-provider-proposal`. BlackCell applied no repository change, ran no acceptance
command, stored no proposal artifact, and replayed the terminal failure with verified context and
outcome artifacts after an API restart. The rejected result never reached the metadata-artifact
stage, so input/output tokens, provider latency, and cost are explicitly unavailable; the measured
6,933 ms value is durable dispatch-to-terminal time, not provider-reported latency. The clean
retained checkout and its base-only deterministic branch were removed afterward under explicit
operator authority.

That live result measures the fail-closed path, not maintained-project output quality or a
successful live change. The checkout also lacks Kernform managed state, so its alpha registration
used an operator-bound digest of the base `pyproject.toml` and makes no Kernform-conformance claim.
The known output-schema gap that allowed an empty operations array to cross structured-output
validation is tightened locally, without a second provider call.

The local behavior gate is:

```bash
uv run python tools/run_pytest.py \
  tests/unit/test_alpha_a08_acceptance.py::test_real_project_completes_through_cli_daemon_workers_restart_and_browser \
  tests/unit/test_alpha_a05_acceptance.py::test_a05_hidden_shortcut_finding_deterministically_fails_and_replays \
  tests/unit/test_alpha_a05_acceptance.py::test_a05_reviewer_and_verifier_errors_remain_durable_non_verdicts \
  tests/unit/test_alpha_worker.py::test_worker_acknowledges_cancellation_from_acceptance_callback \
  tests/unit/test_alpha_lifecycle.py::test_startup_reconciliation_never_requeues_ambiguous_provider_dispatch \
  tests/unit/test_alpha_web.py::test_websocket_streams_resumed_typed_events_and_rejects_ticket_replay \
  tests/unit/test_alpha_worker.py::test_worker_records_content_free_provider_failure_and_retains_checkout \
  tests/unit/test_run_replay.py::test_v2_success_replays_exact_states_and_writes_nothing \
  -q --blackcell-require-all-pass
```

Tag blockers remain explicit in the manifest. A successful contract-valid live proposal,
human-driven PyRatatui/browser usability, broader platform coverage, package compatibility, and the
complete CI gates remain open. This snapshot and live proof are root-authored; independent agent
review and verification were explicitly waived for the draft PR. No package, image, tag, release,
or deployment is created by this evidence snapshot.
