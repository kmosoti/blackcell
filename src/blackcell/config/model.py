"""Typed non-secret configuration and environment-only secrets."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class IdentityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planner_user_id: str
    planner_name: str
    planner_email: str
    owner_github_login: str
    executor_github_login: str


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str
    name: str
    default_branch: str = "main"


class ProjectStatusesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: str
    approved: str
    active: str
    completed: str
    canceled: str


class IssueStatesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backlog: str
    ready: str
    in_progress: str
    in_review: str
    done: str
    canceled: str


class ProjectPresentationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand: str = Field(min_length=1)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    repository_link_label: str = Field(min_length=1)
    icon: str | None = Field(default=None, min_length=1)


class ProjectWorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lead_user_id: str = Field(min_length=1)
    member_user_ids: list[str] = Field(default_factory=list)
    priority: Literal["low", "medium", "high", "critical"]
    label_names: list[str] = Field(default_factory=list)


class LinearConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str
    team_key: str
    team_name: str
    planning_authority: Literal["linear"]
    issue_projection_provider: Literal["linear_github_sync"]
    issue_sync_mode: Literal["two_way"]
    project_presentation: ProjectPresentationConfig
    project_workflow: ProjectWorkflowConfig
    project_statuses: ProjectStatusesConfig
    issue_states: IssueStatesConfig


class LedgerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["sqlite"]
    append_only: Literal[True]


class MaterializationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    marker_prefix: Literal["blackcell"]
    projection_timeout_seconds: int = Field(gt=0, le=900)


class PublicationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    push_remote: str = Field(default="origin", min_length=1)
    push_ssh_host: str = Field(min_length=1)
    branch_prefix: str = Field(min_length=1)
    pull_request_readiness: Literal["ready_for_review", "draft_for_followup_commits"] = (
        "ready_for_review"
    )


class BlackcellConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1"]
    identity: IdentityConfig
    repository: RepositoryConfig
    linear: LinearConfig
    ledger: LedgerConfig
    materialization: MaterializationConfig
    publication: PublicationConfig


class RuntimeSecrets(BaseSettings):
    """Secrets are loaded only from the process environment."""

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        case_sensitive=True,
        frozen=True,
    )

    linear_api_key: SecretStr | None = Field(default=None, alias="LINEAR_API_KEY")
    github_token: SecretStr | None = Field(default=None, alias="GITHUB_TOKEN")
