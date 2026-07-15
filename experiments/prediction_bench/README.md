# PredictionBench

PredictionBench is the credential-free WP24 comparison over Blackcell's advisory
`TransitionPrediction` and `TransitionPredictionScore` contracts. Eight synthetic one-step
scenarios cover stable state, declared and unexpected changes, a deliberately incorrect declared
effect, source missing/conflict, and actual missing/conflict outcomes.

The matched conditions are:

1. `state-persistence`: the WP10 conservative deterministic baseline.
2. `declared-effects`: developer-authored action effects with state-persistence fallback.

Both conditions receive the same source snapshot, action identity, target, horizon, actual outcome
snapshot, and scorer. Declared effects retain supporting source identities and are separate from
hidden actual outcomes. One declared effect intentionally predicts `running` while the actual
outcome is `failed`, preventing the fixture from encoding an oracle.

Run the JSON-first comparison and optionally reserve a retained artifact:

```bash
uv run blackcell bench predict
uv run blackcell bench predict --artifact /tmp/wp24.json
```

The report includes exact match, Brier score, target coverage, typed missing/conflict findings,
latency samples, tokens, provider cost, full prediction/score identities, and environment metadata.
Both measured conditions are deterministic and therefore correctly report zero model tokens and
zero provider cost. The unavailable local-neural and hybrid-neural-symbolic candidates retain
`null` measures rather than being misreported as free or failed trials.

The checked-in `wp24-recorded.json` is descriptive. It is an author-crafted, non-held-out dataset
and supports neither a learned-world-model claim nor a neuro-symbolic-reasoning-system claim.
