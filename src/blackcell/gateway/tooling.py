"""Closed descriptions of command-line model adapter authority and transport surfaces."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blackcell.gateway.models import ModelCapability

_NonEmptyText = Annotated[str, Field(min_length=1)]


class ClosedToolingModel(BaseModel):
    """Strict immutable base for machine-readable adapter inspection contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PromptSurface(ClosedToolingModel):
    transport: Literal["stdin"] = "stdin"
    encoding: Literal["utf-8"] = "utf-8"
    document: Literal["canonical-input-envelope", "canonical-request-envelope"]
    includes_output_schema: bool
    request_content_in_arguments: Literal[False] = False
    request_content_is_untrusted_data: Literal[True] = True


class InvocationSurface(ClosedToolingModel):
    argument_template: tuple[_NonEmptyText, ...] = Field(min_length=1)
    noninteractive_mode: str = Field(min_length=1)
    working_directory: Literal["empty-temporary-git-repository"] = "empty-temporary-git-repository"
    model_selector: str = Field(min_length=1)
    effort_selector: str | None
    provider_timeout_selector: str | None
    host_deadline_enforced: Literal[True] = True
    setup_time_counts_toward_deadline: Literal[True] = True


class AuthoritySurface(ClosedToolingModel):
    sandbox: str = Field(min_length=1)
    approval_policy: str = Field(min_length=1)
    tool_policy: str = Field(min_length=1)
    disabled_features: tuple[_NonEmptyText, ...]
    repository_visibility: Literal["isolated-empty-repository"] = "isolated-empty-repository"
    credentials: Literal["provider-owned"] = "provider-owned"
    environment: Literal["inherited-or-explicit-replacement"] = "inherited-or-explicit-replacement"


class StructuredOutputSurface(ClosedToolingModel):
    schema_transport: str = Field(min_length=1)
    provider_schema_enforcement: bool
    host_schema_enforcement: Literal[True] = True
    response_transport: str = Field(min_length=1)
    process_stdout: str = Field(min_length=1)
    response_shape: Literal["exactly-one-json-object"] = "exactly-one-json-object"


class AccountingSurface(ClosedToolingModel):
    input_tokens: Literal["exact-provider-events", "unavailable"]
    output_tokens: Literal["exact-provider-events", "unavailable"]
    latency: Literal["host-monotonic-clock"] = "host-monotonic-clock"
    cost: Literal["unavailable"] = "unavailable"
    locality: Literal["remote"] = "remote"
    deterministic: Literal[False] = False


class SessionSurface(ClosedToolingModel):
    persistence: Literal["disabled", "provider-owned-not-resumed"]
    resume_arguments_used: Literal[False] = False
    configuration: Literal["ignored-user-config-and-rules", "provider-owned"]


class VersionSurface(ClosedToolingModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "preflight": {"const": "none"},
                        "command_template": {"maxItems": 0},
                        "required_version": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "preflight": {"const": "exact-stdout-match"},
                        "command_template": {"minItems": 1},
                        "required_version": {
                            "type": "string",
                            "minLength": 1,
                            "pattern": r"\S",
                        },
                    }
                },
            ]
        }
    )

    preflight: Literal["none", "exact-stdout-match"]
    command_template: tuple[_NonEmptyText, ...]
    required_version: _NonEmptyText | None

    @model_validator(mode="after")
    def validate_preflight_contract(self) -> VersionSurface:
        has_preflight = self.preflight == "exact-stdout-match"
        if (
            has_preflight != bool(self.command_template)
            or has_preflight != (self.required_version is not None)
            or (self.required_version is not None and not self.required_version.strip())
        ):
            raise ValueError("version preflight fields must agree")
        return self


class BudgetSurface(ClosedToolingModel):
    timeout_ceiling_seconds: float = Field(gt=0)
    max_input_bytes: int = Field(gt=0)
    max_stdout_bytes: int = Field(gt=0)
    max_stderr_bytes: int = Field(gt=0)
    max_response_bytes: int = Field(gt=0)


class CliToolingSurface(ClosedToolingModel):
    adapter_id: str = Field(min_length=1)
    capabilities: tuple[ModelCapability, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    prompt: PromptSurface
    invocation: InvocationSurface
    authority: AuthoritySurface
    structured_output: StructuredOutputSurface
    accounting: AccountingSurface
    session: SessionSurface
    version: VersionSurface
    budgets: BudgetSurface

    @model_validator(mode="after")
    def validate_capabilities(self) -> CliToolingSurface:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("tooling capabilities must be unique")
        return self


class CodexCliToolingSurface(CliToolingSurface):
    adapter_id: Literal["codex-cli"] = "codex-cli"
    tool: Literal["codex-cli"] = "codex-cli"


class AgyCliToolingSurface(CliToolingSurface):
    adapter_id: Literal["agy-cli"] = "agy-cli"
    tool: Literal["agy-cli"] = "agy-cli"
    configured_effort: Literal["low", "medium", "high"]


ToolingSurface = Annotated[
    CodexCliToolingSurface | AgyCliToolingSurface,
    Field(discriminator="tool"),
]


class ToolingFacetDifference(ClosedToolingModel):
    facet: str = Field(min_length=1)
    codex_cli: str = Field(min_length=1)
    agy_cli: str = Field(min_length=1)
    operational_effect: str = Field(min_length=1)


class ToolingSurfaceCatalog(ClosedToolingModel):
    surfaces: tuple[CodexCliToolingSurface, AgyCliToolingSurface]
    shared_facets: tuple[_NonEmptyText, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    differences: tuple[ToolingFacetDifference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> ToolingSurfaceCatalog:
        if tuple(item.tool for item in self.surfaces) != ("codex-cli", "agy-cli"):
            raise ValueError("tooling catalog must contain Codex then AGY exactly once")
        if len(set(self.shared_facets)) != len(self.shared_facets):
            raise ValueError("shared tooling facets must be unique")
        difference_facets = tuple(item.facet for item in self.differences)
        if len(set(difference_facets)) != len(difference_facets):
            raise ValueError("tooling difference facets must be unique")
        return self


__all__ = [
    "AccountingSurface",
    "AgyCliToolingSurface",
    "AuthoritySurface",
    "BudgetSurface",
    "CliToolingSurface",
    "ClosedToolingModel",
    "CodexCliToolingSurface",
    "InvocationSurface",
    "PromptSurface",
    "SessionSurface",
    "StructuredOutputSurface",
    "ToolingFacetDifference",
    "ToolingSurface",
    "ToolingSurfaceCatalog",
    "VersionSurface",
]
