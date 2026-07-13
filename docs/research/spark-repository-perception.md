---
node: research/spark-repository-perception
kind: research
edges:
  informs:
    - evaluation-methodology
  constrained-by:
    - adr/0003-model-execution-boundary
    - concepts/agent-operating-model
---

# Spark Repository Perception Experiment

Status: proposed. This document defines an experiment, not a runtime contract or a production
routing promise.

## Question

Can a `gpt-5.3-codex-spark` medium worker, given a schema-validated minimal-context packet, recover
repository facts with quality comparable to a matched `gpt-5.6-terra` medium worker while reducing
latency and model input? The root remains `gpt-5.6-terra` high for task definition, source checks,
and synthesis.

The experiment measures repository perception: locating relevant paths, tracing bounded execution
paths, identifying test and documentation impact, and returning exact evidence. It does not grant
workers authority to choose architecture or approve changes.

## Matched Conditions

Use a held-out task set with repository inventory, API cataloguing, documentation drift, test
impact, duplication candidates, and bounded execution-path tracing. For each trial, hold constant:

- repository commit and dirty-worktree snapshot;
- objective, acceptance criteria, allowed and forbidden paths, and required evidence;
- serialized change spec, worker-packet schema, result schema, limits, and verification argv;
- tool availability, sandbox, approval policy, cache condition, and timeout;
- root synthesis model and source-check procedure;
- trial order randomization and failure classification.

Run at least the following matched production-style treatments:

| Treatment | Worker | Effort | Fork mode | Input |
| --- | --- | --- | --- | --- |
| T1 | `gpt-5.6-terra` | medium | `none` | validated packet path only |
| T2 | `gpt-5.3-codex-spark` | medium | `none` | identical validated packet path only |

All production-style treatments must pass `fork_turns = "none"`. A full-history fork may be used
only as an isolated, short-context measurement to estimate fork overhead. It must not enter the
recommended routing policy or share results with production-style trials.

## Measurements

Record one machine-readable row per trial with these fields:

| Field | Definition |
| --- | --- |
| `fork_mode` | `none`, or `all` only for the isolated short-context benchmark |
| `change_spec_bytes` | UTF-8 bytes in the canonical serialized change spec |
| `worker_packet_bytes` | UTF-8 bytes in the canonical serialized packet |
| `child_initial_input_tokens` | input tokens reported for the child's first model call |
| `duplicated_context_bytes` | identical base-instruction and packet bytes repeated across workers |
| `parent_context_bytes_avoided` | serialized parent-turn bytes excluded by `fork_turns = "none"` |
| `worker_result_bytes` | validated result bytes returned to the Terra root |
| `synthesis_input_tokens` | root input tokens used to source-check and synthesize results |
| `synthesis_output_tokens` | root output tokens used for the final synthesis |
| `worker_wall_time_ms` | spawn-to-valid-result elapsed time |
| `synthesis_wall_time_ms` | first result available to completed synthesis elapsed time |

Also score evidence precision, evidence recall against an adjudicated answer key, invalid path or
symbol claims, missed acceptance criteria, schema violations, blocked/failed status, verification
accuracy, and total model/tool cost. Preserve conflicting observations rather than counting forced
agreement as quality.

Measure parent-context bytes from the exact serialized parent turns that a full fork would include.
Measure duplicated context separately because mandatory system, tool, custom-agent, and repository
instructions still load for no-history workers. Do not call those base controls avoidable context.

## Trial Protocol

1. Freeze the task set, answer keys, repository commit, and environment fingerprint.
2. Generate one shared change spec and content-identical packets except for stable worker IDs and
   the declared model treatment.
3. Validate every contract before spawning and every result before scoring.
4. Inspect child rollouts to confirm the subagent parent reference exists and no parent
   conversation turns were inherited.
5. Run paired trials in randomized order and distinguish cold-cache from warm-cache results.
6. Have the Terra root source-check consequential claims without access to treatment labels.
7. Report paired differences with uncertainty; do not promote from a single successful example.

Promotion requires Spark to remain within a predeclared quality margin on held-out tasks, produce
no higher rate of fabricated evidence or schema violations, and show a material latency or token
benefit. A quality regression on architecture-sensitive or ambiguous-path tasks keeps those tasks
on Terra regardless of average cost.

## Staged Research Path

1. Use ephemeral structured packets and validated results under `/tmp/blackcell-codex/`.
2. Promote experiment artifacts to content-addressed storage only after the schema and measures
   stabilize.
3. Compare an SQLite FTS5 retrieval baseline only after packet routing has matched evidence.
4. Evaluate graph or embedding retrieval only after matched promotion evidence shows that FTS5 is
   insufficient.

## Non-Goals

This proposal does not implement repository indexing, GraphRAG, embedding retrieval, persistent
agent memory, or BlackCell runtime orchestration. It does not change the model gateway, grant
workers ambient authority, or recommend full-history forks.

The current Codex subagent guidance recommends distilled worker results because each child performs
its own model and tool work. Codex 0.144.1's MultiAgentV2 spawn implementation defaults an omitted
`fork_turns` to `"all"`; the BlackCell workflow therefore treats explicit `"none"` as mandatory.
The same release defaults `hide_spawn_agent_metadata` to `true`, which removes `agent_type`,
`model`, `reasoning_effort`, and `service_tier` from the model-visible spawn schema. BlackCell
explicitly enables MultiAgentV2 for every mode and sets that option to `false` so named project
agents remain selectable. The named agent file, not direct spawn overrides, remains the normal
source of a worker's model and reasoning configuration. Existing threads do not hot-reload this
schema; configuration acceptance uses a fresh session.

References:

- [Codex subagent guidance](https://developers.openai.com/codex/subagents)
- [Codex 0.144.1 MultiAgentV2 spawn implementation](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs)
- [Codex 0.144.1 MultiAgentV2 spawn schema](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/core/src/tools/handlers/multi_agents_spec.rs)
