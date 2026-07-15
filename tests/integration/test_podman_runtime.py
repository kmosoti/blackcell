from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLACKCELL_RUN_PODMAN_TESTS") != "1",
    reason="set BLACKCELL_RUN_PODMAN_TESTS=1 to run the rootless Podman acceptance gate",
)


def test_rootless_compose_runtime_is_restricted_healthy_and_persistent() -> None:
    if shutil.which("podman") is None:
        pytest.skip("Podman is not installed")
    info = json.loads(_run(("podman", "info", "--format", "json")).stdout)
    assert info["host"]["security"]["rootless"] is True

    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    project = f"blackcellwp20{suffix.replace('-', '')}"
    image = f"localhost/blackcell-runtime-wp20-test:{suffix}"
    token = f"Wp20-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    stream_id = f"observation:container-{suffix}"
    port = _available_port()
    environment = dict(os.environ)
    environment.update(
        {
            "BLACKCELL_API_TOKEN": token,
            "BLACKCELL_PUBLISHED_PORT": str(port),
            "BLACKCELL_REPOSITORY_PATH": str(Path.cwd()),
            "BLACKCELL_RUNTIME_IMAGE": image,
            "COMPOSE_PROJECT_NAME": project,
            "PODMAN_COMPOSE_WARNING_LOGS": "false",
        }
    )
    service_process = _start_podman_service()

    try:
        _run(("podman", "compose", "config", "--quiet"), environment=environment)
        _run(
            ("podman", "compose", "build"),
            environment=environment,
            timeout=360,
        )
        _run(
            ("podman", "compose", "up", "--detach", "--no-deps", "blackcell-api"),
            environment=environment,
            timeout=120,
        )
        api = _wait_for_container("blackcell-api", environment)
        _wait_for_container_health(api, description="API container health")
        # The dependency is already proven healthy above. Avoid asking the Docker-compatible
        # provider to recreate it because this rootless host has no autonomous health scheduler.
        _run(
            (
                "podman",
                "compose",
                "up",
                "--detach",
                "--no-deps",
                "blackcell-worker",
            ),
            environment=environment,
            timeout=120,
        )
        api = _wait_for_container("blackcell-api", environment)
        worker = _wait_for_container("blackcell-worker", environment)
        _wait_for_container_health(worker, description="worker container health")
        _wait_until(
            lambda: _health_ready(port),
            description="published API readiness",
        )

        assert _inspect(api, "{{.Config.User}}") == "10001:10001"
        assert _inspect(worker, "{{.Config.User}}") == "10001:10001"
        assert _inspect(api, "{{.HostConfig.ReadonlyRootfs}}") == "true"
        assert _inspect(worker, "{{.HostConfig.ReadonlyRootfs}}") == "true"
        assert _exec(api, ("id", "-u")).stdout.strip() == "10001"
        assert _exec(worker, ("id", "-u")).stdout.strip() == "10001"
        root_write = _exec(
            api,
            (
                "python",
                "-c",
                "from pathlib import Path; Path('/opt/blackcell/write-probe').write_text('x')",
            ),
            check=False,
        )
        assert root_write.returncode != 0

        ownership = json.loads(
            _exec(
                api,
                (
                    "python",
                    "-c",
                    "import json, os, stat; "
                    "paths=('/var/lib/blackcell/data', "
                    "'/var/lib/blackcell/data/kernel.sqlite3'); "
                    "print(json.dumps([[os.stat(path).st_uid, "
                    "stat.S_IMODE(os.stat(path).st_mode)] for path in paths]))",
                ),
            ).stdout
        )
        assert ownership == [[10001, 0o700], [10001, 0o600]]

        image_environment = _run(
            ("podman", "image", "inspect", "--format", "{{json .Config.Env}}", image),
            environment=environment,
        ).stdout
        image_history = _run(
            ("podman", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image),
            environment=environment,
        ).stdout
        assert token not in image_environment
        assert token not in image_history
        assert token not in _inspect(api, "{{json .Args}}")

        created = _request_json(
            f"http://127.0.0.1:{port}/api/v1/observations",
            token=token,
            method="POST",
            body={
                "schema_version": "observation-ingest-request/v1",
                "stream_id": stream_id,
                "expected_sequence": 0,
                "source": "container-acceptance/v1",
                "correlation_id": f"correlation-{suffix}",
                "observations": [
                    {
                        "observation_id": f"observation-{suffix}",
                        "effective_at": "2026-07-13T12:00:00Z",
                        "claims": [
                            {
                                "claim_id": f"claim-{suffix}",
                                "subject": "runtime",
                                "predicate": "container-ready",
                                "value": True,
                            }
                        ],
                        "evidence": [{"locator": "container://wp20-acceptance"}],
                    }
                ],
            },
        )
        assert created["stream_id"] == stream_id

        _run(
            ("podman", "compose", "restart", "blackcell-api"),
            environment=environment,
            timeout=120,
        )
        api = _wait_for_container("blackcell-api", environment)
        _wait_for_container_health(api, description="restarted API container health")
        _wait_until(
            lambda: _health_ready(port),
            description="restarted published API readiness",
        )
        events = _request_json(
            f"http://127.0.0.1:{port}/api/v1/events?after=0&limit=200",
            token=token,
        )
        assert any(event["stream_id"] == stream_id for event in events["events"])
    finally:
        try:
            _cleanup_runtime(environment, project=project, image=image)
        finally:
            _stop_podman_service(service_process)


def _run(
    command: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = f"{completed.stdout}\n{completed.stderr}"
        if environment is not None:
            token = environment.get("BLACKCELL_API_TOKEN")
            if token:
                detail = detail.replace(token, "[REDACTED]")
        raise AssertionError(
            f"container command failed ({completed.returncode}): {command!r}\n{detail}"
        )
    return completed


def _exec(
    container: str,
    command: tuple[str, ...],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(("podman", "exec", container, *command), check=check)


def _inspect(container: str, template: str) -> str:
    return _run(("podman", "inspect", "--format", template, container)).stdout.strip()


def _wait_for_container(service: str, environment: dict[str, str]) -> str:
    container = ""

    def resolve() -> bool:
        nonlocal container
        result = _run(
            ("podman", "compose", "ps", "--quiet", service),
            environment=environment,
            check=False,
        )
        container = result.stdout.strip()
        return result.returncode == 0 and bool(container)

    _wait_until(resolve, description=f"{service} container creation")
    return container


def _wait_for_container_health(container: str, *, description: str) -> None:
    """Drive the declared check when rootless Podman has no health scheduler."""

    def healthy() -> bool:
        if _inspect(container, "{{.State.Status}}") != "running":
            return False
        current = _inspect(container, "{{.State.Health.Status}}")
        if current == "healthy":
            return True
        _run(("podman", "healthcheck", "run", container), check=False)
        return _inspect(container, "{{.State.Health.Status}}") == "healthy"

    _wait_until(healthy, description=description)


def _wait_until(check: Callable[[], bool], *, description: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except OSError, subprocess.SubprocessError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {description}")


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_podman_service() -> subprocess.Popen[str] | None:
    runtime_directory = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    socket_path = runtime_directory / "podman" / "podman.sock"
    if socket_path.exists():
        return None
    process = subprocess.Popen(
        ("podman", "system", "service", "--time=0"),
        cwd=Path.cwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _wait_until(
            lambda: socket_path.exists() and process.poll() is None,
            description="rootless Podman API service",
        )
    except Exception:
        _stop_podman_service(process)
        raise
    return process


def _stop_podman_service(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _cleanup_runtime(environment: dict[str, str], *, project: str, image: str) -> None:
    with suppress(subprocess.TimeoutExpired):
        _run(
            ("podman", "compose", "down", "--volumes", "--remove-orphans"),
            environment=environment,
            timeout=180,
            check=False,
        )
    containers = _run(
        (
            "podman",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
        check=False,
    ).stdout.split()
    if containers:
        _run(("podman", "rm", "--force", *containers), check=False)
    volumes = _run(
        (
            "podman",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ),
        check=False,
    ).stdout.split()
    if volumes:
        _run(("podman", "volume", "rm", *volumes), check=False)
    _run(("podman", "image", "rm", image), environment=environment, check=False)


def _health_ready(port: int) -> bool:
    try:
        payload = _request_json(f"http://127.0.0.1:{port}/health/ready")
    except OSError:
        return False
    return payload == {"status": "ready", "schema_version": "health/v1"}


def _request_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"accept": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.load(response)
    assert isinstance(payload, dict)
    return payload
