from __future__ import annotations

from collections.abc import Callable
from typing import Any

from granian import Granian
from granian.constants import HTTPModes, Interfaces, Loops, RuntimeModes, TaskImpl
from litestar import Litestar

from blackcell.adapters.telemetry import RuntimeTelemetry
from blackcell.bootstrap.runtime_api import RuntimeApiService
from blackcell.config import RuntimeProcessConfig
from blackcell.interfaces.http import SlidingWindowRequestQuota, create_http_app
from blackcell.runtime import RuntimeStorageQuota

GRANIAN_TARGET = "blackcell.bootstrap.granian:create_granian_app"


def create_granian_app() -> Litestar:
    """Build one authenticated runtime application inside the Granian worker."""

    config = RuntimeProcessConfig.from_environment()
    telemetry = RuntimeTelemetry.from_config(config)
    try:
        service = RuntimeApiService.from_config(
            config.security,
            repository_root=config.repository_root,
            workflow_telemetry=telemetry.workflow,
            artifact_max_total_bytes=config.quota.artifact_max_total_bytes,
            alpha_isolation_root=(
                None if config.alpha_worker is None else config.alpha_worker.isolation.root
            ),
            storage_quota=RuntimeStorageQuota(
                config.security.paths,
                max_active_bytes=config.quota.active_storage_max_bytes,
                mutation_reserve_bytes=config.quota.mutation_reserve_bytes,
            ),
        )
        app = create_http_app(
            service,
            authenticator=config.security.authenticator(),
            authorizer=config.security.authorizer(),
            request_quota=SlidingWindowRequestQuota(config.quota.requests_per_minute),
        )
    except Exception:
        telemetry.shutdown()
        raise
    app.on_shutdown.append(telemetry.shutdown)
    return app


class GranianServer:
    """Production-shaped, single-worker ASGI lifecycle for runtime-v1."""

    def __init__(
        self,
        config: RuntimeProcessConfig,
        *,
        server_factory: Callable[..., Any] = Granian,
    ) -> None:
        self._server = server_factory(
            GRANIAN_TARGET,
            address=config.security.bind_host,
            port=config.security.bind_port,
            interface=Interfaces.ASGI,
            workers=1,
            runtime_threads=1,
            runtime_mode=RuntimeModes.st,
            loop=Loops.auto,
            task_impl=TaskImpl.asyncio,
            http=HTTPModes.http1,
            websockets=True,
            backlog=128,
            backpressure=config.api_backpressure,
            log_access=False,
            respawn_failed_workers=False,
            workers_kill_timeout=config.graceful_timeout_seconds,
            factory=True,
            metrics_enabled=False,
            reload=False,
            process_name="blackcell-api",
        )

    def serve(self) -> None:
        self._server.serve()


__all__ = ["GRANIAN_TARGET", "GranianServer", "create_granian_app"]
