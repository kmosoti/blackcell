"""Runtime composition roots."""

from pathlib import Path

from litestar import Litestar

from blackcell.bootstrap.granian import GranianServer
from blackcell.bootstrap.runtime_service import RuntimeService
from blackcell.config import RuntimeSecurityConfig
from blackcell.interfaces.http import create_http_app


def build_runtime_http_app(
    config: RuntimeSecurityConfig,
    *,
    repository_root: Path | str,
) -> Litestar:
    """Compose the canonical application use cases behind the HTTP edge."""

    service = RuntimeService.from_config(config, repository_root=repository_root)
    return create_http_app(
        service,
        authenticator=config.authenticator(),
        authorizer=config.authorizer(),
    )


__all__ = [
    "GranianServer",
    "RuntimeService",
    "build_runtime_http_app",
]
