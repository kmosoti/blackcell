---
node: epistemic-evaluation
kind: methodology
edges:
  governed-by:
    - charter
    - architecture
    - scientific-basis
  complements:
    - guides/review-configuration
    - guides/verification-configuration
---

# Epistemic Evaluation Guard

BlackCell treats a model review as a fallible proposal about evidence, not as proof and not as an
approval. The guard exists to make predictable model failure modes visible and blocking without
asking another model to certify its own confidence or hidden reasoning.

## Research basis

The design follows primary research and public evaluation guidance:

- NIST's [Generative AI Profile](https://doi.org/10.6028/NIST.AI.600-1) identifies confabulation,
  information-integrity failures, and human over-reliance as risks that require documented test,
  evaluation, validation, and verification rather than fluent self-attestation.
- NIST's [AI RMF Measure playbook](https://airc.nist.gov/airmf-resources/playbook/measure/) calls for
  documented methods, metrics, sensitivity analysis, and independent testing decisions.
- NIST's
  [agentic evaluation probes](https://www.nist.gov/programs-projects/building-evaluation-probes-agentic-ai)
  produce machine-readable audit trails and independently rubric factual grounding as faithfulness,
  completeness, and sufficiency. BlackCell maps those concerns to evidence grounding, acceptance
  coverage and counterevidence, and causal overreach instead of collapsing them into one score.
- [Large Language Models Cannot Self-Correct Reasoning Yet](https://arxiv.org/abs/2310.01798)
  reports that intrinsic self-correction without external feedback can fail or degrade answers.
- [Towards Understanding Sycophancy in Language Models](https://arxiv.org/abs/2310.13548) shows
  that assistants can follow a user's stated belief and that preference judgments can reward a
  convincing but incorrect response.
- [Language Models Don't Always Say What They Think](https://arxiv.org/abs/2305.04388) shows that
  generated rationales can omit the influence of biasing prompt features. A chain of thought is
  therefore not accepted as evidence.
- [Lost in the Middle](https://arxiv.org/abs/2307.03172) shows that long-context use varies with
  evidence position. BlackCell requires per-dimension coverage instead of accepting one global
  prose summary.
- [Judging the Judges](https://arxiv.org/abs/2406.07791) documents position bias in model judges.
  Canonical evidence ordering and explicit comparison checks are required when order could affect
  a judgment.
- [Language Models (Mostly) Know What They Know](https://arxiv.org/abs/2207.05221) finds useful but
  task-sensitive calibration behavior. Model confidence may trigger more checking; it cannot
  satisfy a gate.
- [Detecting hallucinations in large language models using semantic entropy](https://www.nature.com/articles/s41586-024-07421-0)
  shows that response variation can identify some confabulations but not consistent systematic
  errors. Disagreement is an escalation signal, not correctness proof.
- External evidence helps: [CRITIC](https://arxiv.org/abs/2305.11738) uses tool feedback during
  critique, while [Chain-of-Verification](https://arxiv.org/abs/2309.11495) separates a draft from
  independently answered verification questions. BlackCell adopts the separation and evidence
  binding, while keeping the reviewer itself tool-free and the verifier deterministic.

## Closed review matrix

Every review proposal must contain exactly one row for each dimension below. Every row has a
bounded claim, a falsification question, and source-line citations into the immutable host-built
review context.

| Dimension | Failure guarded against | Required review action |
| --- | --- | --- |
| `evidence-grounding` | confabulation or citation-shaped decoration | Bind the assessment to exact evidence and ask what cited fact would make the claim false. |
| `counterevidence` | self-confirmation and missed contradictory evidence | Inspect evidence that could defeat the favored interpretation; link any concern to a finding. |
| `acceptance-coverage` | long-context omission or an unexamined node/check | Account for the complete accepted objective, constraints, nodes, scopes, and checks. |
| `causal-overreach` | inferring mechanism or causation from an observed outcome | Separate recorded observation from causal explanation and mark unsupported causality unknown. |
| `scope-challenge` | sycophancy, user-premise capture, or acceptance drift | Test whether the requested framing conflicts with source, policy, authority, or declared scope. |
| `uncertainty` | overconfidence or false precision | Preserve material unknowns; confidence scores and fluent rationales do not close them. |

The only dispositions are:

- `supported`: cited review coverage is present. This passes the review-coverage row only; it does
  not prove the assessment's claim or the implementation's correctness.
- `concern`: one or more linked review findings identify counterevidence. Deterministic verification
  fails.
- `unknown`: the supplied evidence cannot close the question. Deterministic verification is
  inconclusive.
- `not-applicable`: cited evidence establishes that the dimension has no applicable claim in this
  bounded context. This passes coverage only and cannot waive another matrix row.

Every admitted finding must be linked from at least one `concern` row. A concern without a finding,
an unlinked finding, a missing or duplicate dimension, an uncited row, or an out-of-range citation
is rejected before verification.

## Stage separation

1. The host creates an immutable context with accepted intent, exact commands/results, source
   before/after excerpts, effects, outcomes, and stable evidence identifiers.
2. The REVIEW route returns findings plus the closed epistemic matrix. It receives no tools and
   cannot approve, change acceptance, or admit its own output.
3. Admission checks shape, complete dimension coverage, finding links, context identity, and every
   citation range. Admission means structurally evidence-bound, not true.
4. The deterministic verifier reconstructs objective, constraint, node, scope, and check rows from
   host evidence. It adds one epistemic-policy row per review assessment. Concerns fail; unknowns
   remain inconclusive; missing or ambiguous execution evidence remains inconclusive; failed checks
   and identity mismatches fail.
5. Human acceptance remains separate. A model verdict, confidence value, repeated wording, or
   chain-of-thought transcript can never substitute for source and outcome evidence.

## Evidence hierarchy and limitations

Deterministic command results, persisted artifacts, and independently observed outcomes carry more
weight than reviewer prose. Source citations constrain what a reviewer can point at but do not prove
that its interpretation is correct. Canonical ordering reduces position effects but does not erase
model bias. A separate process identity reduces authority coupling but does not guarantee cognitive
independence when models or training data overlap. Tests can also encode a wrong expectation, so
counterevidence, acceptance provenance, and unresolved unknowns remain visible in the final matrix.

The implementation uses semantic capability names. Integer plan lineage is a revision produced by
iteration. Opaque persisted identity, schema, and protocol identifiers retain exact tokens only
when the current persistence or external interoperability boundary requires byte-compatible
discrimination; those tokens are boundary data, not product-generation names.
