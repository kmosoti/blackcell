"""Static catalog for inspecting command-line model adapter boundaries."""

from blackcell.adapters.models.agy_cli import AgyCliModelAdapter
from blackcell.adapters.models.codex_cli import CodexCliModelAdapter
from blackcell.gateway import ToolingFacetDifference, ToolingSurfaceCatalog


def tooling_surface_catalog() -> ToolingSurfaceCatalog:
    """Return default surfaces and their operationally material comparison."""

    return ToolingSurfaceCatalog(
        surfaces=(
            CodexCliModelAdapter().tooling_surface,
            AgyCliModelAdapter().tooling_surface,
        ),
        shared_facets=(
            "canonical UTF-8 request transport over stdin",
            "request and credential content excluded from argv",
            "isolated empty temporary Git repository",
            "remote nondeterministic provider behind gateway policy",
            "host-enforced byte and deadline ceilings",
            "provider-owned credentials",
            "host-enforced final output schema",
            "exactly one JSON response object",
            "no direct gateway tool authority",
        ),
        differences=(
            ToolingFacetDifference(
                facet="output-schema",
                codex_cli="private schema file with CLI enforcement",
                agy_cli="schema embedded in canonical stdin request for prompt adherence",
                operational_effect="both are host-validated, but only Codex rejects by CLI schema",
            ),
            ToolingFacetDifference(
                facet="response-transport",
                codex_cli="private output-last-message file",
                agy_cli="standard output",
                operational_effect=(
                    "Codex separates events from the response; AGY stdout is response data"
                ),
            ),
            ToolingFacetDifference(
                facet="usage-accounting",
                codex_cli="exact token usage required from JSONL events",
                agy_cli="provider token usage unavailable",
                operational_effect="AGY usage remains null and cannot satisfy an exact-usage claim",
            ),
            ToolingFacetDifference(
                facet="version-policy",
                codex_cli="no exact version preflight",
                agy_cli="exact stdout version preflight",
                operational_effect=(
                    "AGY fails closed on CLI drift; Codex relies on capability flags"
                ),
            ),
            ToolingFacetDifference(
                facet="configuration-and-session",
                codex_cli="user config and rules ignored; session persistence disabled",
                agy_cli="provider config and storage owned by AGY; no resume selectors used",
                operational_effect="Codex minimizes ambient state more strongly than AGY",
            ),
            ToolingFacetDifference(
                facet="authority-flags",
                codex_cli="read-only sandbox, approval never, explicit feature denylist",
                agy_cli="plan mode and sandbox without bypass flags",
                operational_effect=(
                    "different provider controls implement the same proposal-only boundary"
                ),
            ),
            ToolingFacetDifference(
                facet="effort-and-provider-timeout",
                codex_cli="no adapter effort or provider timeout selector",
                agy_cli="configured effort and remaining print-timeout passed explicitly",
                operational_effect=(
                    "both retain a host deadline; AGY also receives its own deadline"
                ),
            ),
        ),
    )


__all__ = ["tooling_surface_catalog"]
