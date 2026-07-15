from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from blackcell.config import (
    API_TOKEN_ENV,
    BIND_HOST_ENV,
    BIND_PORT_ENV,
    DATA_DIR_ENV,
    REPOSITORY_ROOT_ENV,
    WORKER_POLL_MILLISECONDS_ENV,
)

TOKEN = "Runtime-v1_process-integration.0123456789-ABCDEFG"


def test_granian_api_serves_authenticated_runtime_and_exits_on_sigterm(tmp_path: Path) -> None:
    environment, port = _environment(tmp_path)
    process = _start("api", environment=environment)
    try:
        live = _wait_for_json(f"http://127.0.0.1:{port}/health/live")
        events = _wait_for_json(
            f"http://127.0.0.1:{port}/api/v1/events?after=0&limit=1",
            token=TOKEN,
        )
        assert live["status"] == "live"
        assert events["schema_version"] == "event-page/v1"

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
    finally:
        _stop(process)

    assert process.returncode == 0
    assert TOKEN not in stdout
    assert TOKEN not in stderr
    database = tmp_path / "data" / "kernel.sqlite3"
    assert database.is_file()
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_worker_process_stops_cleanly_without_acquiring_new_work(tmp_path: Path) -> None:
    environment, _port = _environment(tmp_path)
    process = _start("worker", environment=environment)
    database = tmp_path / "data" / "kernel.sqlite3"
    try:
        _wait_for_path(database)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
    finally:
        _stop(process)

    assert process.returncode == 0
    assert stdout == ""
    assert TOKEN not in stderr
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def _start(mode: str, *, environment: dict[str, str]) -> subprocess.Popen[str]:
    executable = Path(sys.executable).parent / "blackcell-runtime"
    return subprocess.Popen(
        [str(executable), mode],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _environment(tmp_path: Path) -> tuple[dict[str, str], int]:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    port = _free_port()
    environment = dict(os.environ)
    environment.update(
        {
            DATA_DIR_ENV: str(tmp_path / "data"),
            API_TOKEN_ENV: TOKEN,
            REPOSITORY_ROOT_ENV: str(repository),
            BIND_HOST_ENV: "127.0.0.1",
            BIND_PORT_ENV: str(port),
            WORKER_POLL_MILLISECONDS_ENV: "10",
        }
    )
    return environment, port


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_json(url: str, *, token: str | None = None) -> dict[str, object]:
    deadline = time.monotonic() + 15
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers),
                timeout=1,
            ) as response:
                payload = json.loads(response.read())
                if isinstance(payload, dict):
                    return payload
        except OSError, TimeoutError, urllib.error.URLError:
            time.sleep(0.05)
    raise AssertionError("runtime endpoint did not become ready")


def _wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError("worker storage did not become ready")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=5)
