from collections.abc import Callable
from dataclasses import dataclass, field

from blackcell.config.models import BlackcellConfig
from blackcell.providers.base import ProjectProvider
from blackcell.providers.github import GitHubProjectsProvider

ProviderFactory = Callable[[BlackcellConfig], ProjectProvider]


@dataclass(slots=True)
class ProviderRegistry:
    _factories: dict[str, ProviderFactory] = field(default_factory=dict)

    def register(self, name: str, factory: ProviderFactory) -> None:
        if not name:
            raise ValueError("provider name cannot be empty")
        self._factories[name] = factory

    def create(self, name: str, config: BlackcellConfig) -> ProjectProvider:
        try:
            factory = self._factories[name]
        except KeyError as error:
            available = ", ".join(self.names()) or "none"
            raise ValueError(
                f"unknown provider {name!r}; available providers: {available}"
            ) from error
        return factory(config)

    def names(self) -> list[str]:
        return sorted(self._factories)


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(GitHubProjectsProvider.name, GitHubProjectsProvider)
    return registry
