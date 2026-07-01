from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    owner: str
    name: str
    node_id: str | None = None

    @classmethod
    def parse(cls, value: str, *, node_id: str | None = None) -> RepositoryRef:
        owner, separator, name = value.partition("/")
        if not separator or not owner or not name:
            raise ValueError("repository must be in owner/name form")
        return cls(owner=owner, name=name, node_id=node_id)

    @property
    def name_with_owner(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class ProjectRef:
    id: str
    title: str
    number: int | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class BlackcellConfig:
    repository: RepositoryRef
    project: ProjectRef
    provider: str = "github"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> BlackcellConfig:
        repository_data = _mapping(data, "repository")
        project_data = _mapping(data, "project")

        return cls(
            provider=_string(data, "provider", default="github"),
            repository=RepositoryRef(
                owner=_string(repository_data, "owner"),
                name=_string(repository_data, "name"),
                node_id=_optional_string(repository_data, "node_id"),
            ),
            project=ProjectRef(
                id=_string(project_data, "id"),
                title=_string(project_data, "title"),
                number=_optional_int(project_data, "number"),
                url=_optional_string(project_data, "url"),
            ),
        )


def _mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"expected [{key}] table")
    return value


def _string(data: Mapping[str, Any], key: str, *, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"expected non-empty string for {key}")
    return value


def _optional_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"expected non-empty string for {key}")
    return value


def _optional_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"expected integer for {key}")
    return value
