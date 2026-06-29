"""Typed external references."""

from pydantic import BaseModel, ConfigDict, HttpUrl


class LinearProjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    url: HttpUrl
    status: str


class LinearIssueRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    identifier: str
    title: str
    url: HttpUrl


class GitHubIssueRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: str
    repository: str
    number: int
    title: str
    url: HttpUrl
