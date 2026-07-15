---
node: concepts/runtime-adapters
kind: historical-concept
edges:
  retired-by:
    - spec/bcp-0034-evolutionary-runtime
  replaced-by:
    - architecture
---

# Retired Prototype Runtime Adapters

The prototype exposed dry-run and OpenCode discovery as runtime adapters. WP26 removed that
surface: developer tools are not Blackcell runtime adapters, and capability selection belongs to
the model gateway or an explicitly injected execution edge.

The canonical product process is `blackcell-runtime` in API or worker mode. Its accepted
container, quota, recovery, telemetry, model, and execution boundaries are documented in
`../architecture.md`; the Blackcell CLI no longer exposes adapter discovery or a generic doctor.
