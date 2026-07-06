---
description: Review changes through the BlackCell quality posture.
agent: blackcell-umbra
subtask: true
---
<!-- blackcell:opencode:start digest=sha256:550b2cc96478cf782b1add92ecd8bd5acc100cc9f16222e9414f792a638865ec -->
# Workflow
Review the current repository changes through the BlackCell quality posture.

1. Inspect git status and relevant diff when available.
2. Check behavior regressions, security/auth risks, contract drift, missing tests, stale docs, and generated artifact drift.
3. Prioritize findings by severity: blocker, high, medium, low.
4. Use non-mutating checks only.

# Evidence Rules
- Findings first; every finding needs path, evidence, impact, and recommendation.
- Separate confirmed defects from residual risks.

# Output Format
## Findings
| Severity | Path | Evidence | Impact | Recommendation |
## Verification
## Residual Risks
## Stop Conditions

# Verification
List checks run or recommended, including lint, tests, type checks, and drift checks when relevant.

# Risks
Call out unverified behavior, missing tests, partial context, and skipped checks.

# Stop Conditions
Do not edit files, approve your own changes, mutate remotes, or use secrets.
<!-- blackcell:opencode:end -->
