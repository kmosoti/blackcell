# OperatorBench

OperatorBench is Blackcell's deterministic public fixture-contract and matched-comparison
surface for the Repository Operator vertical slice. It renders five context treatments while
holding the task, model boundary, action schema, context ceiling, and grading logic fixed:

1. `raw-chronological`: every observation in sequence order.
2. `latest-n`: only the most recent N observations.
3. `structured`: an explicit state projection with provenance and unknowns.
4. `term-retrieval`: deterministic exact-term ranking over a synthetic SignalPacket.
5. `fts5-retrieval`: ephemeral SQLite FTS5 ranking behind the same evidence-policy port.

The synthetic fixture set covers task dependencies, capacity and check state,
stale and conflicting observations, irrelevant distractors, human corrections,
partial tool failures, and unsafe proposals. The canonical fixtures live in
`blackcell.evaluation.scenarios`; `manifest.json` records the experiment contract.

Primary outcomes are task success, required-evidence recall, evidence precision, invisible
citations, unsupported claims, policy violations, false rejections, context/response size,
model token usage when reported, and end-to-end treatment latency. Paired differences use a
deterministic bootstrap over scenario and replicate identities.

Run the credential-free recorded comparison and optionally retain its canonical report:

```bash
uv run blackcell bench compare --model recorded
uv run blackcell bench compare --model recorded --artifact /tmp/wp23-recorded.json
```

The checked-in [`wp23-recorded.json`](wp23-recorded.json) contains 30 full trial records and is
content-addressed. Because it has six synthetic scenarios, one replicate, fixed replayed
proposals, and a deterministic zero clock, its aggregates are descriptive rather than estimates
of a live model context effect.

Live comparisons are an explicit, separately retained operation. They require a pinned model,
at least three paired replicates, and a fresh artifact path:

```bash
uv run blackcell bench compare \
  --model codex \
  --codex-model MODEL_ID \
  --replicates 3 \
  --artifact /secure/path/wp23-live.json
```

The current six scenarios remain below the 20-scenario promotion gate. A live run on this dataset
is useful for diagnostics but cannot by itself promote a context or retrieval intervention.

The deterministic grader remains the initial baseline. A model judge may be added as a separately
reported secondary measure, never as a replacement for environment outcomes or exact policy
checks.
