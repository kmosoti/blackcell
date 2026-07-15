---
node: evaluation-methodology
kind: evaluation-contract
edges:
  evaluates:
    - charter
    - architecture
---

# Evaluation Methodology

## Research questions

1. Does a conflict-preserving operational state estimate recover task-relevant state more
   accurately than latest-value or free-form summary baselines?
2. Does a structured ContextFrame improve valid decisions per context unit compared with raw
   chronological or latest-N evidence?
3. Does a typed symbolic gate reduce escaped policy violations without an unacceptable false
   rejection rate?
4. Do action-conditioned transition models eventually improve prediction and planning over
   persistence, symbolic, empirical, and LLM-only baselines?

## OperatorBench

OperatorBench is a deterministic repository-work simulator, not a benchmark of broad model
intelligence. Each scenario defines observations visible to the agent, hidden environment
state used only by graders, a task objective, available affordances, hard policies, expected
evidence, and acceptable outcomes.

The deterministic Phase 1 pilot contains six scenarios spanning:

- stale check results;
- conflicting source reports;
- missing required evidence;
- irrelevant distractors;
- dependency-blocked work;
- corrections arriving after initial observations;
- partial tool failures;
- unsafe write or readiness proposals.

Reordered delivery, duplicated delivery, source-order permutations, and a larger scenario
set remain explicit benchmark expansions rather than implemented claims.

## Conditions

The implemented comparison evaluates every scenario under a single decision-model instance,
action schema, model configuration, and hard context-character ceiling:

- raw chronological evidence;
- latest-N evidence;
- structured context projection.
- deterministic term retrieval over a synthetic SignalPacket;
- ephemeral SQLite FTS5 retrieval behind the same feature-owned evidence-policy port.

All retrieval candidates retain source-event and claim identities, stale/conflict flags, and
correction provenance. Linked raw-evidence escalation remains a planned condition.

The current fixture policy and deterministic grader establish the first baseline. Prompt-only
instructions, schema-only validation, the production Python policy gate, and any justified
solver-backed constraints must be reported as separate later interventions.

## Measures

The comparison records the following measures; the remaining columns define the research roadmap
and must not be reported as implemented results.

| Concern | Phase 1 pilot | Planned extensions |
| --- | --- | --- |
| Outcome | task success | valid-action rate, partial credit |
| Evidence | required-evidence recall/precision, invisible citations, unsupported claims | conflict recall |
| State | — | slot accuracy, stale-state errors, unknown precision/recall |
| Policy | violations, false rejection | false accept, repair success, approval rate |
| Context | characters and reported model tokens | redundancy, selection/omission accuracy |
| System | full treatment-to-outcome latency; operator replay and projection-hash checks | projection lag, orphan lineage |
| Prediction | one-step exact match, Brier, missing/conflict, latency, token, and cost baselines | log loss, reliability, held-out rollout error |
| Planning | — | cumulative cost, regret to oracle, goal success |

## PredictionBench

PredictionBench validates one-step advisory transition measurement on eight synthetic scenarios.
It pairs the WP10 state-persistence baseline with an experiment-only developer-declared-effect
baseline over identical source snapshots, actions, targets, horizons, canonical outcomes, and
scoring code. Scenarios cover stable and unexpected changes, a deliberately failed declared
effect, source missing/conflict, and actual missing/conflict outcomes.

The retained report records exact match, Brier score, target match and scored coverage, typed
missing/conflict findings, latency samples, tokens, provider cost, prediction/score identities, and
environment metadata. Deterministic baselines correctly report zero model tokens and provider cost.
Unavailable neural and hybrid candidates retain null measures.

The author-crafted dataset is descriptive and has no held-out scenario families. Developer-declared
effects are symbolic inputs, not learned predictions, and do not establish a neuro-symbolic system.
A learned candidate remains ineligible for promotion until it is pinned behind the gateway and
beats persistence, declared-effect, empirical, and justified hybrid baselines on held-out rollout
and downstream planning evidence.

## RuntimeBench

RuntimeBench executes six existing acceptance surfaces as direct, independently timed pytest
probes: authenticated API/live-free replay, five-role worker continuity, scheduler restart and
fencing, request/storage/artifact quotas, backup/restore/replay, and rootless Podman persistence.
The report retains exact argv and declared environment overrides, pass/skip counts, per-test call
durations, subprocess wall time, output digests, and source/environment fingerprints. It never
retains raw probe output.

A report is complete only when every probe passes, including the explicitly enabled live rootless
container test. The recorded WP25 result is a one-run reliability baseline on one WSL2 host.
Fixture and subprocess timings are descriptive; they are not throughput, tail-latency, service
SLO, or production RTO/RPO measurements and do not independently trigger optimization.

## Trial protocol

- Use the six-scenario deterministic pilot to validate contracts. Expand to 20 to 30
  scenarios before drawing comparative claims, with at least three trials for each
  stochastic model condition.
- Pair conditions by scenario, model, model configuration, and context budget.
- Preserve model identifier, timestamp, ContextFrame hash, response artifact, tool artifacts,
  and scorer version.
- Prefer deterministic environment and policy graders. Use blinded human rubrics for semantic
  utility and treat LLM judges as secondary evidence.
- Report Wilson intervals for proportions and paired bootstrap intervals for intervention
  differences when sample size permits.
- Split by chronology and scenario family. Do not use random row splits for transition-model
  evaluation.

## Promotion criteria

The pilot establishes effect sizes and variance before numerical thresholds are fixed.
Subsequent experiments preregister promotion criteria.

At minimum:

- kernel replay must be deterministic for all fixtures;
- no known hard-policy violation may escape the Phase 1 executor;
- a context intervention must improve success or materially reduce context at non-inferior
  success;
- a learned transition model must outperform simple baselines on held-out scenario families
  and improve downstream planning, not merely latent loss.

## Reproducibility

Public scenarios use synthetic data and recorded model fixtures. CI never requires personal
ChatGPT credentials. Live-model trials are separately labelled and stored as experiment
artifacts so deterministic tests remain independent of model availability.

`blackcell bench compare --model recorded` runs the five-treatment contract without credentials.
The canonical checked-in WP23 report uses replayed fixed proposals and a zero clock, so its
quality and latency values are descriptive only. A live comparison requires an explicit model
identifier, at least three replicates, and an exclusive artifact path before any provider call.

`blackcell bench predict` runs PredictionBench without credentials and can exclusively retain the
canonical WP24 report. Its latency is an environment-specific microbenchmark; its deterministic
zero-token and zero-provider-cost values do not apply to unavailable neural candidates.

`blackcell bench runtime --repo-root .` runs the five non-container probes as an explicitly
incomplete diagnostic. A complete retained WP25 profile requires `--include-podman` and a fresh
owner-only `--artifact` path. Raw test logs remain outside the report; only their digests are
retained.
