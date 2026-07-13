from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Any

from granian.constants import HTTPModes, Interfaces, Loops, RuntimeModes, TaskImpl
from litestar import Litestar

from blackcell.bootstrap.granian import GRANIAN_TARGET, GranianServer, create_granian_app
from blackcell.bootstrap.process import main
from blackcell.config import (
    API_BACKPRESSURE_ENV,
    API_TOKEN_ENV,
    BIND_HOST_ENV,
    BIND_PORT_ENV,
    DATA_DIR_ENV,
    GRACEFUL_TIMEOUT_SECONDS_ENV,
    REPOSITORY_ROOT_ENV,
    RuntimeProcessConfig,
)

TOKEN = "Runtime-v1_granian-token.0123456789-ABCDEFG"


class CapturingServer:
    def __init__(self, target: str, **options: object) -> None:
        self.target = target
        self.options = options
        self.served = False

    def serve(self) -> None:
        self.served = True


def test_granian_server_fixes_the_bounded_single_worker_asgi_contract(tmp_path: Path) -> None:
    config = _config(tmp_path, port="8123")
    captured: list[CapturingServer] = []

    def factory(target: str, **options: object) -> CapturingServer:
        server = CapturingServer(target, **options)
        captured.append(server)
        return server

    server = GranianServer(config, server_factory=factory)
    server.serve()

    assert len(captured) == 1
    instance = captured[0]
    assert instance.target == GRANIAN_TARGET
    assert instance.served
    assert instance.options == {
        "address": "127.0.0.1",
        "port": 8123,
        "interface": Interfaces.ASGI,
        "workers": 1,
        "runtime_threads": 1,
        "runtime_mode": RuntimeModes.st,
        "loop": Loops.auto,
        "task_impl": TaskImpl.asyncio,
        "http": HTTPModes.http1,
        "websockets": False,
        "backlog": 128,
        "backpressure": 17,
        "log_access": False,
        "respawn_failed_workers": False,
        "workers_kill_timeout": 23,
        "factory": True,
        "metrics_enabled": False,
        "reload": False,
        "process_name": "blackcell-api",
    }


def test_granian_factory_builds_the_authenticated_app_and_owner_only_database(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config = _config(tmp_path)
    environment = _environment(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    app = create_granian_app()

    assert isinstance(app, Litestar)
    assert config.security.paths.database_path.is_file()
    assert stat.S_IMODE(config.security.paths.database_path.stat().st_mode) == 0o600


def test_runtime_command_is_json_first_and_does_not_echo_invalid_content(
    capsys: Any,
) -> None:
    invalid = "customer-secret-command"

    assert main((invalid,)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error": {"code": "invalid-command"}}\n'
    assert invalid not in captured.err


def test_worker_once_uses_shared_storage_and_reports_idle(tmp_path: Path, monkeypatch: Any) -> None:
    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)

    assert main(("worker", "--once")) == 3
    database = tmp_path / "data" / "kernel.sqlite3"
    assert database.is_file()
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def _config(tmp_path: Path, *, port: str = "8080") -> RuntimeProcessConfig:
    return RuntimeProcessConfig.from_environment(_environment(tmp_path, port=port))


def _environment(tmp_path: Path, *, port: str = "8080") -> dict[str, str]:
    repository = tmp_path / "repository"
    if not repository.exists():
        repository.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(repository)],
            check=True,
            capture_output=True,
            text=True,
        )
    return {
        DATA_DIR_ENV: str(tmp_path / "data"),
        API_TOKEN_ENV: TOKEN,
        REPOSITORY_ROOT_ENV: str(repository),
        BIND_HOST_ENV: "127.0.0.1",
        BIND_PORT_ENV: port,
        API_BACKPRESSURE_ENV: "17",
        GRACEFUL_TIMEOUT_SECONDS_ENV: "23",
    }
