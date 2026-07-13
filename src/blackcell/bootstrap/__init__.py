"""Runtime-v1 composition roots."""

from pathlib import Path

from litestar import Litestar

from blackcell.bootstrap.runtime_api import RuntimeApiService
from blackcell.config import RuntimeSecurityConfig
from blackcell.interfaces.http import create_http_app


def build_runtime_http_app(
    config: RuntimeSecurityConfig,
    *,
    repository_root: Path | str,
) -> Litestar:
    """Compose the canonical application use cases behind the HTTP edge."""

    service = RuntimeApiService.from_config(config, repository_root=repository_root)
    return create_http_app(
        service,
        authenticator=config.authenticator(),
        authorizer=config.authorizer(),
    )


__all__ = ["RuntimeApiService", "build_runtime_http_app"]
