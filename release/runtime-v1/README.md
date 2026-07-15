# Runtime-v1 release evidence

This directory is the generated, unpublished runtime-v1 evidence bundle.

- `release.toml` is the small human-reviewed generation contract.
- `blackcell-runtime-v1.cdx.json` is a CycloneDX 1.7 pre-build SBOM for the locked Python runtime
  dependency closure.
- `verification-manifest.json` binds the declared release-candidate materials, the SBOM, retained
  WP25/WP26 evidence, and exact verification commands by SHA-256.

Regenerate only after intentional material changes:

```bash
uv run python tools/release_evidence.py generate --repo-root .
```

The normal read-only check is:

```bash
uv run python tools/release_evidence.py verify --repo-root .
```

The SBOM does not describe an installed container filesystem, host packages, vulnerabilities,
signatures, or provenance. No package, image, tag, release, or attestation is published by either
command. See `docs/guides/runtime-v1-release.md` for the supported runtime walkthrough and exact
boundary.
