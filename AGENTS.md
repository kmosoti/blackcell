# Blackcell agent contract

- Linear is authoritative for planning state, approval, priority, hierarchy,
  and dependencies.
- GitHub is authoritative for repository ownership, code, review, and merge.
- The Blackcell proof may mutate Linear only for an approved, digest-matching
  directive and may not mutate GitHub.
- `kmosoti` is the owner, planner, reviewer, and merger.
- `kz-harbringer` is a future executor with GitHub Write access only and must
  never receive `LINEAR_API_KEY`.
- Never log, persist, journal, print, or forward credentials.
- Approved directives are immutable. Divergence is an anomaly, not an implicit
  update.
- Use one implementation assignment, branch, and pull request per delivery
  slice. Never enable auto-merge or merge a pull request.
