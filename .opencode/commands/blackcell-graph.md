---
description: Curate or inspect the BlackCell documentation graph.
agent: blackcell-mycelium
subtask: true
---
<!-- blackcell:opencode:start digest=sha256:b4fd80862b69a90b8fe9adc1efb92632147b5aa4ec886705ea5fbc7973277fd2 -->
# Workflow
Inspect or curate the BlackCell documentation graph. Respect arguments: $ARGUMENTS

1. Inventory relevant docs nodes and frontmatter.
2. Validate node IDs, kind values, edges, and entry points.
3. Detect stale/deleted references, orphan nodes, duplicated concepts, and missing cross-links.
4. Propose minimal edits or report a clean graph.

# Evidence Rules
- Cite each docs path, frontmatter key, and edge involved.
- Separate graph validity from editorial preference.

# Output Format
## Graph Status
## Evidence
## Findings
## Proposed Edits
## Verification
## Risks
## Stop Conditions

# Verification
Prefer docs graph tests and targeted reads of changed docs.

# Risks
Flag broken frontmatter, stale links, deleted targets, and root README bloat.

# Stop Conditions
Ask before broad taxonomy rewrites, deleting docs, or changing product positioning.
<!-- blackcell:opencode:end -->
